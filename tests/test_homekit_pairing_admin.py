"""Actual owner client, parent reset journal, retained flock and Node cleanup."""

import asyncio
import json
import threading
from dataclasses import asdict
from uuid import uuid4

import pytest
from test_homekit_bridge import ADAPTER, NODE
from test_homekit_selection import selected_bridge

from halo_server.homekit.bridge import AdapterResources
from halo_server.homekit.models import HomeKitOperationError
from halo_server.homekit.pairing_maintenance import (
    PairingMaintenanceError,
    PairingReset,
    manifest_revision,
)
from halo_server.homekit.storage_owner import PairingDirectoryOwner
from halo_server.paths import ensure_private_directory
from halo_server.tui.client import AdminClient, AdminClientError


class Adapter:
    def __init__(self, bridge, boot, *, conflict=False):
        self.bridge, self.boot_id, self.conflict = bridge, boot, conflict
        self.state, self.restart_count, self.reason = "ready", 0, None
        self.bulk_channel = None
        ensure_private_directory(bridge._paths.homekit_pairing_directory)
        self._pairing_owner = PairingDirectoryOwner.acquire(bridge._paths.homekit_pairing_directory)
        self.manifest = dict.fromkeys(("accessory_info", "identifier_cache", "controller_storage"))
        self.revision = manifest_revision(self.manifest)
        self.requests = []
        self.reveal_gate = None

    @property
    def retirement_pending(self):
        return self.boot_id is not None or self._pairing_owner is not None

    async def request(self, kind, payload, *, expected):
        self.requests.append(kind)
        if kind == "pairing_status_request":
            return payload | {
                "accepted": True,
                "code": "ok",
                "published": True,
                "paired": False,
                "paired_clients": 0,
                "pairing_revision": self.revision,
                "pairing_manifest": self.manifest,
            }
        if kind == "pairing_reveal_request":
            if self.reveal_gate is not None:
                await self.reveal_gate.wait()
            return payload | {
                "accepted": True,
                "code": "ok",
                "pincode": "192-83-746",
                "setup_uri": "X-HM://0023ISYWYABCD",
                "ttl_ms": 60000,
            }
        assert kind == "pairing_reset_prepare" and expected == "pairing_reset_prepare_result"
        # Initial reset owns durable deny before asking the child to quiesce;
        # metadata scope stays exact, but no media operation can re-admit it.
        assert self.bridge._pairing_admin.store.load().entries[0].phase == "denied"
        scope = next(
            camera.scope
            for camera in self.bridge._broker.controller.inventory().cameras
            if camera.scope.camera_id == payload["scope"]["camera_id"]
        )
        assert asdict(scope) == payload["scope"]
        with pytest.raises(HomeKitOperationError):
            self.bridge._broker.controller.authorize(self.boot_id, scope, "streaming")
        return payload | {
            "accepted": not self.conflict,
            "code": "conflict" if self.conflict else "ok",
            "quiesced": True,
            "quiesced_revision": None if self.conflict else self.revision,
            "pairing_manifest": None if self.conflict else self.manifest,
        }

    async def stop(self):
        if self.boot_id is not None:
            await self.bridge._broker.retired(self.boot_id)
        self.boot_id, self.state = None, "stopped"
        if self._pairing_owner is not None:
            self._pairing_owner.close()
            self._pairing_owner = None


async def fixture(tmp_path, monkeypatch):
    bridge, boot, scope, service, client, sessions = await selected_bridge(tmp_path, count=2)
    runtime = Adapter(bridge, boot)
    bridge._runtime = runtime
    bridge._resources = AdapterResources(NODE, ADAPTER / "main.js")
    starts = []
    monkeypatch.setattr(bridge, "start", lambda: starts.append(True))
    admin = AdminClient(service.paths.admin_socket)
    await service._admin.start()
    return bridge, runtime, scope, service, admin, starts


