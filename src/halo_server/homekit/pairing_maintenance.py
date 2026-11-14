"""Durable deny-first pairing reset journal and bounded offline helper owner."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from uuid import UUID

from halo_server.account_scope import valid_profile_id
from halo_server.homekit.storage_owner import PairingDirectoryOwner
from halo_server.homekit.text import literal_text
from halo_server.media.manager import settle_owned
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore
from halo_server.release_guard import inherited_guard_environment, inherited_guard_fds

MANIFEST_KEYS = {"accessory_info", "identifier_cache", "controller_storage"}
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
USERNAME = re.compile(r"(?:[A-F0-9]{2}:){5}[A-F0-9]{2}\Z")
PHASES = ("denied", "quiesced", "cleanup_started", "completed")


class PairingMaintenanceError(PrivateJsonError):
    pass


class PairingMaintenanceConflict(PairingMaintenanceError):
    pass


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def manifest_revision(manifest):
    if (
        not isinstance(manifest, dict)
        or set(manifest) != MANIFEST_KEYS
        or any(
            value is not None and (not isinstance(value, str) or DIGEST.fullmatch(value) is None)
            for value in manifest.values()
        )
    ):
        raise PairingMaintenanceError("Pairing manifest is invalid")
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"halo-pairing-manifest-v1\0" + raw).hexdigest()


@dataclass(frozen=True)
class PairingReset:
    operation_id: str
    profile_id: str
    camera_id: str
    accessory_id: str
    username: str
    pairing_revision: str
    manifest: tuple[tuple[str, str | None], ...]
    phase: str = "denied"

    def __post_init__(self):
        if (
            not valid_uuid(self.operation_id)
            or not valid_profile_id(self.profile_id)
            or not literal_text(self.camera_id, 128, empty=False)
            or not valid_uuid(self.accessory_id)
            or not isinstance(self.username, str)
            or USERNAME.fullmatch(self.username) is None
            or self.phase not in PHASES
            or not isinstance(self.manifest, tuple)
            or len(self.manifest) != 3
            or len(dict(self.manifest)) != 3
            or manifest_revision(dict(self.manifest)) != self.pairing_revision
        ):
            raise PairingMaintenanceError("Pairing reset identity is invalid")

    @property
    def key(self):
        return self.profile_id, self.camera_id

    def as_dict(self):
        return {
            "operation_id": self.operation_id,
            "profile_id": self.profile_id,
            "camera_id": self.camera_id,
            "accessory_id": self.accessory_id,
            "username": self.username,
            "pairing_revision": self.pairing_revision,
            "manifest": dict(self.manifest),
            "phase": self.phase,
        }


@dataclass(frozen=True)
class PairingJournal:
    version: int = 1
    revision: int = 0
    entries: tuple[PairingReset, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or not isinstance(self.entries, tuple)
            or len(self.entries) > 256
            or any(not isinstance(row, PairingReset) for row in self.entries)
            or len({row.key for row in self.entries}) != len(self.entries)
            or len({row.accessory_id for row in self.entries}) != len(self.entries)
            or len({row.operation_id for row in self.entries}) != len(self.entries)
        ):
            raise PairingMaintenanceError("Pairing maintenance journal is invalid")

    def as_dict(self):
        return {
            "version": self.version,
            "revision": self.revision,
            "entries": [row.as_dict() for row in self.entries],
        }


def _decode(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "revision", "entries"}
        or not isinstance(value["entries"], list)
        or len(value["entries"]) > 256
    ):
        raise PairingMaintenanceError("Pairing maintenance journal format is invalid")
    rows = []
    for row in value["entries"]:
        if not isinstance(row, dict) or set(row) != {
            "operation_id",
            "profile_id",
            "camera_id",
            "accessory_id",
            "username",
            "pairing_revision",
            "manifest",
            "phase",
        }:
            raise PairingMaintenanceError("Pairing reset row format is invalid")
        manifest_revision(row["manifest"])
        rows.append(PairingReset(**(row | {"manifest": tuple(sorted(row["manifest"].items()))})))
    return PairingJournal(**(value | {"entries": tuple(rows)}))


@dataclass(frozen=True)
class _Enrollment:
    version: int = 1
    revision: int = 0
    initialized: bool = False

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or type(self.initialized) is not bool
        ):
            raise PairingMaintenanceError("Pairing maintenance enrollment is invalid")

    def as_dict(self):
        return {"version": self.version, "revision": self.revision, "initialized": self.initialized}


def _enrollment(value):
    if not isinstance(value, dict) or set(value) != {"version", "revision", "initialized"}:
        raise PairingMaintenanceError("Pairing maintenance enrollment format is invalid")
    return _Enrollment(**value)


class PairingMaintenanceStore(PrivateJsonStore):
    def __init__(self, path):
        super().__init__(
            path,
            snapshot_type=PairingJournal,
            decode=_decode,
            default=PairingJournal,
            max_bytes=512 * 1024,
            label="Pairing maintenance",
            noun="pairing maintenance",
            error_type=PairingMaintenanceError,
            conflict_type=PairingMaintenanceConflict,
        )
        self.enrollment = PrivateJsonStore(
            self.path.with_name(f".{self.path.name}.enrolled"),
            snapshot_type=_Enrollment,
            decode=_enrollment,
            default=_Enrollment,
            max_bytes=1024,
            label="Pairing maintenance enrollment",
            noun="pairing maintenance enrollment",
            error_type=PairingMaintenanceError,
            conflict_type=PairingMaintenanceConflict,
        )

    def _read_locked(self, directory):
        current = super()._read_locked(directory)
        if current == PairingJournal() and self.enrollment.load().initialized:
            raise PairingMaintenanceError("Enrolled pairing maintenance journal is missing")
        return current

    def begin(self, row):
        if not isinstance(row, PairingReset) or row.phase != "denied":
            raise PairingMaintenanceError("Pairing reset must begin denied")

        def change(current):
            old = next((item for item in current.entries if item.key == row.key), None)
            duplicate = next(
                (item for item in current.entries if item.operation_id == row.operation_id), None
            )
            if duplicate is not None:
                if replace(duplicate, phase="denied") != row:
                    raise PairingMaintenanceConflict("Pairing reset operation identity changed")
                return current
            if old is not None and (
                old.phase != "completed"
                or old.accessory_id != row.accessory_id
                or old.username != row.username
            ):
                raise PairingMaintenanceConflict("Pairing reset already pending for this identity")
            entries = tuple(item for item in current.entries if item.key != row.key) + (row,)
            return replace(current, entries=entries)

        saved = self.update(change)
        self.enrollment.update(lambda current: replace(current, initialized=True))
        return saved

    def advance(self, operation_id, phase):
        if phase not in PHASES:
            raise PairingMaintenanceError("Pairing reset phase is invalid")

        def change(current):
            row = next((row for row in current.entries if row.operation_id == operation_id), None)
            if row is None:
                raise PairingMaintenanceConflict("Pairing reset operation is unknown")
            if (
                PHASES.index(phase) < PHASES.index(row.phase)
                or PHASES.index(phase) > PHASES.index(row.phase) + 1
            ):
                raise PairingMaintenanceConflict(
                    "Pairing reset phase cannot be skipped or reversed"
                )
            return replace(
                current,
                entries=tuple(
                    replace(item, phase=phase) if item is row else item for item in current.entries
                ),
            )

        return self.update(change)


class MaintenanceOwner:
    """Keep the exact flock description alive across adapter exit and helper."""

    helper_name = "pairing-reset.js"

    def __init__(self, directory_fd, lock_fd):
        self.directory_fd, self.lock_fd = directory_fd, lock_fd
        self.process = None
        self._task = None

    @classmethod
    def capture(cls, runtime, path):
        owner = getattr(runtime, "_pairing_owner", None) if runtime is not None else None
        if owner is not None:
            directory = os.dup(owner.directory_fd)
            try:
                lock = os.dup(owner.lock_fd)
            except BaseException:
                os.close(directory)
                raise
            return cls(directory, lock)
        owner = PairingDirectoryOwner.acquire(path)
        directory = -1
        try:
            directory = os.dup(owner.directory_fd)
            lock = os.dup(owner.lock_fd)
            result = cls(directory, lock)
            directory = -1
            return result
        finally:
            if directory >= 0:
                os.close(directory)
            owner.close()

    def close(self):
        if self._task is not None and not self._task.done():
            raise PairingMaintenanceError("Pairing helper operation is still owned")
        if self.process is not None and self.process.returncode is None:
            raise PairingMaintenanceError("Pairing reset helper is still owned")
        for key in ("directory_fd", "lock_fd"):
            value = getattr(self, key)
            if value >= 0:
                os.close(value)
                setattr(self, key, -1)

    async def cleanup(self, resources, row, *, resume):
        if self._task is not None and not self._task.done():
            raise PairingMaintenanceError("Pairing helper operation is already running")
        self._task = asyncio.create_task(
            self._cleanup(resources, row, resume=resume), name="pairing-offline-helper"
        )
        try:
            return await asyncio.shield(self._task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await settle_owned(self._task)
            raise

    async def _cleanup(self, resources, row, *, resume):
        helper = resources.entry.with_name(self.helper_name)
        payload = {
            "v": 1,
            "operation_id": row.operation_id,
            "accessory_id": row.accessory_id,
            "username": row.username,
            "pairing_revision": row.pairing_revision,
            "pairing_manifest": dict(row.manifest),
            "resume_cleanup": resume,
        }
        if self.process is not None:
            if self.process.returncode is None:
                self.process.kill()
                try:
                    async with asyncio.timeout(5):
                        await self.process.wait()
                except TimeoutError as error:
                    raise PairingMaintenanceError(
                        "Pairing reset helper cleanup is pending"
                    ) from error
            self.process = None
        self.process = await asyncio.create_subprocess_exec(
            str(resources.node),
            str(helper),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=4097,
            pass_fds=(self.directory_fd, self.lock_fd, *inherited_guard_fds()),
            env={
                **inherited_guard_environment(),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TZ": "UTC",
                "HALO_PARENT_PID": str(os.getpid()),
                "HALO_PAIRING_FD": str(self.directory_fd),
                "HALO_PAIRING_LOCK_FD": str(self.lock_fd),
            },
        )
        process = self.process
        try:
            process.stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            async with asyncio.timeout(10):
                try:
                    await process.stdout.readexactly(4097)
                except asyncio.IncompleteReadError as error:
                    output = error.partial
                else:
                    raise PairingMaintenanceError("Pairing reset helper response is oversized")
                code = await process.wait()
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError) as error:
                raise PairingMaintenanceError("Pairing reset helper response is invalid") from error
            if json.dumps(result, separators=(",", ":")).encode() + b"\n" != output:
                raise PairingMaintenanceError("Pairing helper response is not canonical")
            expected = {
                key: payload[key]
                for key in ("v", "operation_id", "accessory_id", "username", "pairing_revision")
            }
            if (
                not isinstance(result, dict)
                or set(result) != set(expected) | {"accepted", "code"}
                or any(result[key] != value for key, value in expected.items())
                or type(result["v"]) is not int
                or result["accepted"] is not True
                or result["code"] != "ok"
                or code != 0
            ):
                raise PairingMaintenanceConflict("Pairing reset helper did not prove cleanup")
            os.fsync(self.directory_fd)
        finally:
            if process.returncode is None:
                process.kill()
                try:
                    async with asyncio.timeout(5):
                        await process.wait()
                except TimeoutError as error:
                    raise PairingMaintenanceError(
                        "Pairing reset helper retirement is pending"
                    ) from error
            if process.returncode is not None:
                self.process = None
