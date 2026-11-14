"""Composed bridge preparation without camera readiness or Ring writes."""

import asyncio
import json
import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from test_device_monitor import fixture, preference, record

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.bridge import DeviceBridge
from halo_server.devices.bridge_registry import BridgeRegistryError
from halo_server.owner_state import MAX_OWNER_STATE_BYTES


def preferences(sessions, revision=0, **changes):
    return {
        "profile_id": sessions.active_scope.profile_id,
        "account_generation": sessions.active_scope.generation,
        "expected_revision": revision,
        "enabled": True,
        "auto_import": True,
        "included_devices": [],
        "excluded_devices": [],
        "excluded_locations": [],
        "excluded_kinds": [],
        "excluded_mappings": [],
        **changes,
    }


def sensor(sessions, **changes):
    return replace(record(sessions, **changes), capability_evidence=("ring-hub-device-schema",))


async def publish(monitor, provider, records, **kwargs):
    provider.publish(records, **kwargs)
    await asyncio.sleep(0)
    if monitor.bridge._task is not None:
        await monitor.bridge._task


@pytest.mark.asyncio
async def test_unconfigured_inventory_no_disk_then_atomic_prepare_and_exclude(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    service._account_integrations_ready = False
    service._media_scope = None
    await monitor.set_enabled(**preference(sessions))
    row = sensor(sessions)
    await publish(monitor, providers[0], (row,))
    view = monitor.snapshot()["bridge"]
    assert not view["configured"] and not view["enabled"]
    assert not monitor.bridge.store.path.exists()
    result = await service.handle_admin("devices_bridge_set", preferences(sessions))
    assert result["selected_count"] == 1 and result["publication"] == "not_implemented"
    view = monitor.snapshot()["bridge"]
    assert view["devices"][0]["selected"]
    identity = view["devices"][0]["accessory_uuid"]
    result = await service.handle_admin(
        "devices_bridge_set",
        preferences(sessions, result["revision"], excluded_devices=[row.key]),
    )
    view = monitor.snapshot()["bridge"]
    assert result["selected_count"] == 0 and view["devices"][0]["excluded"]
    assert view["devices"][0]["accessory_uuid"] == identity
    assert len(providers) == 1 and len(client.all_devices) == 1
    assert monitor.snapshot()["safety"]["panic_enabled"] is False
    await monitor.close()


@pytest.mark.asyncio
async def test_account_switch_hides_previous_bridge_and_never_reuses_identity(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    await publish(monitor, providers[0], (sensor(sessions),))
    result = await service.handle_admin("devices_bridge_set", preferences(sessions))
    before = monitor.snapshot()["bridge"]
    old_scope = sessions.active_scope
    sessions.active_scope = AccountScope(str(uuid4()), 2)
    assert not monitor.snapshot()["bridge"]["configured"]
    assert monitor.snapshot()["bridge"]["devices"] == []
    with pytest.raises(AdminProtocolError) as error:
        await monitor.bridge.configure(
            **(preferences(sessions) | {"profile_id": old_scope.profile_id})
        )
    assert error.value.code == "account_changed"
    await monitor.reconcile()
    await publish(monitor, providers[1], (sensor(sessions, generation=2),))
    await monitor.bridge.configure(**preferences(sessions, result["revision"]))
    after = monitor.snapshot()["bridge"]
    assert after["accessory_uuid"] != before["accessory_uuid"]
    assert after["username"] != before["username"]
    sessions.active_scope = AccountScope(old_scope.profile_id, 3)
    restored = monitor.snapshot()["bridge"]
    assert restored["accessory_uuid"] == before["accessory_uuid"]
    assert restored["devices"][0]["accessory_uuid"] == before["devices"][0]["accessory_uuid"]
    assert not restored["devices"][0]["present"]
    await monitor.close()


@pytest.mark.asyncio
async def test_event_observations_do_not_write_identity_registry(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    row = sensor(sessions)
    await publish(monitor, providers[0], (row,))
    await monitor.bridge.configure(**preferences(sessions))
    body = monitor.bridge.store.path.read_bytes()
    monitor.bridge.store.reconcile = lambda *_a, **_k: pytest.fail("observation triggered disk IO")
    for revision in range(2, 6):
        observed = replace(row.observations[0], value=bool(revision % 2))
        await publish(
            monitor, providers[0], (replace(row, observations=(observed,)),), revision=revision
        )
    assert monitor.bridge.store.path.read_bytes() == body
    await monitor.close()


@pytest.mark.asyncio
async def test_metadata_discovery_persists_new_children_and_missing_reserves_identity(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    await monitor.bridge.configure(**preferences(sessions))
    rows = (sensor(sessions), sensor(sessions, identifier="second"))
    await publish(monitor, providers[0], rows)
    before = monitor.snapshot()["bridge"]
    assert before["selected_count"] == 2
    await publish(monitor, providers[0], (), revision=2)
    assert monitor.snapshot()["bridge"]["selected_count"] == 2
    await monitor.set_enabled(**preference(sessions, revision=1, enabled=False))
    restarted = DeviceBridge(service)
    await restarted.reconcile()
    after = restarted.snapshot()
    assert after["selected_count"] == 2
    assert [row["accessory_uuid"] for row in after["devices"]] == [
        row["accessory_uuid"] for row in before["devices"]
    ]
    assert all(not row["present"] for row in after["devices"])
    await monitor.close()
    await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"panic_enabled": True},
        {"enabled": "true"},
        {"account_generation": True},
        {"excluded_devices": ["secret-name"]},
        {"excluded_mappings": ["panic"]},
        {"excluded_kinds": ["unknown\ncontrol"]},
        {"included_devices": ["a" * 64] * 2},
        {"included_devices": ["a" * 64], "excluded_devices": ["a" * 64]},
    ],
)
async def test_invalid_or_dangerous_preferences_rejected_before_storage(tmp_path, changes):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    with pytest.raises(AdminProtocolError) as error:
        await service.handle_admin("devices_bridge_set", preferences(sessions, **changes))
    assert error.value.code == "bad_params"
    assert not monitor.bridge.store.path.exists() and not providers


@pytest.mark.asyncio
async def test_revision_conflict_refreshes_saved_preferences(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    first = await monitor.bridge.configure(**preferences(sessions))
    monitor.bridge.store.configure(
        sessions.active_scope.profile_id,
        expected_revision=first["revision"],
        enabled=False,
        auto_import=False,
    )
    with pytest.raises(AdminProtocolError) as error:
        await monitor.bridge.configure(**preferences(sessions, first["revision"]))
    assert error.value.code == "configuration_conflict"
    assert not monitor.bridge.snapshot()["enabled"]
    assert monitor.bridge.snapshot()["status"] == "ready"


@pytest.mark.asyncio
async def test_uncertain_save_hides_cached_selection_until_reloaded(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    first = await monitor.bridge.configure(**preferences(sessions))
    save = monitor.bridge.store.configure

    def uncertain(*args, **kwargs):
        save(*args, **kwargs)
        raise BridgeRegistryError("Fixture sync failed", possibly_saved=True)

    monitor.bridge.store.configure = uncertain
    with pytest.raises(AdminProtocolError) as error:
        await monitor.bridge.configure(**preferences(sessions, first["revision"], enabled=False))
    assert error.value.code == "device_bridge_unavailable"
    assert monitor.bridge.snapshot()["status"] == "error"
    assert not monitor.bridge.snapshot()["enabled"]
    await monitor.bridge.reconcile()
    assert monitor.bridge.snapshot()["status"] == "ready"
    assert not monitor.bridge.snapshot()["enabled"]


@pytest.mark.asyncio
async def test_disconnect_joins_preference_write_and_audits_without_household_names(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()
    save = monitor.bridge.store.configure

    def blocked_save(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return save(*args, **kwargs)

    monitor.bridge.store.configure = blocked_save
    request = asyncio.create_task(service.handle_admin("devices_bridge_set", preferences(sessions)))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    finally:
        release.set()
        await asyncio.gather(*service._owner_tasks)
    assert monitor.bridge.snapshot()["configured"]
    assert any(
        row["action"] == "devices_bridge_set"
        for row in service._owner_activity.snapshot()["entries"]
    )


@pytest.mark.asyncio
async def test_bad_registry_does_not_disable_camera_or_device_monitoring(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    path = monitor.bridge.store.path
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text('{"version":999}')
    path.chmod(0o600)
    await monitor.reconcile()
    await monitor.set_enabled(**preference(sessions))
    await publish(monitor, providers[0], (sensor(sessions),))
    state = monitor.snapshot()
    assert state["bridge"]["status"] == "error"
    assert state["state"] == "ready" and state["devices"]
    assert client.is_authenticated and len(client.all_devices) == 1
    await monitor.close()


@pytest.mark.asyncio
async def test_large_registry_projection_keeps_complete_preferences_and_wire_budget(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    rows = tuple(
        sensor(sessions, identifier=str(index), name="\ufffd" * 256) for index in range(600)
    )
    await publish(monitor, providers[0], rows)
    excluded = sorted(row.key for row in rows[:512])
    await monitor.bridge.configure(**preferences(sessions, excluded_devices=excluded))
    result = await service.handle_admin("owner_state", {})
    view = result["noncamera"]["bridge"]
    assert view["excluded_devices"] == excluded
    assert view["retained_count"] == 600 and view["selected_count"] == 88
    assert view["truncated"]
    assert (
        len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode())
        <= MAX_OWNER_STATE_BYTES
    )
    await monitor.close()


@pytest.mark.asyncio
async def test_new_catalog_classification_cannot_break_camera_owner_state(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    future = replace(sensor(sessions), kind="future.sensor", supported=False)
    await publish(monitor, providers[0], (future,))
    result = await service.handle_admin("owner_state", {})
    assert result["noncamera"]["bridge"]["status"] == "error"
    assert result["noncamera"]["devices"][0]["kind"] == "future.sensor"
    assert client.is_authenticated and len(client.all_devices) == 1
    await monitor.close()
