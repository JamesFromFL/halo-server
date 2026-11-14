"""Actual isolated device reset helper: exact namespace, CAS, and owned locks."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homekit_bridge import ADAPTER, NODE

from halo_server.devices.publication import DeviceMaintenanceOwner
from halo_server.devices.publication_policy import BridgeReset
from halo_server.homekit.pairing_maintenance import (
    PairingMaintenanceConflict,
    PairingMaintenanceError,
    manifest_revision,
)
from halo_server.homekit.storage_owner import PairingDirectoryOwner, PairingStorageError
from halo_server.paths import ensure_private_directory

USERNAME = "02:11:22:33:44:55"
KINDS = {
    "accessory_info": "AccessoryInfo",
    "identifier_cache": "IdentifierCache",
    "controller_storage": "ControllerStorage",
}


def write(path, value):
    payload = json.dumps(value, separators=(",", ":")).encode()
    path.write_bytes(payload)
    path.chmod(0o600)
    return hashlib.sha256(payload).hexdigest()


def accessory_info(name):
    public = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    return {
        "displayName": name,
        "category": 2,
        "pincode": "192-83-746",
        "signSk": "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60" + public,
        "signPk": public,
        "pairedClients": {"fixture-controller": "cc" * 32},
        "pairedClientsPermission": {"fixture-controller": 1},
        "configVersion": 1,
        "configHash": "",
        "setupID": "ABCD",
        "lastFirmwareVersion": "2.3.0",
    }


def namespace(directory, username, name):
    suffix = username.replace(":", "")
    values = {
        "accessory_info": accessory_info(name),
        "identifier_cache": {"cache": {"|nextAID": 3, f"{uuid4()}|AID": 2}},
        "controller_storage": {"accessories": {}},
    }
    return {
        field: write(directory / f"{kind}.{suffix}.json", values[field])
        for field, kind in KINDS.items()
    }


def fixtures(tmp_path):
    pairings = tmp_path / "homekit" / "devices" / "pairings"
    camera = tmp_path / "homekit" / "pairings"
    ensure_private_directory(pairings)
    ensure_private_directory(camera)
    manifest = namespace(pairings, USERNAME, "Halo Bridge")
    namespace(pairings, "02:11:22:33:44:66", "Another Account Bridge")
    namespace(camera, "02:11:22:33:44:77", "Camera")
    protected = {
        path: path.read_bytes()
        for directory in (pairings, camera)
        for path in directory.iterdir()
        if "021122334455" not in path.name
    }
    row = BridgeReset(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        USERNAME,
        manifest_revision(manifest),
        tuple(sorted(manifest.items())),
        phase="cleanup_started",
    )
    resources = SimpleNamespace(node=NODE, entry=ADAPTER / "device-main.js")
    return pairings, row, protected, resources


def assert_protected(protected):
    assert all(path.read_bytes() == data for path, data in protected.items())


@pytest.mark.asyncio
async def test_actual_device_helper_removes_only_target_and_preserves_camera_and_other_account(
    tmp_path, monkeypatch
):
    pairings, row, protected, resources = fixtures(tmp_path)
    owner = DeviceMaintenanceOwner.capture(None, pairings)
    original = asyncio.create_subprocess_exec
    commands = []

    async def record(*args, **kwargs):
        commands.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    try:
        await owner.cleanup(resources, row, resume=False)
        assert len(commands) == 1
        assert Path(commands[0][0][1]).name == "device-pairing-reset.js"
        assert commands[0][1]["pass_fds"][:2] == (owner.directory_fd, owner.lock_fd)
        assert not list(pairings.glob("*.021122334455.json"))
        assert_protected(protected)
        assert owner.process is None
        assert not (pairings / ".hap-storage-intent").exists()
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_device_helper_stale_manifest_refuses_all_deletions_and_resume_is_exact(tmp_path):
    pairings, row, protected, resources = fixtures(tmp_path)
    owner = DeviceMaintenanceOwner.capture(None, pairings)
    target = pairings / "IdentifierCache.021122334455.json"
    original = target.read_bytes()
    try:
        write(target, {"cache": {"changed": 4}})
        changed = {path: path.read_bytes() for path in pairings.iterdir()}
        with pytest.raises(PairingMaintenanceConflict):
            await owner.cleanup(resources, row, resume=False)
        assert all(path.read_bytes() == data for path, data in changed.items())
        with pytest.raises(PairingMaintenanceConflict):
            await owner.cleanup(resources, row, resume=True)
        assert all(path.read_bytes() == data for path, data in changed.items())
        target.write_bytes(original)
        target.unlink()
        with pytest.raises(PairingMaintenanceConflict):
            await owner.cleanup(resources, row, resume=False)
        assert (pairings / "AccessoryInfo.021122334455.json").exists()
        await owner.cleanup(resources, row, resume=True)
        assert not list(pairings.glob("*.021122334455.json"))
        assert_protected(protected)
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_device_helper_inherits_exclusive_lock_until_actual_reap_on_cancel(
    tmp_path, monkeypatch
):
    pairings, row, protected, resources = fixtures(tmp_path)
    active = PairingDirectoryOwner.acquire(pairings)
    owner = DeviceMaintenanceOwner.capture(SimpleNamespace(_pairing_owner=active), pairings)
    active.close()
    entered, release = asyncio.Event(), asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    cleanup = asyncio.create_task(owner.cleanup(resources, row, resume=False))
    try:
        await entered.wait()
        cleanup.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done()
        with pytest.raises(PairingMaintenanceError, match="still owned"):
            owner.close()
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(pairings)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert owner.process is None
        assert_protected(protected)
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(pairings)
    finally:
        release.set()
        await asyncio.gather(cleanup, return_exceptions=True)
        owner.close()
    replacement = PairingDirectoryOwner.acquire(pairings)
    replacement.close()


@pytest.mark.asyncio
async def test_device_helper_preserves_unresolved_storage_intent_and_every_pairing(tmp_path):
    pairings, row, protected, resources = fixtures(tmp_path)
    marker = pairings / ".hap-storage-intent"
    marker.write_bytes(b"unresolved fixture transaction")
    marker.chmod(0o600)
    before = {path: path.read_bytes() for path in pairings.iterdir()}
    owner = DeviceMaintenanceOwner.capture(None, pairings)
    try:
        with pytest.raises(PairingMaintenanceError):
            await owner.cleanup(resources, row, resume=True)
        assert all(path.read_bytes() == data for path, data in before.items())
        assert_protected(protected)
        assert owner.process is None
    finally:
        owner.close()
