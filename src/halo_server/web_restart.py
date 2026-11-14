"""Owner-requested restart of one verified managed daemon; never code activation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import UUID

from halo_server.media.manager import owned_thread
from halo_server.private_json import PrivateJsonError, PrivateJsonStore
from halo_server.release_activation import (
    ActivationError,
    InstallLock,
    ReleaseReference,
    SelectionStore,
)
from halo_server.release_execution import managed_context
from halo_server.release_launcher import verify_installed_launchers
from halo_server.release_legacy import capture_owner
from halo_server.release_service import HEALTH_SECONDS, UNIT_NAME, local_release_healthy
from halo_server.release_services import inspect_unit, unit_command

MAX_OPERATIONS = 256


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class RestartRecord:
    operation_id: str
    selection_revision: int
    release_id: str
    manifest_sha256: str
    initial_invocation: str
    status: str = "pending"
    code: str = "restart_pending"
    final_invocation: str = ""

    def __post_init__(self):
        if (
            not _uuid(self.operation_id)
            or type(self.selection_revision) is not int
            or not 0 <= self.selection_revision < 2**53
            or self.status not in {"pending", "completed", "unknown", "rejected"}
            or self.code
            not in {
                "restart_pending",
                "ok",
                "restart_unverified",
                "service_failed",
                "selection_changed",
            }
            or any(
                not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in (self.release_id, self.manifest_sha256)
            )
            or any(
                not isinstance(value, str) or value and re.fullmatch(r"[a-f0-9]{32}", value) is None
                for value in (self.initial_invocation, self.final_invocation)
            )
        ):
            raise PrivateJsonError("Restart operation is invalid")

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class RestartJournal:
    version: int = 1
    revision: int = 0
    operations: tuple[RestartRecord, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision < 2**53
            or not isinstance(self.operations, tuple)
            or len(self.operations) > MAX_OPERATIONS
            or any(not isinstance(row, RestartRecord) for row in self.operations)
            or len({row.operation_id for row in self.operations}) != len(self.operations)
        ):
            raise PrivateJsonError("Restart journal is invalid")

    def as_dict(self):
        return {
            "version": self.version,
            "revision": self.revision,
            "operations": [row.as_dict() for row in self.operations],
        }


def _decode(value):
    if set(value) != {"version", "revision", "operations"} or not isinstance(
        value["operations"], list
    ):
        raise PrivateJsonError("Restart journal is invalid")
    try:
        return RestartJournal(
            value["version"],
            value["revision"],
            tuple(RestartRecord(**row) for row in value["operations"]),
        )
    except (TypeError, ValueError) as error:
        raise PrivateJsonError("Restart journal is invalid") from error


def _store(state):
    return PrivateJsonStore(
        Path(state) / "web-restart-operations.json",
        snapshot_type=RestartJournal,
        decode=_decode,
        default=RestartJournal,
        max_bytes=256 * 1024,
        label="Daemon restart journal",
        noun="restart operation",
        error_type=PrivateJsonError,
        conflict_type=PrivateJsonError,
    )


class ManagedDaemonRestart:
    def __init__(
        self,
        prefix,
        paths,
        client,
        *,
        command=unit_command,
        config_directory=None,
        context=None,
        verify_launcher=verify_installed_launchers,
    ):
        self.prefix, self.paths, self.client = Path(prefix), paths, client
        self.command, self.verify_launcher = command, verify_launcher
        self.context = managed_context() if context is None else context
        self.config_directory = Path(
            config_directory or os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        )
        self.store = _store(paths.state_directory)
        self.selection = SelectionStore(self.prefix)
        self.lock = asyncio.Lock()
        self.tasks = {}
        self.closing = False

    async def _inspect(self):
        return await inspect_unit(
            UNIT_NAME,
            self.prefix,
            self.paths,
            self.config_directory,
            command=self.command,
        )

    async def _preflight(self):
        context = self.context
        if context is None or Path(context.root).parent.parent != self.prefix:
            raise ActivationError("Restart requires a managed installation", code="unsupported")
        selected = await owned_thread(self.selection.load)
        reference = ReleaseReference(context.release_id, context.manifest_sha256)
        if selected.active != reference:
            raise ActivationError("The selected release changed", code="selection_changed")
        await owned_thread(self.verify_launcher, self.prefix, interpreter=context.interpreter)
        unit = await self._inspect()
        if not unit.exists:
            raise ActivationError("The daemon unit is not installed", code="unsupported")
        return selected, unit

    async def info(self):
        try:
            selected, unit = await self._preflight()
            return {
                "available": not self.closing,
                "code": "ok" if not self.closing else "stopping",
                "selection_revision": selected.revision,
                "daemon_active": unit.active,
            }
        except Exception as error:
            return {
                "available": False,
                "code": getattr(error, "code", "unavailable"),
                "selection_revision": None,
                "daemon_active": None,
            }

    async def start(self, operation_id, expected_revision):
        if (
            not _uuid(operation_id)
            or type(expected_revision) is not int
            or not 0 <= expected_revision < 2**53
        ):
            raise ActivationError("Restart parameters are invalid", code="bad_parameters")
        async with self.lock:
            journal = await owned_thread(self.store.load)
            previous = next(
                (row for row in journal.operations if row.operation_id == operation_id), None
            )
            if previous is not None:
                if previous.selection_revision != expected_revision:
                    raise ActivationError("Restart identity changed", code="operation_conflict")
                return await self.get(operation_id)
            if self.closing or any(not task.done() for task in self.tasks.values()):
                raise ActivationError("Another restart is still owned", code="busy")
            if len(journal.operations) >= MAX_OPERATIONS:
                raise ActivationError(
                    "Restart history is full; use local administration", code="capacity"
                )
            maintenance = InstallLock(self.paths.state_directory, name="release-install.lock")
            try:
                selected, unit = await self._preflight()
                if selected.revision != expected_revision:
                    raise ActivationError(
                        "The selected release changed", code="configuration_conflict"
                    )
                record = RestartRecord(
                    operation_id,
                    expected_revision,
                    selected.active.release_id,
                    selected.active.manifest_sha256,
                    unit.invocation,
                )
                await owned_thread(
                    self.store.save, replace(journal, operations=journal.operations + (record,))
                )
                task = asyncio.create_task(
                    self._perform(record, maintenance), name="web-daemon-restart"
                )
                maintenance = None
                self.tasks[operation_id] = task
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
                return record.as_dict()
            finally:
                if maintenance is not None:
                    maintenance.close()

    async def _save_result(self, record):
        await owned_thread(
            self.store.update,
            lambda journal: replace(
                journal,
                operations=tuple(
                    record if row.operation_id == record.operation_id else row
                    for row in journal.operations
                ),
            ),
        )

    async def _observed(self, record):
        selected = await owned_thread(self.selection.load)
        reference = ReleaseReference(record.release_id, record.manifest_sha256)
        if selected.revision != record.selection_revision or selected.active != reference:
            return replace(record, status="unknown", code="selection_changed")
        unit = await self._inspect()
        if not unit.active or not unit.invocation or unit.invocation == record.initial_invocation:
            return None
        status = await self.client.status()
        owner = await capture_owner(self.client)
        try:
            # Production clients always have a socket path. The PID proof ties
            # local health to the manager's new invocation, not an old daemon.
            if (
                owner is None
                or owner.pid != unit.pid
                or not local_release_healthy(status, reference)
            ):
                return None
        finally:
            if owner is not None:
                owner.close()
        return replace(record, status="completed", code="ok", final_invocation=unit.invocation)

    async def _perform(self, record, maintenance):
        try:
            # No automatic retry after this point, even if subprocess creation,
            # output collection, health verification, or durable acknowledgement fails.
            try:
                await self.command(UNIT_NAME, "restart")
                deadline = asyncio.get_running_loop().time() + HEALTH_SECONDS
                result = None
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        async with asyncio.timeout(
                            min(2, deadline - asyncio.get_running_loop().time())
                        ):
                            result = await self._observed(record)
                        if result is not None:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(
                        min(0.25, max(0, deadline - asyncio.get_running_loop().time()))
                    )
                result = result or replace(record, status="unknown", code="restart_unverified")
            except Exception:
                result = replace(record, status="unknown", code="service_failed")
            await self._save_result(result)
        finally:
            maintenance.close()

    async def get(self, operation_id):
        if not _uuid(operation_id):
            raise ActivationError("Restart identity is invalid", code="bad_parameters")
        journal = await owned_thread(self.store.load)
        record = next((row for row in journal.operations if row.operation_id == operation_id), None)
        if record is None:
            raise ActivationError("Restart operation is unknown", code="not_found")
        if record.status in {"pending", "unknown"} and (
            operation_id not in self.tasks or self.tasks[operation_id].done()
        ):
            try:
                maintenance = InstallLock(self.paths.state_directory, name="release-install.lock")
            except ActivationError as error:
                if error.code == "busy":
                    return record.as_dict()
                raise
            try:
                try:
                    async with asyncio.timeout(3):
                        observed = await self._observed(record)
                    if observed is not None:
                        record = observed
                    elif record.status == "pending":
                        record = replace(record, status="unknown", code="restart_unverified")
                except Exception:
                    if record.status == "pending":
                        record = replace(record, status="unknown", code="restart_unverified")
                await self._save_result(record)
            finally:
                maintenance.close()
        return record.as_dict()

    async def close(self):
        self.closing = True
        pending = [task for task in self.tasks.values() if not task.done()]
        if pending:
            _, remaining = await asyncio.wait(pending, timeout=85)
            if remaining:
                raise ActivationError("Restart retirement is incomplete", code="busy")
        for task in self.tasks.values():
            with contextlib.suppress(Exception):
                task.result()
