"""Pairing maintenance deny journal, namespace CAS and actual offline helper."""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homekit_bridge import ADAPTER, NODE
from test_homekit_controller import POLICY, configured
from test_homekit_registry import PROFILE

from halo_server.homekit.models import HomeKitOperationError
from halo_server.homekit.pairing_maintenance import (
    MaintenanceOwner,
    PairingMaintenanceConflict,
    PairingMaintenanceError,
    PairingMaintenanceStore,
    PairingReset,
    manifest_revision,
)
from halo_server.paths import ensure_private_directory

USERNAME = "02:00:00:00:00:01"


def record(manifest=None):
    manifest = manifest or dict.fromkeys(
        ("accessory_info", "identifier_cache", "controller_storage")
    )
    return PairingReset(
        str(uuid4()),
        PROFILE,
        "one",
        str(uuid4()),
        USERNAME,
        manifest_revision(manifest),
        tuple(sorted(manifest.items())),
    )


def test_durable_deny_is_resumable_and_phases_cannot_skip_or_reassociate(tmp_path):
    store = PairingMaintenanceStore(tmp_path / "maintenance.json")
    row = record()
    denied = store.begin(row)
    assert PairingMaintenanceStore(store.path).load() == denied
    assert store.begin(row) == denied
    with pytest.raises(PairingMaintenanceConflict):
        store.advance(row.operation_id, "completed")
    with pytest.raises(PairingMaintenanceConflict):
        store.begin(replace(row, operation_id=str(uuid4())))
    for phase in ("quiesced", "cleanup_started", "completed"):
        saved = store.advance(row.operation_id, phase)
        assert saved.entries[0].phase == phase
    assert store.begin(row) == saved
    with pytest.raises(PairingMaintenanceConflict):
        store.begin(replace(row, accessory_id=str(uuid4()), operation_id=str(uuid4())))
    store.path.unlink()
    with pytest.raises(PairingMaintenanceError, match="missing"):
        store.load()


@pytest.mark.asyncio
async def test_quiesce_preserves_child_scope_but_permanently_denies_this_boot():
    controller, backend, boot, scopes = await configured(2)
    scope, other = scopes
    live = await controller.open_live(boot, scope)
    controller.quiesce_camera(boot, scope)
    assert controller.inventory().cameras[0].scope == scope
    assert not live.is_current()
    for operation in (
        lambda: controller.authorize(boot, scope, "streaming"),
        lambda: controller.validate_selected_scope(boot, scope),
    ):
        with pytest.raises(HomeKitOperationError) as error:
            operation()
        assert error.value.code == "maintenance_pending"
    with pytest.raises(HomeKitOperationError):
        await controller.update_policy(boot, scope, POLICY)
    controller.validate_selected_scope(boot, other)
    await controller.detach()
    await controller.attach(boot, tuple(camera.scope for camera in backend.cameras))
    with pytest.raises(HomeKitOperationError):
        controller.validate_selected_scope(boot, controller.inventory().cameras[0].scope)
    await controller.detach()
    new_boot = str(uuid4())
    await controller.attach(new_boot, tuple(camera.scope for camera in backend.cameras))
    controller.validate_selected_scope(new_boot, controller.inventory().cameras[0].scope)
    await controller.detach()


def write(path, value):
    payload = json.dumps(value, separators=(",", ":")).encode()
    path.write_bytes(payload)
    path.chmod(0o600)
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_actual_helper_exact_namespace_cas_and_partial_resume(tmp_path):
    pairings = tmp_path / "private" / "pairings"
    ensure_private_directory(pairings)
    target = pairings / "IdentifierCache.020000000001.json"
    other = pairings / "IdentifierCache.020000000002.json"
    digest = write(target, {"cache": {"key": 2}})
    public = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    write(
        pairings / "AccessoryInfo.020000000002.json",
        {
            "displayName": "Fixture",
            "category": 18,
            "pincode": "192-83-746",
            "signSk": "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60" + public,
            "signPk": public,
            "pairedClients": {},
            "pairedClientsPermission": {},
            "configVersion": 1,
            "configHash": "",
            "setupID": "ABCD",
            "lastFirmwareVersion": "2.2.3",
        },
    )
    other_bytes = b'{"cache":{"other":3}}'
    other.write_bytes(other_bytes)
    other.chmod(0o600)
    manifest = {"accessory_info": None, "identifier_cache": digest, "controller_storage": None}
    row = record(manifest)
    owner = MaintenanceOwner.capture(None, pairings)
    resources = SimpleNamespace(node=NODE, entry=ADAPTER / "main.js")
    try:
        write(target, {"cache": {"changed": 4}})
        with pytest.raises(PairingMaintenanceConflict):
            await owner.cleanup(resources, row, resume=False)
        assert target.exists() and other.read_bytes() == other_bytes
        target.unlink()
        with pytest.raises(PairingMaintenanceConflict):
            await owner.cleanup(resources, row, resume=False)
        await owner.cleanup(resources, row, resume=True)
        assert not target.exists() and other.read_bytes() == other_bytes
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_helper_cancel_during_spawn_retains_fds_until_actual_child_reaped(
    tmp_path, monkeypatch
):
    pairings = tmp_path / "private" / "pairings"
    ensure_private_directory(pairings)
    owner = MaintenanceOwner.capture(None, pairings)
    original = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    cleanup = asyncio.create_task(
        owner.cleanup(SimpleNamespace(node=NODE, entry=ADAPTER / "main.js"), record(), resume=False)
    )
    try:
        await entered.wait()
        cleanup.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done()
        with pytest.raises(PairingMaintenanceError, match="still owned"):
            owner.close()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert owner.process is None and owner._task.done()
    finally:
        release.set()
        await asyncio.gather(cleanup, return_exceptions=True)
        owner.close()


@pytest.mark.asyncio
async def test_helper_rejects_valid_prefix_followed_by_fragmented_extra_output(tmp_path):
    pairings = tmp_path / "pairings"
    ensure_private_directory(pairings)
    helper = tmp_path / "pairing-reset.js"
    helper.write_text("""
let body='';process.stdin.on('data',chunk=>body+=chunk);
process.stdin.on('end',()=>{
 const v=JSON.parse(body);
 const result={v:v.v,operation_id:v.operation_id,accessory_id:v.accessory_id,
 username:v.username,pairing_revision:v.pairing_revision,accepted:true,code:'ok'};
 process.stdout.write(JSON.stringify(result)+'\\n');
 setTimeout(()=>process.stdout.write('unexpected-extra-output\\n'),30);
});
""")
    owner = MaintenanceOwner.capture(None, pairings)
    try:
        with pytest.raises(PairingMaintenanceError):
            await owner.cleanup(
                SimpleNamespace(node=NODE, entry=tmp_path / "main.js"), record(), resume=False
            )
        assert owner.process is None
    finally:
        owner.close()
