"""Offline code activation; mutable account, privacy and pairing state is never copied.

The stable launcher verifies the same selection and monotonic reader policy before
importing release code. The canonical lifetime gate is shared by every process
that may mutate that state, including adapter/native children.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from halo_server.media.manager import owned_thread, settle_owned
from halo_server.paths import open_private_directory
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

READER_VERSION = 14
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ActivationError(PrivateJsonError):
    def __init__(
        self,
        message="Managed release operation failed",
        *,
        possibly_saved=False,
        code="unavailable",
    ):
        super().__init__(message, possibly_saved=possibly_saved)
        self.code = code


class ActivationConflict(ActivationError):
    pass


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_REVISION


@dataclass(frozen=True)
class ReleaseReference:
    release_id: str
    manifest_sha256: str

    def __post_init__(self):
        if any(
            not isinstance(value, str) or DIGEST.fullmatch(value) is None
            for value in (self.release_id, self.manifest_sha256)
        ):
            raise ActivationError("Release identity is invalid", code="invalid_release")

    def as_dict(self):
        return {"release_id": self.release_id, "manifest_sha256": self.manifest_sha256}


@dataclass(frozen=True)
class ReleaseSelection:
    version: int = 1
    revision: int = 0
    active: ReleaseReference | None = None
    previous: ReleaseReference | None = None

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or not _integer(self.revision)
            or self.active is not None
            and not isinstance(self.active, ReleaseReference)
            or self.previous is not None
            and not isinstance(self.previous, ReleaseReference)
        ):
            raise ActivationError("Release selection is invalid")
        if self.active is None and self.previous is not None:
            raise ActivationError("Release selection has no active code")

    def as_dict(self):
        return {
            "version": self.version,
            "revision": self.revision,
            "active": self.active.as_dict() if self.active else None,
            "previous": self.previous.as_dict() if self.previous else None,
        }


@dataclass(frozen=True)
class ReleasePolicy:
    version: int = 1
    revision: int = 0
    minimum_reader: int = READER_VERSION

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or not _integer(self.revision)
            or not _integer(self.minimum_reader, READER_VERSION)
        ):
            raise ActivationError("Mutable release reader policy is invalid")

    def as_dict(self):
        return {
            "version": self.version,
            "revision": self.revision,
            "minimum_reader": self.minimum_reader,
        }


def _reference(value):
    if not isinstance(value, dict) or set(value) != {"release_id", "manifest_sha256"}:
        raise ActivationError("Release reference is invalid")
    return ReleaseReference(**value)


def _selection(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "revision", "active", "previous"}
        or value["active"] is None
    ):
        raise ActivationError("Release selection format is invalid")
    return ReleaseSelection(
        value["version"],
        value["revision"],
        _reference(value["active"]),
        _reference(value["previous"]) if value["previous"] is not None else None,
    )


def _policy(value):
    if not isinstance(value, dict) or set(value) != {"version", "revision", "minimum_reader"}:
        raise ActivationError("Mutable release policy format is invalid")
    return ReleasePolicy(**value)


@dataclass(frozen=True)
class _Enrollment:
    version: int = 1
    revision: int = 0
    initialized: bool = False

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or not _integer(self.revision)
            or type(self.initialized) is not bool
        ):
            raise ActivationError("Release policy enrollment is invalid")

    def as_dict(self):
        return {"version": self.version, "revision": self.revision, "initialized": self.initialized}


def _enrollment(value):
    if not isinstance(value, dict) or set(value) != {"version", "revision", "initialized"}:
        raise ActivationError("Release policy enrollment format is invalid")
    return _Enrollment(**value)


class SelectionStore(PrivateJsonStore):
    def __init__(self, prefix):
        super().__init__(
            Path(prefix) / "selection.json",
            snapshot_type=ReleaseSelection,
            decode=_selection,
            default=ReleaseSelection,
            max_bytes=4096,
            label="Managed release selection",
            noun="release selection",
            error_type=ActivationError,
            conflict_type=ActivationConflict,
        )

    def activate(self, reference, *, expected_revision):
        if not isinstance(reference, ReleaseReference):
            raise ActivationError("Release reference is invalid")
        return self.update(
            lambda current: (
                current
                if current.active == reference
                else replace(current, active=reference, previous=current.active)
            ),
            expected_revision=expected_revision,
        )

    def remove(self, *, expected_revision):
        self._expected(expected_revision)
        with self._transaction() as directory:
            current = self._read_locked(directory)
            self._check_revision(current, expected_revision)
            if current.active is not None:
                os.unlink(self.path.name, dir_fd=directory)
                try:
                    os.fsync(directory)
                except OSError as error:
                    raise ActivationError(
                        "Release selection removal may have completed", possibly_saved=True
                    ) from error
            return current


class PolicyStore(PrivateJsonStore):
    def __init__(self, state_directory):
        super().__init__(
            Path(state_directory) / "release-policy.json",
            snapshot_type=ReleasePolicy,
            decode=_policy,
            default=ReleasePolicy,
            max_bytes=4096,
            label="Mutable release policy",
            noun="release policy",
            error_type=ActivationError,
            conflict_type=ActivationConflict,
        )
        self.enrollment = PrivateJsonStore(
            self.path.with_name(".release-policy.json.enrolled"),
            snapshot_type=_Enrollment,
            decode=_enrollment,
            default=_Enrollment,
            max_bytes=1024,
            label="Release policy enrollment",
            noun="release policy enrollment",
            error_type=ActivationError,
            conflict_type=ActivationConflict,
        )

    def _read_locked(self, directory):
        current = super()._read_locked(directory)
        if current == ReleasePolicy() and self.enrollment.load().initialized:
            raise ActivationError(
                "Enrolled mutable reader policy is missing", code="policy_missing"
            )
        return current

    def _save_locked(self, directory, current, snapshot):
        if snapshot.minimum_reader < current.minimum_reader:
            raise ActivationError("Mutable reader policy cannot decrease", code="reader_too_old")
        return super()._save_locked(directory, current, snapshot)

    def advance(self, minimum_reader):
        if not _integer(minimum_reader, READER_VERSION):
            raise ActivationError("Release reader is unsupported", code="reader_too_old")

        def change(current):
            return replace(current, minimum_reader=max(current.minimum_reader, minimum_reader))

        with self._transaction() as directory:
            current = self._read_locked(directory)
            proposed = self._validated(change(current))
            saved = (
                self._save_locked(directory, current, proposed)
                if current.revision == 0 or proposed != current
                else current
            )
            self.enrollment.update(lambda row: replace(row, initialized=True))
            return saved


class ServiceController(Protocol):
    async def stop(self): ...
    async def start(self, reference: ReleaseReference): ...
    async def healthy(self, reference: ReleaseReference) -> bool: ...
    async def disable(self): ...
    async def prepare(self, prefix: Path): ...


class InstallLock:
    """One operator per prefix; distinct from the global lifetime reader gate."""

    def __init__(self, prefix, *, name="install.lock"):
        if name not in {"install.lock", "release-install.lock"}:
            raise ActivationError("Installer lock name is invalid")
        self.directory = self.descriptor = -1
        try:
            self.directory = open_private_directory(Path(prefix), create=True)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            try:
                self.descriptor = os.open(
                    name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.directory
                )
                os.fchmod(self.descriptor, 0o600)
            except FileExistsError:
                self.descriptor = os.open(name, flags, dir_fd=self.directory)
            info = os.fstat(self.descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size != 0
            ):
                raise ActivationError("Managed installer lock is unsafe")
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ActivationError(
                    "Another release operation is running", code="busy"
                ) from error
        except BaseException:
            self.close()
            raise

    def close(self):
        for name in ("descriptor", "directory"):
            value = getattr(self, name)
            if value >= 0:
                os.close(value)
                setattr(self, name, -1)


@dataclass(frozen=True)
class ActivationResult:
    status: str
    selection: ReleaseSelection
    policy: ReleasePolicy
    code: str = "ok"

    def as_dict(self):
        return {
            "status": self.status,
            "code": self.code,
            "selection": self.selection.as_dict(),
            "policy": self.policy.as_dict(),
            "mutable_state_restored": False,
        }


class ReleaseActivator:
    def __init__(
        self,
        prefix,
        state_directory,
        service,
        *,
        verify=None,
        guard=None,
        install=None,
        prepare_unit=False,
        initial_mode=None,
    ):
        self.prefix = Path(prefix)
        self.state_directory = Path(state_directory)
        if not self.prefix.is_absolute() or not self.state_directory.is_absolute():
            raise ActivationError("Managed release paths must be absolute")
        self.selection = SelectionStore(self.prefix)
        self.policy = PolicyStore(self.state_directory)
        self.service = service
        self.verify = verify or self._verify
        self.guard = guard or self._guard
        self.install = install or self._install
        if type(prepare_unit) is not bool:
            raise ActivationError("Unit installation preference is invalid")
        self.prepare_unit = prepare_unit
        if initial_mode is not None and initial_mode != "homekit":
            raise ActivationError("Initial managed mode is invalid", code="bad_parameters")
        self.initial_mode = initial_mode
        self._task = None

    @staticmethod
    def _verify(root, digest, minimum_reader):
        from halo_server.release import ReleaseError, verify_release

        try:
            return verify_release(root, digest, minimum_reader=minimum_reader)
        except ReleaseError as error:
            raise ActivationError(code=error.code) from error

    def _install(self, verified):
        from halo_server.release_launcher import install_launchers

        install_launchers(self.prefix, interpreter=verified.interpreter)

    def _guard(self):
        from halo_server.release_guard import ReleaseGuard, ReleaseGuardError

        try:
            return ReleaseGuard.acquire(
                self.state_directory, exclusive=True, nonblocking=True, create=True
            )
        except ReleaseGuardError as error:
            raise ActivationError(
                "Managed daemon or child retirement is incomplete", code="busy"
            ) from error

    async def activate(self, reference, *, expected_revision):
        return await self._owned(reference, expected_revision, False)

    async def rollback(self, *, expected_revision):
        return await self._owned(None, expected_revision, True)

    async def _owned(self, reference, revision, rollback):
        if self._task is not None and not self._task.done():
            raise ActivationError("Another release operation is running", code="busy")
        self._task = asyncio.create_task(
            self._activate(reference, revision, rollback), name="managed-release-activation"
        )
        try:
            return await asyncio.shield(self._task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await settle_owned(self._task)
            raise

    async def _activate(self, reference, revision, rollback):
        global_lock = InstallLock(self.state_directory, name="release-install.lock")
        lock = gate = None
        try:
            lock = InstallLock(self.prefix)
            selected = await owned_thread(self.selection.load)
            if not _integer(revision) or selected.revision != revision:
                raise ActivationConflict("Managed release selection changed", code="conflict")
            if rollback:
                reference = selected.previous
            if not isinstance(reference, ReleaseReference):
                raise ActivationError(
                    "No compatible previous release is selected", code="no_previous"
                )
            policy = await owned_thread(self.policy.load)
            if policy.minimum_reader > READER_VERSION:
                raise ActivationError(
                    "Activation operator cannot read current mutable policy",
                    code="operator_reader_too_old",
                )
            verified = await owned_thread(
                self.verify,
                self.prefix / "releases" / reference.release_id,
                reference.manifest_sha256,
                policy.minimum_reader,
            )
            reader = verified.reader_version
            if not _integer(reader, policy.minimum_reader):
                raise ActivationError(
                    "Selected code cannot read current mutable state", code="reader_too_old"
                )
            from halo_server.release_modes import ManagedModeStore

            mode_store = ManagedModeStore(self.state_directory / "configuration.json")
            await owned_thread(mode_store.preflight, self.initial_mode)
            if self.prepare_unit:
                await self.service.prepare(self.prefix)
            await self.service.stop()
            # EX is nonblocking and only succeeds after daemon AND all inherited
            # native/adapter owners retired. A PID disappearing is not proof.
            gate = self.guard()
            # Another prefix may have raised the canonical floor while this
            # transaction verified artifacts or waited for old processes.
            policy = await owned_thread(self.policy.load)
            if policy.minimum_reader > READER_VERSION:
                raise ActivationError(
                    "Activation operator cannot read current mutable policy",
                    code="operator_reader_too_old",
                )
            if reader < policy.minimum_reader:
                raise ActivationError(
                    "Selected code cannot read current mutable state", code="reader_too_old"
                )
            await owned_thread(mode_store.prepare, self.initial_mode)
            if selected.active is None:
                await owned_thread(self.install, verified)
            policy = await owned_thread(self.policy.advance, reader)
            try:
                selected = await owned_thread(
                    self.selection.activate, reference, expected_revision=revision
                )
            except ActivationError:
                # Never pretend the old selection survived a post-replace error.
                selected = await owned_thread(self.selection.load)
                raise
            gate.close()
            gate = None
            try:
                await self.service.start(reference)
                if not await self.service.healthy(reference):
                    raise ActivationError(
                        "Managed release failed local health checks", code="local_health_failed"
                    )
            except Exception:
                await self.service.stop()
                gate = self.guard()
                policy = await owned_thread(self.policy.load)
                previous = selected.previous
                if previous is None:
                    return ActivationResult("stopped", selected, policy, "local_health_failed")
                try:
                    old = await owned_thread(
                        self.verify,
                        self.prefix / "releases" / previous.release_id,
                        previous.manifest_sha256,
                        policy.minimum_reader,
                    )
                    if old.reader_version < policy.minimum_reader:
                        raise ActivationError(code="reader_too_old")
                except Exception:
                    return ActivationResult("stopped", selected, policy, "rollback_incompatible")
                selected = await owned_thread(
                    self.selection.activate, previous, expected_revision=selected.revision
                )
                gate.close()
                gate = None
                try:
                    await self.service.start(previous)
                    if not await self.service.healthy(previous):
                        raise ActivationError(code="local_health_failed")
                except Exception:
                    await self.service.stop()
                    gate = self.guard()
                    return ActivationResult("stopped", selected, policy, "rollback_health_failed")
                return ActivationResult("rolled_back", selected, policy, "local_health_failed")
            return ActivationResult("active", selected, policy)
        finally:
            if gate is not None:
                gate.close()
            if lock is not None:
                lock.close()
            global_lock.close()

    async def uninstall(self, *, expected_revision):
        """Remove managed selection; retain code and all mutable state for recovery."""
        if self._task is not None and not self._task.done():
            raise ActivationError("Another release operation is running", code="busy")
        self._task = asyncio.create_task(
            self._uninstall(expected_revision), name="managed-release-uninstall"
        )
        try:
            return await asyncio.shield(self._task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await settle_owned(self._task)
            raise

    async def _uninstall(self, revision):
        global_lock = InstallLock(self.state_directory, name="release-install.lock")
        lock = gate = None
        try:
            lock = InstallLock(self.prefix)
            selected = await owned_thread(self.selection.load)
            if not _integer(revision) or selected.revision != revision:
                raise ActivationConflict("Managed release selection changed", code="conflict")
            await self.service.stop()
            gate = self.guard()
            await self.service.disable()
            await owned_thread(self.selection.remove, expected_revision=revision)
            return {
                "status": "uninstalled",
                "code": "ok",
                "mutable_state_preserved": True,
                "release_files_preserved": True,
            }
        finally:
            if gate is not None:
                gate.close()
            if lock is not None:
                lock.close()
            global_lock.close()
