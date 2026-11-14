"""Parent owner pairing metadata, explicit secret reveal and reset maintenance."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict

from halo_server.api.admin import AdminProtocolError
from halo_server.homekit.pairing_maintenance import (
    DIGEST,
    MaintenanceOwner,
    PairingJournal,
    PairingMaintenanceError,
    PairingMaintenanceStore,
    PairingReset,
    manifest_revision,
    valid_uuid,
)
from halo_server.media.manager import owned_thread, settle_owned


class PairingAdministration:
    def __init__(self, bridge, paths):
        self.bridge, self.paths = bridge, paths
        self.store = (
            PairingMaintenanceStore(paths.homekit_pairing_maintenance_file) if paths else None
        )
        self.journal = PairingJournal()
        self.available = paths is None
        self.lock = asyncio.Lock()
        self.local_denied = set()
        self.owner = None
        self.tasks = set()

    def denied(self, profile, camera):
        return (
            not self.available
            or (profile, camera) in self.local_denied
            or any(
                row.key == (profile, camera) and row.phase != "completed"
                for row in self.journal.entries
            )
        )

    async def load(self):
        if self.store is None:
            return
        try:
            self.journal = await owned_thread(self.store.load)
        except PairingMaintenanceError:
            self.available = False
            self.bridge._broker.controller.fence()
            raise
        self.available = True

    async def handle(self, method, params):
        if method == "homekit_pairing_list":
            return await self.list(params)
        if method == "homekit_pairing_reveal":
            return await self.reveal(params)
        if method == "homekit_pairing_reset":
            task = asyncio.create_task(self.reset(params), name="owner-pairing-reset")
            self.tasks.add(task)
            task.add_done_callback(self._task_done)
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await settle_owned(task)
                raise
        raise AdminProtocolError("unknown_method", "Unknown pairing method")

    def _task_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def close(self):
        if self.tasks:
            _, pending = await asyncio.wait(tuple(self.tasks), timeout=20)
            if pending:
                raise PairingMaintenanceError("Pairing owner operation retirement is pending")
        if self.owner is not None:
            if self.owner.process is not None:
                process = self.owner.process
                if process.returncode is None:
                    process.kill()
                    async with asyncio.timeout(5):
                        await process.wait()
                self.owner.process = None
            self.owner.close()
            self.owner = None

    async def list(self, params):
        _only(params, {"offset", "limit"})
        offset, limit = params.get("offset", 0), params.get("limit", 32)
        if (
            type(offset) is not int
            or not 0 <= offset <= 256
            or type(limit) is not int
            or not 1 <= limit <= 32
        ):
            raise AdminProtocolError("bad_params", "Pairing page bounds are invalid")
        registry, inventory = await self.bridge.cameras()
        runtime = self.bridge._runtime
        boot = runtime.boot_id if runtime is not None else None
        ready = (
            self.available and runtime is not None and runtime.state == "ready" and boot is not None
        )
        current = {camera.scope.camera_id: camera for camera in inventory.cameras}
        rows = [row for row in registry.entries if row.profile_id == inventory.profile_id]
        result = []
        semaphore = asyncio.Semaphore(4)
        selected_scopes = {
            (item.scope.profile_id, item.scope.camera_id): item.scope
            for item in self.bridge._broker.controller.inventory().cameras
        }

        async def inspect(row):
            scope = selected_scopes.get(row.key)
            if not ready or scope is None or self.denied(*row.key):
                return None
            try:
                async with semaphore:
                    return await runtime.request(
                        "pairing_status_request",
                        _identity(row, scope, registry.revision),
                        expected="pairing_status_result",
                    )
            except Exception:
                return None

        try:
            async with asyncio.timeout(25):
                statuses = await asyncio.gather(
                    *(inspect(row) for row in rows[offset : offset + limit])
                )
        except TimeoutError:
            statuses = [None] * len(rows[offset : offset + limit])
        for row, status in zip(rows[offset : offset + limit], statuses, strict=True):
            camera = current.get(row.camera_id)
            known = status is not None and status.get("accepted") is True
            pending = next(
                (
                    item
                    for item in self.journal.entries
                    if item.key == row.key and item.phase != "completed"
                ),
                None,
            )
            result.append(
                {
                    "camera_id": row.camera_id,
                    "accessory_id": row.accessory_uuid,
                    "username": row.username,
                    "name": camera.name if camera else "",
                    "selected": row.selected,
                    "available": camera is not None,
                    "device_generation": camera.scope.device_generation if camera else None,
                    "published": bool(status and status.get("published")),
                    "paired_known": known,
                    "paired": status["paired"] if known else False,
                    "paired_clients": status["paired_clients"] if known else 0,
                    "pairing_revision": status["pairing_revision"]
                    if known
                    else pending.pairing_revision
                    if pending
                    else None,
                    "pending_reset": pending is not None,
                    "reset_operation_id": pending.operation_id if pending else None,
                }
            )
        if (
            runtime is not self.bridge._runtime
            or (runtime is not None and runtime.boot_id != boot)
            or registry != self.bridge._registry
            or (
                self.bridge._backend.inventory().profile_id,
                self.bridge._backend.inventory().account_generation,
            )
            != (inventory.profile_id, inventory.account_generation)
        ):
            raise AdminProtocolError(
                "account_changed", "Pairing authority changed; reload metadata"
            )
        return {
            "profile_id": inventory.profile_id,
            "account_generation": inventory.account_generation,
            "registry_revision": registry.revision,
            "boot_id": boot,
            "available": ready,
            "total": len(rows),
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "cameras": result,
        }

    async def _target(self, params, *, reset=False):
        allowed = {
            "profile_id",
            "account_generation",
            "camera_id",
            "accessory_id",
            "expected_registry_revision",
            "expected_pairing_revision",
            "boot_id",
        } | ({"operation_id"} if reset else set())
        _only(params, allowed)
        if (
            set(params) != allowed
            or type(params["account_generation"]) is not int
            or not 1 <= params["account_generation"] < 2**53
            or type(params["expected_registry_revision"]) is not int
            or not 0 <= params["expected_registry_revision"] < 2**53
            or not isinstance(params["expected_pairing_revision"], str)
            or DIGEST.fullmatch(params["expected_pairing_revision"]) is None
            or (params["boot_id"] is not None and not valid_uuid(params["boot_id"]))
            or (reset and not valid_uuid(params["operation_id"]))
        ):
            raise AdminProtocolError("bad_params", "Pairing identity parameters are invalid")
        registry, inventory = await self.bridge.cameras()
        if (inventory.profile_id, inventory.account_generation) != (
            params["profile_id"],
            params["account_generation"],
        ):
            raise AdminProtocolError("account_changed", "Pairing account changed; reload metadata")
        row = next(
            (
                row
                for row in registry.entries
                if row.profile_id == params["profile_id"]
                and row.camera_id == params["camera_id"]
                and row.accessory_uuid == params["accessory_id"]
            ),
            None,
        )
        if row is None or registry.revision != params["expected_registry_revision"]:
            raise AdminProtocolError("configuration_conflict", "Pairing accessory identity changed")
        runtime = self.bridge._runtime
        if params["boot_id"] != (runtime.boot_id if runtime is not None else None):
            raise AdminProtocolError("account_changed", "Pairing adapter changed; reload metadata")
        scope = next(
            (
                item.scope
                for item in self.bridge._broker.controller.inventory().cameras
                if item.scope.profile_id == row.profile_id and item.scope.camera_id == row.camera_id
            ),
            None,
        )
        return row, scope, runtime, registry

    async def reveal(self, params):
        async with self.lock:
            row, scope, runtime, registry = await self._target(params)
            if (
                self.denied(*row.key)
                or not row.selected
                or scope is None
                or runtime is None
                or runtime.state != "ready"
            ):
                raise AdminProtocolError(
                    "unavailable", "Pairing reveal requires a current published camera"
                )
            data = _identity(row, scope, registry.revision) | {
                "pairing_revision": params["expected_pairing_revision"]
            }
            result = await runtime.request(
                "pairing_reveal_request", data, expected="pairing_reveal_result"
            )
            if not result["accepted"]:
                if result["code"] == "policy-denied":
                    # The child withholds setup credentials once paired, and
                    # also when no setup PIN is available. Neither condition
                    # is a stale revision or permission to reset the accessory.
                    raise AdminProtocolError(
                        "pairing_not_available", "Pairing code is unavailable for this camera"
                    )
                if result["code"] == "conflict":
                    raise AdminProtocolError(
                        "configuration_conflict", "Pairing state changed; reload metadata"
                    )
                raise AdminProtocolError("unavailable", "HomeKit pairing is not ready")
            # A pairing/account/selection update while the request awaited must
            # not disclose an obsolete secret to the owner UI.
            try:
                self.bridge._broker.controller.validate_selected_scope(params["boot_id"], scope)
            except Exception as error:
                raise AdminProtocolError("account_changed", "Pairing authority changed") from error
            if (
                registry != self.bridge._registry
                or self.denied(*row.key)
                or runtime.boot_id != params["boot_id"]
            ):
                raise AdminProtocolError("account_changed", "Pairing authority changed")
            return {
                key: params[key]
                for key in (
                    "profile_id",
                    "account_generation",
                    "camera_id",
                    "accessory_id",
                    "boot_id",
                )
            } | {
                "registry_revision": registry.revision,
                "pairing_revision": result["pairing_revision"],
                "pin": result["pincode"],
                "setup_uri": result["setup_uri"],
                "expires_in_ms": result["ttl_ms"],
            }

    async def reset(self, params):
        async with self.lock, self.bridge._inventory_apply_lock:
            row, scope, runtime, registry = await self._target(params, reset=True)
            if not self.available or self.store is None:
                raise AdminProtocolError(
                    "configuration_unavailable", "Pairing maintenance state is unavailable"
                )
            old = next(
                (
                    item
                    for item in self.journal.entries
                    if item.operation_id == params["operation_id"]
                ),
                None,
            )
            if old is not None and (
                old.key != row.key
                or old.accessory_id != row.accessory_uuid
                or old.username != row.username
                or old.pairing_revision != params["expected_pairing_revision"]
            ):
                raise AdminProtocolError(
                    "operation_conflict", "Pairing reset operation identity changed"
                )
            new_operation = old is None
            if old is not None and old.phase == "completed":
                self.local_denied.discard(row.key)
                return _reset_result(old, True)
            if old is None:
                if (
                    self.denied(*row.key)
                    or scope is None
                    or not row.selected
                    or runtime is None
                    or runtime.state != "ready"
                ):
                    raise AdminProtocolError(
                        "unavailable",
                        "Pairing reset requires current metadata or its pending operation",
                    )
                status = await runtime.request(
                    "pairing_status_request",
                    _identity(row, scope, registry.revision),
                    expected="pairing_status_result",
                )
                if (
                    not status["accepted"]
                    or status["pairing_revision"] != params["expected_pairing_revision"]
                    or manifest_revision(status["pairing_manifest"]) != status["pairing_revision"]
                ):
                    raise AdminProtocolError(
                        "configuration_conflict", "Pairing state changed; reload metadata"
                    )
                current = self.bridge._backend.inventory()
                if (
                    registry != self.bridge._registry
                    or (current.profile_id, current.account_generation)
                    != (params["profile_id"], params["account_generation"])
                    or runtime is not self.bridge._runtime
                    or runtime.boot_id != params["boot_id"]
                ):
                    raise AdminProtocolError(
                        "account_changed", "Pairing authority changed during reset review"
                    )
                old = PairingReset(
                    params["operation_id"],
                    row.profile_id,
                    row.camera_id,
                    row.accessory_uuid,
                    row.username,
                    status["pairing_revision"],
                    tuple(sorted(status["pairing_manifest"].items())),
                )
                # Immediate source delivery fence precedes disk work. Durable
                # deny survives any subsequent failure or process restart.
                self.local_denied.add(row.key)
                self.bridge._broker.controller.quiesce_camera(params["boot_id"], scope)
                try:
                    self.journal = await owned_thread(self.store.begin, old)
                except PairingMaintenanceError:
                    with contextlib.suppress(PairingMaintenanceError):
                        await self.load()
                    raise AdminProtocolError(
                        "configuration_unavailable", "Pairing reset deny could not be saved safely"
                    ) from None
            try:
                if self.owner is None:
                    self.owner = MaintenanceOwner.capture(
                        runtime, self.paths.homekit_pairing_directory
                    )
                if new_operation and runtime is not None and runtime.boot_id is not None:
                    if scope is None or runtime.state != "ready":
                        raise PairingMaintenanceError("Pairing adapter cannot prove quiescence")
                    prepared = await runtime.request(
                        "pairing_reset_prepare",
                        _identity(row, scope, registry.revision)
                        | {"pairing_revision": old.pairing_revision},
                        expected="pairing_reset_prepare_result",
                    )
                    if (
                        not prepared["accepted"]
                        or not prepared["quiesced"]
                        or prepared["quiesced_revision"] != old.pairing_revision
                        or tuple(sorted(prepared["pairing_manifest"].items())) != old.manifest
                    ):
                        raise PairingMaintenanceError("Pairing reset quiescence conflicted")
                await self.bridge.stop()
                if runtime is not None and (
                    runtime.boot_id is not None or runtime.retirement_pending
                ):
                    raise PairingMaintenanceError("Pairing adapter retirement is incomplete")
                if old.phase == "denied":
                    self.journal = await owned_thread(
                        self.store.advance, old.operation_id, "quiesced"
                    )
                    old = next(
                        item
                        for item in self.journal.entries
                        if item.operation_id == old.operation_id
                    )
                resume = old.phase == "cleanup_started"
                if old.phase == "quiesced":
                    self.journal = await owned_thread(
                        self.store.advance, old.operation_id, "cleanup_started"
                    )
                    old = next(
                        item
                        for item in self.journal.entries
                        if item.operation_id == old.operation_id
                    )
                resources = self.bridge._resources
                if resources is None:
                    from halo_server.homekit.bridge import development_resources

                    resources = development_resources()
                await self.owner.cleanup(resources, old, resume=resume)
                self.journal = await owned_thread(self.store.advance, old.operation_id, "completed")
                self.local_denied.discard(row.key)
                self.owner.close()
                self.owner = None
                if not self.bridge._backend._service._stop_requested.is_set():
                    self.bridge.start()
                return _reset_result(old, True)
            except Exception:
                # prepare_reset permanently quiesces its child even on a
                # fingerprint conflict. Reap it before considering recovery.
                with contextlib.suppress(Exception):
                    await self.bridge.stop()
                with contextlib.suppress(PairingMaintenanceError):
                    await self.load()
                completed = self.available and any(
                    item.operation_id == old.operation_id and item.phase == "completed"
                    for item in self.journal.entries
                )
                if completed:
                    self.local_denied.discard(row.key)
                # Keep the journal and exact owner when cleanup is uncertain.
                # A retry of this operation resumes, never selects another HAP
                # namespace or admits the camera before deletion is proven.
                if self.owner is not None and self.owner.process is None:
                    self.owner.close()
                    self.owner = None
                runtime = self.bridge._runtime
                if (
                    self.owner is None
                    and (runtime is None or not runtime.retirement_pending)
                    and not self.bridge._backend._service._stop_requested.is_set()
                ):
                    with contextlib.suppress(Exception):
                        self.bridge.start()
                return _reset_result(old, completed)


def _only(params, allowed):
    if not isinstance(params, dict) or set(params) - allowed:
        raise AdminProtocolError("bad_params", "Pairing parameters are invalid")


def _identity(row, scope, revision):
    return {
        "scope": asdict(scope),
        "registry_revision": revision,
        "accessory_id": row.accessory_uuid,
        "username": row.username,
    }


def _reset_result(row, complete):
    return {
        "operation_id": row.operation_id,
        "status": "completed" if complete else "pending",
        "code": "ok" if complete else "cleanup_pending",
        "restart_required": not complete,
    }
