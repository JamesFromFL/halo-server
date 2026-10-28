"""Software publication readiness uses exact public events, separate from Apple qualification."""

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homekit_selection import selected_bridge

from halo_server.homekit.ipc import validate_direction


async def setup(tmp_path):
    bridge, boot, _, _, _, _ = await selected_bridge(tmp_path)
    bridge._publication_enabled = True
    inventory = bridge._backend.inventory()
    scope = {"profile_id": inventory.profile_id, "account_generation": inventory.account_generation}
    bridge._live.ready = bridge._live.audio_ready = bridge._snapshots.ready = True
    bridge._live.capabilities = SimpleNamespace(ready=True)
    bridge._runtime = SimpleNamespace(
        state="ready", boot_id=boot, bulk_channel=None, restart_count=0
    )
    bridge._config_generation = 1
    bridge._config_signature = (scope, bridge._registry.revision, [{"camera_id": "0"}], True)
    message = {
        "v": 1,
        "kind": "accessory_status",
        "boot_id": boot,
        "request_id": str(uuid4()),
        "payload": dict(
            scope=scope,
            revision=bridge._registry.revision,
            generation=1,
            configured=1,
            published=1,
            advertised=0,
        ),
    }
    return bridge, boot, message


@pytest.mark.asyncio
async def test_listening_alone_is_not_advertisement_and_neither_is_apple_qualification(tmp_path):
    bridge, boot, message = await setup(tmp_path)
    try:
        validate_direction(message, "hap")
        await bridge._notice(message)
        assert bridge.status()["publication_listening"] == 1
        assert bridge.status()["camera_ready"] is False
        message["payload"]["advertised"] = 1
        await bridge._notice(message)
        status = bridge.status()
        assert status["camera_ready"] is True
        assert status["real_ring_apple_qualified"] is False
        bridge._snapshots.ready = False
        assert bridge.status()["camera_ready"] is False
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["boot", "account", "revision", "generation", "incomplete"])
async def test_stale_or_incomplete_configuration_never_sets_readiness(tmp_path, change):
    bridge, boot, message = await setup(tmp_path)
    message["payload"]["advertised"] = 1
    try:
        if change == "boot":
            message["boot_id"] = str(uuid4())
        elif change == "account":
            message["payload"]["scope"]["account_generation"] += 1
        elif change == "revision":
            message["payload"]["revision"] += 1
        elif change == "generation":
            message["payload"]["generation"] += 1
        else:
            bridge._config_signature = None
        await bridge._notice(message)
        assert bridge.status()["camera_ready"] is False
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_retirement_zero_counts_and_storage_intent_clear_software_readiness(tmp_path):
    bridge, boot, message = await setup(tmp_path)
    message["payload"]["advertised"] = 1
    try:
        await bridge._notice(message)
        assert bridge.status()["camera_ready"]
        zero = copy.deepcopy(message)
        zero["payload"].update(configured=0, published=0, advertised=0)
        await bridge._notice(zero)
        assert not bridge.status()["camera_ready"]
        await bridge._notice(message)
        assert bridge.status()["camera_ready"]
        bridge._paths.homekit_pairing_directory.mkdir(mode=0o700, exist_ok=True)
        marker = bridge._paths.homekit_pairing_directory / ".hap-storage-intent"
        marker.touch(mode=0o600)
        assert bridge.status()["reason"] == "storage_recovery_required"
        assert not bridge.status()["camera_ready"]
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_new_generation_notice_cannot_acknowledge_old_completed_configuration(tmp_path):
    import asyncio

    bridge, boot, message = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    blocked = False

    async def request(kind, payload, *, expected):
        if blocked:
            entered.set()
            await release.wait()
        return {"accepted": True, "code": "ok"}

    bridge._runtime.request = request
    bridge._config_signature = None
    changing = None
    try:
        await bridge._configure()
        message["payload"].update(generation=bridge._config_generation, advertised=1)
        await bridge._notice(message)
        assert bridge.status()["camera_ready"]
        from dataclasses import replace

        bridge._audio_settings = replace(
            bridge._audio_settings, revision=bridge._audio_settings.revision + 1
        )
        blocked = True
        changing = asyncio.create_task(bridge._configure())
        await entered.wait()
        message["payload"]["generation"] = bridge._config_generation
        await bridge._notice(message)
        assert bridge._config_signature is None
        assert not bridge.status()["camera_ready"]
        release.set()
        await changing
        assert bridge.status()["camera_ready"]
    finally:
        release.set()
        if changing is not None:
            await changing
        await bridge._broker.retired(boot)
