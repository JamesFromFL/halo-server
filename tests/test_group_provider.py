"""Composed group discovery and control; only synthetic Ring fixtures are used."""

import asyncio
import copy
import json
from dataclasses import replace

import pytest
from test_device_monitor import fixture as monitor_fixture
from test_device_monitor import preference
from test_light_device_commands import Transport as BeamsTransport
from test_light_device_commands import light
from test_lighting_groups import Gateway as GroupGateway
from test_lighting_groups import args, inventory, report
from test_noncamera_provider import Gateway, Transport
from test_ring_shared_auth import Response, fake_auth

from halo_server.devices.models import DeviceIdentity
from halo_server.devices.provider import NoncameraProvider, _group_context
from halo_server.ring.control_routes import group_url
from halo_server.ring.hub.protocol import DeviceDocument, DeviceInfoDocList, DeviceInfoDocUpdate
from halo_server.ring.hub.rest import (
    DEVICES_ENDPOINT,
    LOCATIONS_ENDPOINT,
    AlarmRestError,
    RingAlarmRestGateway,
)
from halo_server.ring.lighting import LightingReservation, client_lighting

PROFILE = "11111111-1111-4111-8111-111111111111"


def camera(**patch):
    return {"id": 7, "kind": "floodlight_v2", "location_id": "home", "owned": True, **patch}


class Groups(GroupGateway):
    def __init__(self):
        super().__init__()
        self.closed = False
        self.list_calls = []
        self.report_calls = []
        self.list_entered = asyncio.Event()
        self.list_release = None
        self.close_check = None

    async def read_light_groups(self, location, **kwargs):
        self.list_calls.append(location)
        self.list_entered.set()
        frozen = copy.deepcopy(self.inventory)
        if self.list_release:
            await self.list_release.wait()
        if isinstance(frozen, Exception):
            raise frozen
        return frozen

    async def read_light_group_report(self, location, group, **kwargs):
        self.report_calls.append((location, group))
        return await super().read_light_group_report(location, group, **kwargs)

    async def close(self):
        if self.close_check:
            self.close_check()
        self.closed = True


def make(*, devices=None, transport=Transport, reservation=None):
    gateway = Gateway(devices if devices is not None else {"stickup_cams": [camera()]})
    batches, clock = [], [100.0]
    provider = NoncameraProvider(
        None,
        profile_id=PROFILE,
        account_generation=1,
        gateway=gateway,
        transport_factory=transport,
        on_snapshot=lambda **batch: batches.append(batch),
        clock=lambda: clock[0],
        monotonic_clock=lambda: clock[0],
        lighting_reservation=reservation,
        command_timeout=0.05,
    )
    groups = Groups()
    provider._device_gateway = groups
    return provider, gateway, groups, batches, clock


async def populate(provider):
    await provider.refresh()
    for runtime in provider._runtimes.values():
        await runtime.transport.ready.wait()
    await provider._refresh_groups()


@pytest.mark.asyncio
async def test_camera_only_account_discovers_canonical_group_and_routes_confirmed_command():
    provider, gateway, groups, batches, _ = make()
    try:
        await populate(provider)
        assert not provider._runtimes
        assert batches[-1]["complete"] is True
        records = provider.snapshot()
        assert len(records) == 1 and records[0].kind == "light-group"
        assert records[0].identity.provider == "ring-light-group"
        assert batches[-1]["lights"][0]["authorization"] == "allowed"
        assert groups.list_calls == ["home"] and not groups.calls
        result = await provider.set_light(**args(provider._groups))
        assert result["status"] == "confirmed" and result["on"] is True
        assert groups.calls == [("home", "garden", True, 60)]
        assert batches[-1]["lights"][0]["on"] is True
    finally:
        await provider.close()
    assert groups.closed and gateway.closed


@pytest.mark.parametrize(
    "rows,allowed",
    [
        ({"stickup_cams": [camera()]}, True),
        ({"stickup_cams": [camera(id="7")]}, True),
        ({"stickup_cams": [camera(owned=False)]}, False),
        ({"stickup_cams": [camera(owned=1)]}, False),
        ({"stickup_cams": [camera(kind="floodlight_v3")]}, False),
        ({"stickup_cams": [camera()], "doorbots": [camera()]}, False),
        ({"stickup_cams": [camera()], "doorbots": [camera(kind="other")]}, False),
        ({"beams_bridges": [camera(role="owner")]}, False),
        (
            {"beams": [{"id": 7, "kind": "floodlight_v2", "location_id": "home", "role": "owner"}]},
            True,
        ),
        ({"beams": [camera(owned=False, role="owner")]}, False),
    ],
)
def test_owner_proof_joins_exact_unique_physical_row(rows, allowed):
    _, ownership = _group_context(rows, {"user_locations": [{"location_id": "home"}]})
    assert (ownership.get(("home", "7", "floodlight_v2")) is True) is allowed