@pytest.mark.asyncio
async def test_explicit_reveal_and_reset_through_real_owner_socket_keep_metadata_secret_free(
    tmp_path, monkeypatch
):
    bridge, runtime, _, service, client, starts = await fixture(tmp_path, monkeypatch)
    try:
        page = await client.homekit_pairing_list()
        row = page["cameras"][0]
        serialized = json.dumps(page)
        assert (
            "192-83-746" not in serialized
            and "X-HM://" not in serialized
            and "manifest" not in serialized
        )
        revealed = await client.homekit_pairing_reveal(page, row)
        assert revealed["pin"] == "192-83-746" and revealed["expires_in_ms"] == 60000
        before = bridge._registry
        result = await client.homekit_pairing_reset(page, row, str(uuid4()))
        assert result["status"] == "completed" and not result["restart_required"]
        assert starts == [True] and bridge._registry == before
        assert bridge._pairing_admin.journal.entries[0].phase == "completed"
        assert not bridge._pairing_admin.denied(*before.entries[0].key)
        assert runtime.boot_id is None and runtime._pairing_owner is None
        owner = PairingDirectoryOwner.acquire(service.paths.homekit_pairing_directory)
        owner.close()
    finally:
        await service._admin.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_prepare_conflict_reaps_quiesced_child_and_pending_operation_resumes(
    tmp_path, monkeypatch
):
    bridge, runtime, _, service, client, starts = await fixture(tmp_path, monkeypatch)
    try:
        page = await client.homekit_pairing_list()
        row, operation = page["cameras"][0], str(uuid4())
        runtime.conflict = True
        pending = await client.homekit_pairing_reset(page, row, operation)
        assert pending["status"] == "pending" and runtime.boot_id is None
        assert bridge._pairing_admin.denied(*bridge._registry.entries[0].key)
        refreshed = await client.homekit_pairing_list()
        target = refreshed["cameras"][0]
        assert target["pending_reset"] and target["reset_operation_id"] == operation
        assert (
            target["pairing_revision"] == row["pairing_revision"] and refreshed["boot_id"] is None
        )
        assert (await client.homekit_pairing_reset(refreshed, target, operation))[
            "status"
        ] == "completed"
        assert starts == [True, True]
    finally:
        await service._admin.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_denied_crash_recovery_reaps_new_other_camera_graph_before_exact_cleanup(
    tmp_path, monkeypatch
):
    bridge, runtime, _, service, client, starts = await fixture(tmp_path, monkeypatch)
    try:
        identity = bridge._registry.entries[0]
        row = PairingReset(
            str(uuid4()),
            identity.profile_id,
            identity.camera_id,
            identity.accessory_uuid,
            identity.username,
            runtime.revision,
            tuple(sorted(runtime.manifest.items())),
        )
        bridge._pairing_admin.store.begin(row)
        await runtime.stop()
        # Simulate a new boot after the durable deny survived a process exit.
        await bridge._load_registry()
        boot = str(uuid4())
        await bridge._broker.ready(boot)
        runtime = Adapter(bridge, boot)
        bridge._runtime = runtime
        assert [
            camera.scope.camera_id for camera in bridge._broker.controller.inventory().cameras
        ] == ["1"]
        page = await client.homekit_pairing_list()
        target = page["cameras"][0]
        assert target["pending_reset"] and target["reset_operation_id"] == row.operation_id
        assert (await client.homekit_pairing_reset(page, target, row.operation_id))[
            "status"
        ] == "completed"
        assert "pairing_reset_prepare" not in runtime.requests and runtime.boot_id is None
        assert starts == [True]
    finally:
        await service._admin.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_reset_disconnect_joins_durable_deny_and_offline_cleanup(tmp_path, monkeypatch):
    bridge, runtime, scope, service, client, _ = await fixture(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = bridge._pairing_admin.store.begin

    def blocked(row):
        entered.set()
        assert release.wait(3)
        return original(row)

    monkeypatch.setattr(bridge._pairing_admin.store, "begin", blocked)
    try:
        page = await client.homekit_pairing_list()
        row = page["cameras"][0]
        params = {
            "profile_id": page["profile_id"],
            "account_generation": page["account_generation"],
            "camera_id": row["camera_id"],
            "accessory_id": row["accessory_id"],
            "expected_registry_revision": page["registry_revision"],
            "expected_pairing_revision": row["pairing_revision"],
            "boot_id": page["boot_id"],
            "operation_id": str(uuid4()),
        }
        resetting = asyncio.create_task(bridge.owner_pairing("homekit_pairing_reset", params))
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(HomeKitOperationError):
            service.homekit.authorize(runtime.boot_id, scope, "streaming")
        resetting.cancel()
        await asyncio.sleep(0)
        assert not resetting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await resetting
        assert bridge._pairing_admin.journal.entries[0].phase == "completed"
        assert runtime.boot_id is None
    finally:
        release.set()
        await service._admin.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_reveal_late_account_change_never_returns_obsolete_secret(tmp_path, monkeypatch):
    bridge, runtime, _, service, client, _ = await fixture(tmp_path, monkeypatch)
    try:
        page = await client.homekit_pairing_list()
        runtime.reveal_gate = asyncio.Event()
        revealing = asyncio.create_task(client.homekit_pairing_reveal(page, page["cameras"][0]))
        while "pairing_reveal_request" not in runtime.requests:
            await asyncio.sleep(0)
        service.homekit.fence()
        runtime.reveal_gate.set()
        with pytest.raises(AdminClientError) as error:
            await revealing
        assert error.value.code == "account_changed"
    finally:
        await service._admin.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_uncertain_completed_journal_reload_recovers_without_permanent_local_deny(
    tmp_path, monkeypatch
):
    bridge, runtime, _, service, client, starts = await fixture(tmp_path, monkeypatch)
    original = bridge._pairing_admin.store.advance

    def uncertain(operation, phase):
        saved = original(operation, phase)
        if phase == "completed":
            raise PairingMaintenanceError("Injected post-replace failure", possibly_saved=True)
        return saved

    monkeypatch.setattr(bridge._pairing_admin.store, "advance", uncertain)
    try:
        page = await client.homekit_pairing_list()
        result = await client.homekit_pairing_reset(page, page["cameras"][0], str(uuid4()))
        assert result["status"] == "completed" and starts == [True]
        assert not bridge._pairing_admin.denied(*bridge._registry.entries[0].key)
        assert runtime.boot_id is None and bridge._pairing_admin.owner is None
    finally:
        await service._admin.stop()
        await bridge.stop()
