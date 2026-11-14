"""Group publication is optional; permissions and aggregate observations stay separate."""

import asyncio
from dataclasses import replace

import pytest
from test_device_bridge_registry import PROFILE, configure, record
from test_device_light import command, light_source, policy_request
from test_device_monitor import fixture, preference

from halo_server.devices.bridge_registry import BridgeRegistryStore, bridge_projection
from halo_server.devices.light_policy import qualified_group, qualified_light
from halo_server.devices.models import DeviceIdentity, DeviceObservation


def group(profile=PROFILE):
    return record(
        profile=profile,
        identity=DeviceIdentity(profile, "ring-light-group", "location", "", "outside"),
        name="Outside lights",
        family="lighting-group",
        kind="light-group",
        model="Ring Light Group",
        capabilities=("switch",),
        capability_evidence=("ring-light-group-schema",),
        source_health="online",
        observations=(DeviceObservation("light_on", False, 100),),
    )


def test_groups_require_explicit_import_and_keep_identity_across_absence(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    sensor, aggregate = record(), group()
    first = configure(store, (sensor, aggregate))
    assert next(row for row in first.entries if row.device_id == sensor.key).selected
    before = next(row for row in first.entries if row.device_id == aggregate.key)
    assert before.mappings == ("lightbulb",) and not before.selected
    projection = bridge_projection(first, PROFILE, (sensor, aggregate))
    assert projection["overflow_count"] == 0
    assert next(row for row in projection["devices"] if row["id"] == aggregate.key)["reason"] == (
        "explicit-selection-required"
    )
    selected = configure(store, (sensor, aggregate), included_devices=(aggregate.key,))
    after = next(row for row in selected.entries if row.device_id == aggregate.key)
    assert after.selected and after.accessory_uuid == before.accessory_uuid
    assert store.reconcile(PROFILE, (sensor,)) == selected
    renamed = replace(aggregate, name="Renamed", provider_generation=8)
    assert store.reconcile(PROFILE, (renamed, sensor)) == selected
    assert BridgeRegistryStore(store.path).load() == selected
    excluded = configure(store, (sensor, aggregate), excluded_kinds=("light-group",))
    assert not next(row for row in excluded.entries if row.device_id == aggregate.key).selected


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "unqualified"},
        {"family": "hub"},
        {"supported": False},
        {"capability_evidence": ("ring-hub-device-schema",)},
        {"capabilities": ()},
        {"identity": DeviceIdentity(PROFILE, "ring-hub", "location", "", "outside")},
    ],
)
def test_group_lookalikes_never_gain_bridge_or_write_qualification(tmp_path, changes):
    row = replace(group(), **changes)
    assert not qualified_group(row) and not qualified_light(row)
    store = BridgeRegistryStore(tmp_path / "registry.json")
    saved = configure(store, (row,), included_devices=(row.key,))
    projection = bridge_projection(saved, PROFILE, (row,))["devices"][0]
    assert not projection["selected"] and projection["mappings"] == []


@pytest.mark.asyncio
async def test_group_uses_light_permission_without_inventing_member_state(tmp_path):
    _, client, sessions, monitor, providers = fixture(tmp_path)
    monitor.bridge.publisher.start = monitor.bridge.publisher.notify
    await monitor.set_enabled(**preference(sessions))
    row = group(sessions.active_scope.profile_id)
    calls = []

    async def write(device_id, target, **params):
        assert params["admission"]()
        calls.append((device_id, target, params["duration_seconds"]))
        return {"status": "confirmed", "code": "observed", "possibly_sent": True}

    provider = providers[0]
    provider.set_light = write
    try:
        provider.options["on_snapshot"](
            records=(row,),
            revision=1,
            complete=True,
            observed_at=100,
            lights=(light_source(row),),
        )
        await asyncio.sleep(0)
        initial = monitor.snapshot()["devices"][0]
        assert initial["provider"] == "ring-light-group"
        assert initial["light"]["on"] is False and not initial["light"]["can_set"]
        before = client.submissions
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        assert client.submissions == before
        assert monitor.snapshot()["devices"][0]["light"]["can_set"]
        result = await monitor.light.set_light(**command(sessions, monitor, row))
        assert result["status"] == "confirmed" and calls == [(row.key, True, 60)]
        # An acknowledgement is not a synthetic update, even for the aggregate.
        final = monitor.snapshot()["devices"][0]
        assert final["light"]["on"] is False
        assert "members" not in final and "member_states" not in final
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_group_hap_publication_requires_selection_and_separate_control_grant(tmp_path):
    from test_device_bridge import preferences
    from test_device_light_publication import payload
    from test_device_publication import ready

    _, _, sessions, monitor, providers, sensor = await ready(tmp_path)
    row = group(sessions.active_scope.profile_id)
    provider = providers[0]
    calls = []

    async def write(device_id, target, **params):
        assert params["admission"]()
        calls.append((device_id, target))
        return {"status": "confirmed", "code": "observed", "on": True, "possibly_sent": True}

    provider.set_light = write
    try:
        provider.options["on_snapshot"](
            records=(sensor, row),
            revision=2,
            complete=True,
            observed_at=100,
            lights=(light_source(row),),
        )
        await asyncio.sleep(0)
        if monitor.bridge._task:
            await monitor.bridge._task
        publisher = monitor.bridge.publisher
        async with publisher._lock:
            await publisher._step()
        assert all(item["device_id"] != row.key for item in publisher._last_snapshot["children"])
        await monitor.bridge.configure(
            **preferences(sessions, monitor.bridge.registry.revision, included_devices=[row.key])
        )
        async with publisher._lock:
            await publisher._step()
        child = next(
            item for item in publisher._last_snapshot["children"] if item["device_id"] == row.key
        )
        assert child["mappings"] == ["lightbulb"]
        assert child["light"] == {"on": False, "control_enabled": False}
        assert not (await publisher._light_set(payload(publisher, child), lambda: True))["accepted"]
        assert calls == []
        await monitor.light.configure(**policy_request(sessions, monitor, row))
        async with publisher._lock:
            await publisher._step()
        child = next(
            item for item in publisher._last_snapshot["children"] if item["device_id"] == row.key
        )
        assert child["light"] == {"on": False, "control_enabled": True}
        assert (await publisher._light_set(payload(publisher, child), lambda: True))["accepted"]
        assert calls == [(row.key, True)]
    finally:
        await monitor.close()
