"""Boot-scoped native operations between the HAP child and Python resource owners."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4

from halo_server.homekit.controller import HomeKitController
from halo_server.homekit.ipc import (
    MAX_INVENTORY_PAGE,
    HomeKitProtocolError,
    encode_frame,
    validate_direction,
)
from halo_server.homekit.models import CameraPolicy, HomeKitOperationError, MediaScope

MAX_CURSOR_PAGES = 4
CURSOR_SECONDS = 10.0
MAX_WRITE_OPERATIONS = 4096
MAX_SESSION_IDENTITIES = 512
RETIRED_SESSION_SECONDS = 60.0


@dataclass
class _Resource:
    scope: MediaScope
    kind: str
    handle: Any
    deadline: float


class HomeKitBroker:
    """Own private leases, with no accessory publication or implicit selection.

    Selected scopes come from trusted Python configuration (S07), never a child
    inventory query. S06 defaults to no selected cameras. Tests inject explicitly
    selected synthetic devices without changing that production default.
    """

    def __init__(
        self,
        controller: HomeKitController,
        backend,
        *,
        selection: Callable = lambda: (),
        changed: Callable[[], None] = lambda: None,
    ) -> None:
        self.controller, self.backend = controller, backend
        self._selection, self._changed = selection, changed
        self.runtime = None
        self.boot_id: str | None = None
        self._account = None
        self._selection_keys = ()
        self._retiring_boot = None
        self._resources: dict[str, _Resource] = {}
        self._session_ids: dict[str, float | None] = {}
        self._pages: OrderedDict[str, tuple[Any, int, float]] = OrderedDict()
        self._writes: set[str] = set()
        self._reaper: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()
        self.cleanup_failed = False

    async def ready(self, boot_id: str) -> None:
        async with self._refresh_lock:
            if (
                self.boot_id is not None
                or self._retiring_boot is not None
                or self._resources
                or self.cleanup_failed
            ):
                raise HomeKitOperationError("cleanup_pending")
            self.boot_id = boot_id
            selection = tuple(self._selection())
            await self.controller.attach(boot_id, selection)
            self._selection_keys = tuple(scope.source_key for scope in selection)
            if self.boot_id != boot_id:
                raise HomeKitOperationError("boot_changed")
            self._account = _account(self.backend.inventory())
            self._reaper = asyncio.create_task(self._expire(), name="homekit-lease-expiry")
            await self._refresh_locked()

    async def refresh(self) -> None:
        async with self._refresh_lock:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        boot = self.boot_id
        if boot is None or self.runtime is None:
            return
        account = _account(self.backend.inventory())
        selection = tuple(self._selection())
        keys = tuple(scope.source_key for scope in selection)
        if (
            account != self._account
            or keys != self._selection_keys
            or self.controller.boot_id != boot
        ):
            self.controller.fence()
            await self.controller.detach()
            if self.boot_id != boot:
                return
            await self._close_resources()
            if self.boot_id != boot:
                return
            await self.controller.attach(boot, selection)
            self._selection_keys = keys
            if self.boot_id != boot:
                return
            self._account = account
        inventory = await self.controller.reconcile()
        self._pages.clear()
        if self.boot_id == boot:
            await self.runtime.send(
                "inventory_reset",
                {
                    "scope": _account(inventory),
                    "revision": inventory.revision,
                },
            )

    async def retired(self, boot_id: str) -> None:
        if boot_id not in (self.boot_id, self._retiring_boot):
            return
        self._retiring_boot = boot_id
        self.boot_id = None  # Fence before waiting for a concurrent refresh.
        async with self._refresh_lock:
            if self._retiring_boot != boot_id or self.boot_id is not None:
                return
            self._pages.clear()
            reaper, self._reaper = self._reaper, None
            if reaper is not None:
                reaper.cancel()
                await asyncio.gather(reaper, return_exceptions=True)
            failures = []
            for operation in (self._close_resources(), self.controller.detach()):
                try:
                    await operation
                except Exception as exc:
                    failures.append(exc)
            self._writes.clear()
            self._account = None
            self.cleanup_failed = bool(failures)
            if failures:
                raise ExceptionGroup("HomeKit resource retirement incomplete", failures)
            self._retiring_boot = None
            self._session_ids.clear()

    async def _close_resources(self) -> None:
        failures = []
        for identity, resource in tuple(self._resources.items()):
            try:
                await resource.handle.close()
            except Exception as exc:
                failures.append(exc)
            else:
                self._forget_resource(identity, resource)
        if failures:
            self.cleanup_failed = True
            raise ExceptionGroup("Native resource cleanup failed", failures)

    async def _expire(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.1)
                now = asyncio.get_running_loop().time()
                current = {camera.scope for camera in self.controller.inventory().cameras}
                for identity, resource in tuple(self._resources.items()):
                    if now >= resource.deadline or resource.scope not in current:
                        try:
                            await resource.handle.close()
                        except Exception:
                            self.cleanup_failed = True
                            # Retain the claim and terminate this boot; never spin
                            # a failed close or reopen a source behind it.
                            self._changed()
                            return
                        self._forget_resource(identity, resource)
        except asyncio.CancelledError:
            raise

    def _require_boot(self, envelope: dict) -> str:
        validate_direction(envelope, "hap")
        if self.boot_id is None or envelope["boot_id"] != self.boot_id:
            raise HomeKitProtocolError("retired broker boot")
        if self.cleanup_failed:
            raise HomeKitProtocolError("broker cleanup is incomplete")
        return self.boot_id

    async def request(self, envelope: dict) -> dict:
        boot = self._require_boot(envelope)
        kind, data = envelope["kind"], envelope["payload"]
        if kind == "inventory_request":
            try:
                return self._inventory(envelope)
            except HomeKitOperationError as exc:
                return {**envelope, "kind": "error", "payload": {"code": _code(exc), "message": ""}}
        if kind == "policy_update":
            try:
                await self.controller.update_policy(
                    boot, _scope(data), CameraPolicy(**data["policy"])
                )
            except HomeKitOperationError as exc:
                return {"scope": data["scope"], "accepted": False, "code": _code(exc)}
            self._pages.clear()
            return {"scope": data["scope"], "accepted": True, "code": "ok"}
        if kind in {"lease_request", "lease_renew", "lease_stop", "talkback_request"}:
            return await self._resource_request(boot, kind, data)
        if kind == "snapshot_request":
            # S10 must decode/resize in bounded isolated work before this can
            # promise the requested image dimensions or return a transfer ID.
            return {
                "scope": data["scope"],
                "job_id": data["job_id"],
                "status": "rejected",
                "code": "not-ready",
                "transfer_id": None,
            }
        if kind == "control_request":
            result = {"scope": data["scope"], "operation_id": data["operation_id"]}
            if data["operation_id"] in self._writes:
                return result | {"status": "rejected", "code": "conflict"}
            if len(self._writes) >= MAX_WRITE_OPERATIONS:
                return result | {"status": "rejected", "code": "busy"}
            self._writes.add(data["operation_id"])
            try:
                await self.controller.set_control(
                    boot, _scope(data), data["control"], data["enabled"]
                )
            except HomeKitOperationError as exc:
                return result | {
                    "status": "unknown" if exc.possibly_sent else "rejected",
                    "code": _code(exc),
                }
            return result | {"status": "accepted", "code": "ok"}
        raise HomeKitProtocolError("unsupported child request")

    def _inventory(self, envelope: dict) -> dict:
        data = envelope["payload"]
        current = self.controller.inventory()
        if data["scope"] != _account(current):
            raise HomeKitOperationError("stale_scope")
        now = asyncio.get_running_loop().time()
        for key, (_, _, deadline) in tuple(self._pages.items()):
            if deadline <= now:
                self._pages.pop(key)
        cursor = data["cursor"]
        if cursor is None:
            inventory, offset = current, 0
            if data["revision"] not in (0, inventory.revision):
                raise HomeKitOperationError("stale_scope")
        else:
            stored = self._pages.pop(cursor, None)
            if stored is None:
                raise HomeKitOperationError("stale_scope")
            inventory, offset, _ = stored
            if inventory != current or data["revision"] != inventory.revision:
                raise HomeKitOperationError("stale_scope")
        continuation = str(uuid4())
        payload = {
            "scope": data["scope"],
            "revision": inventory.revision,
            "cursor": cursor,
            "next_cursor": None,
            "total": len(inventory.cameras),
            "complete": inventory.complete,
            "devices": [],
        }
        end = offset
        for camera in inventory.cameras[offset : offset + MAX_INVENTORY_PAGE]:
            candidate = payload | {
                "devices": payload["devices"] + [_device_dict(camera)],
                "next_cursor": continuation if end + 1 < len(inventory.cameras) else None,
            }
            try:
                encode_frame({**envelope, "kind": "inventory_page", "payload": candidate})
            except HomeKitProtocolError:
                if not payload["devices"]:
                    raise
                break
            payload = candidate
            end += 1
        if end < len(inventory.cameras):
            payload["next_cursor"] = continuation
            while len(self._pages) >= MAX_CURSOR_PAGES:
                self._pages.popitem(last=False)
            self._pages[continuation] = inventory, end, now + CURSOR_SECONDS
        return payload

    async def _resource_request(self, boot, kind, data) -> dict:
        identity, scope = data["session_id"], _scope(data)
        result = {"scope": data["scope"], "session_id": identity}
        speaker = kind == "talkback_request"
        stopping = kind == "lease_stop" or speaker and data["action"] == "stop"
        resource = self._resources.get(identity)
        ttl = data.get("ttl_ms", 30000)
        code = "ok"
        try:
            if resource is not None and (
                resource.scope != scope or resource.kind != ("speaker" if speaker else "live")
            ):
                raise HomeKitOperationError("session_conflict")
            if stopping:
                if resource is None and self._session_ids.get(identity, 0) is None:
                    raise HomeKitOperationError("busy")
                if resource is not None:
                    await resource.handle.close()
                    self._forget_resource(identity, resource)
                status = "stopped"
            elif kind == "lease_renew":
                if resource is None or not self._current(resource):
                    raise HomeKitOperationError("expired")
                if not await resource.handle.renew():
                    raise HomeKitOperationError("expired")
                resource.deadline = asyncio.get_running_loop().time() + ttl / 1000
                status = "accepted"
            else:
                if resource is None:
                    now = asyncio.get_running_loop().time()
                    self._session_ids = {
                        key: deadline
                        for key, deadline in self._session_ids.items()
                        if deadline is None or deadline > now
                    }
                    if identity in self._session_ids:
                        raise HomeKitOperationError("session_conflict")
                    if len(self._session_ids) >= MAX_SESSION_IDENTITIES:
                        raise HomeKitOperationError("busy")
                    method = self.controller.open_speaker if speaker else self.controller.open_live
                    self._session_ids[identity] = None  # Reserve before acquisition yields.
                    try:
                        handle = await method(boot, scope, session_id=identity)
                    except BaseException:
                        self._session_ids[identity] = (
                            asyncio.get_running_loop().time() + RETIRED_SESSION_SECONDS
                        )
                        raise
                    resource = _Resource(
                        scope,
                        "speaker" if speaker else "live",
                        handle,
                        asyncio.get_running_loop().time() + ttl / 1000,
                    )
                    self._resources[identity] = resource
                    self._session_ids[identity] = None
                    if self.boot_id != boot or not self._current(resource):
                        await resource.handle.close()
                        self._forget_resource(identity, resource)
                        raise HomeKitOperationError("stale_scope")
                elif not self._current(resource):
                    raise HomeKitOperationError("expired")
                if not speaker:
                    ttl = min(
                        ttl, int((resource.deadline - asyncio.get_running_loop().time()) * 1000)
                    )
                    if ttl <= 0:
                        raise HomeKitOperationError("expired")
                status = "accepted"
        except HomeKitOperationError as exc:
            code, status = _code(exc), "rejected"
        if kind in {"lease_request", "lease_renew"}:
            result["ttl_ms"] = ttl if status == "accepted" else 0
        return result | {"status": status, "code": code}

    async def notice(self, envelope: dict) -> None:
        self._require_boot(envelope)
        if envelope["kind"] == "snapshot_cancel":
            return  # No S06 image worker/job exists to cancel.
        if envelope["kind"] != "transfer_open":
            raise HomeKitProtocolError("unsupported child notice")
        data = envelope["payload"]
        resource = self._resources.get(data["operation_id"])
        if (
            data["kind"] != "talkback_pcm"
            or data["total_bytes"] != 1920
            or data["chunk_bytes"] != 1920
            or resource is None
            or resource.kind != "speaker"
            or resource.scope != _scope(data)
            or not self._current(resource)
        ):
            raise HomeKitProtocolError("unowned talkback transfer")
        channel = self.runtime.bulk_channel
        if channel is None:
            raise HomeKitProtocolError("bulk unavailable")
        channel.authorize_receive(
            scope=data["scope"],
            operation_id=data["operation_id"],
            transfer_id=data["transfer_id"],
            kind="talkback_pcm",
            total_bytes=1920,
            sink=resource.handle.write,
            valid=lambda: self._current(resource),
        )

    def _forget_resource(self, identity: str, resource: _Resource) -> None:
        if self._resources.get(identity) is resource:
            self._resources.pop(identity)
            # Longer than either control deadline: bound late/replayed traffic
            # without an ever-growing history or a lifetime streaming limit.
            self._session_ids[identity] = (
                asyncio.get_running_loop().time() + RETIRED_SESSION_SECONDS
            )

    def _current(self, resource: _Resource) -> bool:
        return (
            self.boot_id is not None
            and not self.cleanup_failed
            and any(owned is resource for owned in self._resources.values())
            and asyncio.get_running_loop().time() < resource.deadline
            and any(
                camera.scope == resource.scope for camera in self.controller.inventory().cameras
            )
        )


def _account(inventory):
    return (
        None
        if inventory.profile_id is None
        else {
            "profile_id": inventory.profile_id,
            "account_generation": inventory.account_generation,
        }
    )


def _scope(data: dict) -> MediaScope:
    return MediaScope(**data["scope"])


def _code(error: HomeKitOperationError) -> str:
    if error.code in {"busy", "timeout", "expired", "cancelled", "unsupported", "unauthorized"}:
        return error.code
    if error.code in {"scope_changed", "stale_scope", "boot_changed", "authority_changed"}:
        return "stale-scope"
    if error.code in {"policy_denied"}:
        return "policy-denied"
    if error.code in {"session_conflict"}:
        return "conflict"
    if error.code in {"capability_unsupported", "snapshot_unsupported", "intercom_unsupported"}:
        return "unsupported"
    if error.code in {"cleanup_failed", "cleanup_pending", "closed"}:
        return "not-ready"
    return "source-failed"


def _device_dict(camera):
    value = asdict(camera)
    value["capabilities"]["evidence"] = list(value["capabilities"]["evidence"])
    return value