@pytest.mark.parametrize("value", [True, 0, -1, "007", "7.0", " 7", 2**63])
def test_invalid_camera_identity_cannot_prove_complete_member_ownership(value):
    with pytest.raises(ValueError):
        _group_context(
            {"stickup_cams": [camera(id=value)]},
            {"user_locations": [{"location_id": "home"}]},
        )


@pytest.mark.parametrize(
    "metadata",
    [{"has_more": True}, {"complete": False}, {"pagination": {"next": 2}}, {"count": True}],
)
def test_partial_location_inventory_is_not_removal_or_owner_proof(metadata):
    with pytest.raises(ValueError):
        _group_context(
            {"stickup_cams": [camera()]},
            {"user_locations": [{"location_id": "home"}], **metadata},
        )


@pytest.mark.asyncio
async def test_fresh_rest_owner_revocation_takes_effect_without_group_rediscovery():
    provider, gateway, groups, batches, _ = make()
    try:
        await populate(provider)
        old = args(provider._groups)
        gateway.devices["stickup_cams"][0]["owned"] = False
        await provider.refresh()
        assert batches[-1]["lights"][0]["authorization"] == "unknown"
        assert not batches[-1]["lights"][0]["can_set"]
        assert (await provider.set_light(**old))["status"] == "rejected"
        assert groups.list_calls == ["home"] and not groups.calls
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_rest_failure_revokes_group_before_publishing_and_recovery_refreshes_inventory():
    provider, gateway, groups, batches, _ = make()
    try:
        await populate(provider)
        gateway.error = AlarmRestError("network-unavailable")
        await provider.refresh()
        assert batches[-1]["lights"][0]["on"] is None
        assert not batches[-1]["lights"][0]["can_set"]
        gateway.error = None
        await provider.refresh()
        assert provider._group_discovery_at == 0
        await provider._refresh_groups()
        assert groups.list_calls == ["home", "home"]
        assert batches[-1]["lights"][0]["can_set"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_crossed_group_inventory_cannot_restore_revoked_owner_context():
    provider, gateway, groups, batches, clock = make()
    try:
        await populate(provider)
        groups.list_release = asyncio.Event()
        groups.list_entered.clear()
        clock[0] += 301
        pending = asyncio.create_task(provider._refresh_groups())
        await groups.list_entered.wait()
        gateway.devices["stickup_cams"][0]["owned"] = False
        await provider.refresh()
        groups.list_release.set()
        await pending
        assert batches[-1]["lights"][0]["authorization"] == "unknown"
        assert not batches[-1]["lights"][0]["can_set"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_discovery_and_report_schedules_are_independent_of_rest_receipts():
    provider, _, groups, _, clock = make()
    try:
        await populate(provider)
        for _ in range(3):
            clock[0] += 5
            await provider.refresh()
            await provider._refresh_groups()
        assert len(groups.list_calls) == len(groups.report_calls) == 1
        clock[0] = 131
        await provider._refresh_groups()
        assert len(groups.list_calls) == 1 and len(groups.report_calls) == 2
        clock[0] = 401
        await provider._refresh_groups()
        assert len(groups.list_calls) == 2 and len(groups.report_calls) == 3
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_background_group_get_never_blocks_other_rest_discovery_and_closes_before_gateway():
    provider, gateway, groups, _, _ = make()
    groups.list_release = asyncio.Event()
    try:
        await provider.start()
        await asyncio.wait_for(groups.list_entered.wait(), 1)
        await asyncio.wait_for(provider.refresh(), 0.1)
        assert len(gateway.calls) >= 4
        groups.close_check = lambda: (
            pytest.fail("Group task still running at gateway close")
            if not provider._group_task.done() or not provider._groups._closed
            else None
        )
    finally:
        await provider.close()
    assert groups.closed and gateway.closed


@pytest.mark.asyncio
async def test_removed_group_is_explicitly_unavailable_when_hub_is_partial():
    provider, _, groups, batches, clock = make(
        devices={
            "stickup_cams": [camera()],
            "base_stations": [{"id": 8, "location_id": "home", "owned": True}],
        }
    )
    try:
        await populate(provider)
        old = next(record for record in provider.snapshot() if record.kind == "light-group")
        runtime = provider._runtimes["home"]
        provider._store.mark_location_stale("home", runtime.epoch)
        groups.inventory = {"device_groups": []}
        clock[0] += 301
        await provider._refresh_groups()
        assert batches[-1]["complete"] is False
        retired = next(record for record in provider.snapshot() if record.key == old.key)
        assert retired.source_health == "unavailable"
        assert retired.observations[0].status == "stale"
        assert not batches[-1]["lights"]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group_id,suppressed",
    [
        ("garden", True),
        ("other", False),
        (None, False),
        ("garden\u200b", False),
        ("x" * 129, False),
        (7, False),
    ],
)
async def test_clap_alias_requires_exact_bounded_group_id_and_location(group_id, suppressed):
    provider, _, _, _, _ = make(
        devices={
            "stickup_cams": [camera()],
            "beams_bridges": [{"id": 8, "location_id": "home", "role": "owner"}],
        }
    )
    try:
        await populate(provider)
        runtime = provider._runtimes["home"]
        await runtime.transport.message(
            DeviceInfoDocList(
                runtime.transport.assets[0].asset_id,
                (
                    DeviceDocument(
                        "alias",
                        {
                            "zid": "alias",
                            "deviceType": "group.light-group.beams",
                            "groupId": group_id,
                            "on": False,
                            "commStatus": "ok",
                        },
                    ),
                ),
            )
        )
        assert (
            any(record.model == "group.light-group.beams" for record in provider.snapshot())
        ) is not suppressed
        actual = provider._store.snapshot.find_location("home").devices[0]
        assert actual.group_id == (
            group_id
            if isinstance(group_id, str) and len(group_id) <= 128 and "\u200b" not in group_id
            else None
        )
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_shared_reservation_blocks_group_and_physical_beams_overlap():
    reservation = LightingReservation()
    provider, _, groups, _, _ = make(
        devices={
            "stickup_cams": [camera()],
            "beams_bridges": [{"id": 8, "location_id": "home", "role": "owner"}],
        },
        transport=BeamsTransport,
        reservation=reservation,
    )
    try:
        # The ticket identity must match this exact owned bridge row.
        await provider.refresh()
        runtime = provider._runtimes["home"]
        runtime.transport.assets = (replace(runtime.transport.assets[0], doorbot_id=8),)
        await runtime.transport.ready.wait()
        await provider._refresh_groups()
        assert provider._groups.reservation is provider._light_commands.reservation is reservation
        token = reservation.try_acquire()  # same primitive used by camera controls
        result = await provider.set_light(**args(provider._groups))
        assert result["status"] == "busy" and not groups.calls
        reservation.release(token)
        groups.behavior = "blocked"
        command = asyncio.create_task(provider.set_light(**args(provider._groups)))
        await groups.write_entered.wait()
        # The group handoff invalidated the lamp's cached On evidence. A fresh
        # physical report restores that evidence, but the shared writer is busy.
        await runtime.transport.message(DeviceInfoDocUpdate("asset", (light(False),)))
        state = provider._light_commands.snapshots()[0]
        result = await provider.set_light(
            state["device_id"],
            True,
            expected_revision=state["revision"],
            expected_epoch=state["epoch"],
            duration_seconds=60,
            admission=lambda: True,
        )
        assert result["status"] == "busy" and not runtime.transport.sent
        groups.write_release.set()
        await command
    finally:
        await provider.close()
    # Disabling noncamera monitoring must not retire the shared camera client.
    assert reservation.try_acquire() is not None


@pytest.mark.asyncio
async def test_monitor_passes_exact_session_reservation_and_new_account_is_independent(tmp_path):
    service, client, sessions, monitor, providers = monitor_fixture(tmp_path)
    try:
        await monitor.set_enabled(**preference(sessions))
        assert providers[0].options["lighting_reservation"] is client_lighting(client)
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_composed_record_cap_revokes_all_light_command_publication(monkeypatch):
    provider, _, _, batches, _ = make()
    try:
        await populate(provider)
        original = provider._groups.records()[0]
        records = tuple(
            replace(
                original,
                identity=DeviceIdentity(PROFILE, "ring-light-group", "home", "", f"group-{i}"),
            )
            for i in range(1025)
        )
        monkeypatch.setattr(provider._groups, "records", lambda: records)
        await provider._publish()
        assert len(batches[-1]["records"]) <= 1024
        assert not batches[-1]["complete"] and not batches[-1]["lights"]
        assert provider.status()["error"] == "device-limit-exceeded"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_partial_location_receipt_revokes_authority_and_retains_group_record():
    provider, gateway, groups, batches, clock = make()
    try:
        await populate(provider)
        key = provider._groups.records()[0].key
        gateway.locations["meta"] = {"has_next_page": True}
        await provider.refresh()
        clock[0] += 301
        await provider._refresh_groups()
        assert any(record.key == key for record in provider.snapshot())
        assert not batches[-1]["complete"]
        assert not batches[-1]["lights"][0]["can_set"]
        assert batches[-1]["lights"][0]["on"] is None
        assert groups.list_calls == ["home"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_owner_revocation_precedes_awaiting_removed_hub_cleanup():
    provider, gateway, _, _, _ = make(
        devices={
            "stickup_cams": [camera()],
            "base_stations": [{"id": 8, "location_id": "home", "owned": True}],
        }
    )
    try:
        await populate(provider)
        original = provider._stop_runtime
        checked = []

        async def stop(runtime):
            checked.append(True)
            state = provider._groups.snapshots()[0]
            assert state["authorization"] == "unknown" and not state["can_set"]
            await original(runtime)

        provider._stop_runtime = stop
        gateway.devices = {"stickup_cams": [camera(owned=False)]}
        await provider.refresh()
        assert checked
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_real_shared_transport_composes_discovery_and_single_confirmed_group_write():
    state = [False]

    async def request(url):
        method, _, options = calls[-1]
        if url == DEVICES_ENDPOINT:
            body = {"stickup_cams": [camera()]}
        elif url == LOCATIONS_ENDPOINT:
            body = {"user_locations": [{"location_id": "home", "name": "Home"}]}
        elif url == group_url("home"):
            body = inventory()
        elif url == group_url("home", "garden"):
            if method == "POST":
                assert options["json"] == {"lights_on": {"enabled": True, "duration_seconds": 60}}
                state[0] = True
                return Response(b"", status=204)
            body = report(state[0])
        else:
            pytest.fail("Unexpected synthetic Ring endpoint")
        return Response(json.dumps(body).encode())

    auth, calls, _ = fake_auth(request=request)
    alarm = RingAlarmRestGateway(auth)
    provider = NoncameraProvider(auth, profile_id=PROFILE, account_generation=1, gateway=alarm)
    try:
        await provider.refresh()
        await provider._refresh_groups()
        assert provider._groups.gateway._transport is alarm.shared_transport
        result = await provider.set_light(**args(provider._groups))
        assert result["status"] == "confirmed" and result["on"] is True
        assert sum(method == "POST" for method, _, _ in calls) == 1
        assert all(options["allow_redirects"] is False for _, _, options in calls)
    finally:
        await provider.close()
    assert alarm.shared_transport._closed


@pytest.mark.asyncio
async def test_hub_packet_crossed_by_lighting_write_cannot_refresh_light_but_preserves_battery(
    monkeypatch,
):
    provider, _, _, batches, _ = make(
        devices={
            "stickup_cams": [camera(id=9)],
            "beams_bridges": [{"id": 7, "location_id": "home", "role": "owner"}],
        },
        transport=BeamsTransport,
    )
    try:
        await populate(provider)
        runtime = provider._runtimes["home"]
        apply = provider._store.apply_message

        def crossed(*args):
            token = provider._lighting_reservation.try_acquire()
            assert token is not None
            provider._lighting_reservation.mark_sent(token)
            provider._lighting_reservation.release(token)
            return apply(*args)

        monkeypatch.setattr(provider._store, "apply_message", crossed)
        await runtime.transport.message(
            DeviceInfoDocUpdate("asset", (light(True, batteryLevel=50),))
        )
        row = next(record for record in provider.snapshot() if record.kind == "beams-light")
        observed = {item.name: item for item in row.observations}
        assert observed["light_on"].status == "stale"
        assert observed["light_on"].as_dict(now=100, max_age=120)["value"] is None
        assert (
            observed["battery_percent"].status == "current"
            and observed["battery_percent"].value == 50
        )
        beam = next(item for item in batches[-1]["lights"] if item["device_id"] == row.key)
        assert beam["on"] is None and not beam["can_set"]
        monkeypatch.setattr(provider._store, "apply_message", apply)
        await runtime.transport.message(DeviceInfoDocUpdate("asset", (light(True),)))
        assert (
            next(item for item in batches[-1]["lights"] if item["device_id"] == row.key)["on"]
            is True
        )
    finally:
        await provider.close()
