"""Physical Beams qualification with synthetic inventory and sockets only."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from halo_server.devices.light_commands import BEAMS_TYPES
from halo_server.devices.provider import NoncameraProvider
from halo_server.ring.hub.models import AlarmConnectionStatus
from halo_server.ring.hub.protocol import (
    AssetSessionInfo,
    DeviceDocument,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    SessionInfo,
    encode_light_command,
)
from halo_server.ring.hub.provider import AlarmCommandSupersededError, AlarmLocationSeed
from halo_server.ring.hub.rest import DEVICES_ENDPOINT
from halo_server.ring.hub.transport import (
    AlarmTicketAsset,
    AlarmTransportError,
    RingAlarmTransport,
    _parse_ticket,
)

PROFILE = "11111111-1111-4111-8111-111111111111"


def light(on=False, *, zid="light", device_type="switch.beams", **patch):
    return DeviceDocument(
        zid, {"zid": zid, "deviceType": device_type, "on": on, "commStatus": "ok", **patch}
    )


class Gateway:
    def __init__(self, role="owner"):
        self.devices = {
            "beams_bridges": [{"id": 7, "location_id": "home", "role": role, "owner_id": 1000}]
        }

    async def request_json(self, url):
        return (
            self.devices
            if url == DEVICES_ENDPOINT
            else {"user_locations": [{"location_id": "home", "name": "Home"}]}
        )

    async def close(self):
        pass


class Transport:
    def __init__(self, gateway, seed):
        self.seed = seed
        self.connected = False
        self.ready = asyncio.Event()
        self.sent_event = asyncio.Event()
        self.release = asyncio.Event()
        self.behavior = "confirm"
        self.sent = []
        self.requests = []
        self.before_send = None
        self.readback = None
        self.assets = (AlarmTicketAsset("asset", "beams_bridge_v1", "online", doorbot_id=7),)
        self.initial = (light(),)

    async def connect_once(self, *, epoch, stop_event, on_open, on_message):
        self.epoch = epoch
        self.message = on_message
        self.connected = True
        await on_open(self.assets)
        await self.message(DeviceInfoDocList("asset", self.initial))
        self.ready.set()
        await stop_event.wait()

    async def send_light(self, asset_id, zid, target, *, duration_seconds, epoch, admission):
        if self.before_send:
            await self.before_send()
        with admission():
            self.sent.append((asset_id, zid, target, duration_seconds, epoch))
            self.sent_event.set()
            if self.behavior == "confirm":
                await self.message(DeviceInfoDocUpdate("asset", (light(target, zid=zid),)))
            elif self.behavior == "error":
                raise AlarmTransportError("permission-denied")
            elif self.behavior == "blocked":
                await self.release.wait()

    async def request_inventory(self, asset, *, epoch):
        self.requests.append((asset, epoch))
        if self.readback:
            await self.message(self.readback)

    async def close(self):
        self.connected = False


async def setup(*, role="owner", configure=None, **kwargs):
    batches = []

    def factory(gateway, seed):
        transport = Transport(gateway, seed)
        if configure:
            configure(transport)
        return transport

    instance = NoncameraProvider(
        None,
        profile_id=PROFILE,
        account_generation=2,
        provider_generation=3,
        gateway=Gateway(role),
        transport_factory=factory,
        command_timeout=0.03,
        on_snapshot=lambda **snapshot: batches.append(snapshot),
        **kwargs,
    )
    await instance.refresh()
    await instance._runtimes["home"].transport.ready.wait()
    instance.batches = batches
    return instance


def snapshot(instance, index=0):
    return instance._light_commands.snapshots()[index]


async def command(
    instance, target=True, *, duration="default", before=None, admission=lambda: True
):
    current = before or snapshot(instance)
    return await instance.set_light(
        current["device_id"],
        target,
        expected_revision=current["revision"],
        expected_epoch=current["epoch"],
        duration_seconds=(60 if target is True else None) if duration == "default" else duration,
        admission=admission,
    )


@pytest.mark.asyncio
async def test_exact_bridge_owner_and_observed_state_confirm_without_optimism():
    instance = await setup()
    try:
        assert snapshot(instance)["authorization"] == "allowed"
        assert snapshot(instance)["can_set"]
        record = next(r for r in instance.snapshot() if r.family == "hub")
        assert record.kind == "beams-light"
        assert record.model == "switch.beams"
        assert (await command(instance, admission=None))["code"] == "policy-denied"
        result = await command(instance)
        assert result == {
            "status": "confirmed",
            "code": "observed",
            "possibly_sent": True,
            "on": True,
        }
        pending = [
            b["lights"][0] for b in instance.batches if b["lights"] and b["lights"][0]["pending"]
        ]
        assert pending[0]["on"] is False and pending[0]["requested_on"] is True
        assert snapshot(instance)["on"] is True
        # An already-on light must receive a fresh override, not an optimistic shortcut.
        assert (await command(instance, duration=300))["status"] == "confirmed"
        assert len(instance._runtimes["home"].transport.sent) == 2
        assert (await command(instance, False))["on"] is False
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [None, True, 1, "shared", "Owner", "owner\n"])
async def test_unqualified_role_never_uses_owner_id_or_inventory_access(role):
    instance = await setup(role=role)
    try:
        assert snapshot(instance)["authorization"] == "unknown"
        assert (await command(instance))["code"] == "authorization-unknown"
        assert not instance._runtimes["home"].transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asset_id,kind,doorbot_id",
    [
        ("asset", "beams_bridge_v1", None),
        ("asset", "beams_bridge_v1", 8),
        ("asset", "base_station_v1", 7),
        ("asset", "beams_bridge_v2", 7),
        ("asset", "beams_bridge_v1-unknown", 7),
    ],
)
async def test_ticket_requires_matching_bridge_not_location_ownership(asset_id, kind, doorbot_id):
    instance = await setup(
        configure=lambda t: setattr(
            t, "assets", (AlarmTicketAsset(asset_id, kind, "online", doorbot_id=doorbot_id),)
        )
    )
    try:
        assert snapshot(instance)["authorization"] == "unknown"
        assert not (await command(instance))["possibly_sent"]
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_duplicate_ticket_bridge_mapping_cannot_authorize_either_asset():
    instance = await setup(
        configure=lambda t: setattr(
            t,
            "assets",
            (
                AlarmTicketAsset("asset", "beams_bridge_v1", "online", doorbot_id=7),
                AlarmTicketAsset("other", "beams_bridge_v1", "online", doorbot_id=7),
            ),
        )
    )
    try:
        assert snapshot(instance)["authorization"] == "unknown"
        assert not (await command(instance))["possibly_sent"]
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "device_type",
    [
        *BEAMS_TYPES,
        "switch",
        "switch.multilevel",
        "group.light-group.beams",
        "device.beams",
        "motion-sensor.beams",
        "security-panel",
        "switch.multilevel.bulb",
    ],
)
async def test_only_exact_physical_beams_models_publish_command_dto(device_type):
    instance = await setup(
        configure=lambda t: setattr(t, "initial", (light(device_type=device_type),))
    )
    try:
        snapshots = instance._light_commands.snapshots()
        assert bool(snapshots) is (device_type in BEAMS_TYPES)
        record = next(r for r in instance.snapshot() if r.family == "hub")
        assert (record.kind == "beams-light") is (device_type in BEAMS_TYPES)
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [None, 0, 1, "true", {}, []])
async def test_unknown_or_coerced_state_is_never_command_eligible(on):
    instance = await setup(configure=lambda t: setattr(t, "initial", (light(on),)))
    try:
        assert snapshot(instance)["on"] is None
        assert not snapshot(instance)["can_set"]
        assert not (await command(instance))["possibly_sent"]
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,duration",
    [
        (1, 60),
        ("on", 60),
        (True, None),
        (True, True),
        (True, 31),
        (True, 60.0),
        (False, 60),
        (False, 0),
    ],
)
async def test_unsupported_values_never_send(target, duration):
    instance = await setup()
    try:
        assert (await command(instance, target, duration=duration))["code"] == "invalid-request"
        assert not instance._runtimes["home"].transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "admission", "role", "device", "epoch", "removed", "ticket"]
)
async def test_state_and_policy_rechecked_at_last_send_boundary(change):
    active = [True]
    permit = [True]
    instance = await setup(admission=lambda: active[0])
    transport = instance._runtimes["home"].transport

    async def before_send():
        if change == "account":
            active[0] = False
        elif change == "admission":
            permit[0] = False
        elif change == "role":
            instance._beams_owners[("home", "7")] = False
        elif change == "device":
            await transport.message(DeviceInfoDocUpdate("asset", (light(name="Renamed"),)))
        elif change == "epoch":
            instance._runtimes["home"].epoch += 1
        elif change == "removed":
            await transport.message(DeviceInfoDocList("asset", ()))
        elif change == "ticket":
            transport.assets = (replace(transport.assets[0], doorbot_id=8),)

    transport.before_send = before_send
    try:
        assert not (await command(instance, admission=lambda: permit[0]))["possibly_sent"]
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_return_to_automatic_cannot_invent_off_or_accept_old_observation():
    instance = await setup(configure=lambda t: setattr(t, "initial", (light(True),)))
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"
    try:
        result = await command(instance, False)
        assert result["status"] == "uncertain"
        # A handed-off lighting mutation invalidates the old physical reading;
        # returning automatic cannot invent either on or off without readback.
        assert result["on"] is None
        assert snapshot(instance)["on"] is None
        assert snapshot(instance)["outcome_unknown"]
        assert not (await command(instance, False))["possibly_sent"]
        marker = snapshot(instance)["readback_revision"]
        await transport.message(
            DeviceInfoDocUpdate(
                "asset", (DeviceDocument("light", {"zid": "light", "batteryLevel": 70}),)
            )
        )
        assert snapshot(instance)["readback_revision"] == marker
        assert snapshot(instance)["outcome_unknown"]
        await transport.message(DeviceInfoDocList("asset", (light(True),)))
        assert snapshot(instance)["readback_revision"] > marker
        assert not snapshot(instance)["outcome_unknown"]
        # Same target as already-observed On still needs new readback after send.
        assert (await command(instance, True))["status"] == "uncertain"
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_busy_and_cancellation_keep_uncertain_write_blocked_until_readback():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    transport.behavior = "blocked"
    task = asyncio.create_task(command(instance))
    try:
        await transport.sent_event.wait()
        assert (await command(instance))["status"] == "busy"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert snapshot(instance)["outcome_unknown"]
        assert not snapshot(instance)["pending"]
        await transport.message(DeviceInfoDocList("asset", (light(True),)))
        assert not snapshot(instance)["outcome_unknown"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await instance.close()


@pytest.mark.asyncio
async def test_fresh_battery_or_on_patch_cannot_refresh_old_device_connection():
    now = [1000.0]
    instance = await setup(monotonic_clock=lambda: now[0])
    transport = instance._runtimes["home"].transport
    try:
        now[0] += 121
        await transport.message(
            DeviceInfoDocUpdate(
                "asset",
                (DeviceDocument("light", {"zid": "light", "on": True, "batteryLevel": 80}),),
            )
        )
        assert not snapshot(instance)["can_set"]
        marker = snapshot(instance)["readback_revision"]
        await transport.message(
            DeviceInfoDocList(
                "asset",
                (
                    DeviceDocument(
                        "light", {"zid": "light", "deviceType": "switch.beams", "on": True}
                    ),
                ),
            )
        )
        assert not snapshot(instance)["can_set"]
        assert snapshot(instance)["readback_revision"] == marker
        await transport.message(DeviceInfoDocList("asset", (light(True),)))
        assert snapshot(instance)["can_set"]
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_disconnection_invalidates_pending_without_replaying():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"
    task = asyncio.create_task(command(instance))
    try:
        await transport.sent_event.wait()
        await transport.message(
            SessionInfo(
                (AssetSessionInfo("asset", AlarmConnectionStatus.OFFLINE, "beams_bridge_v1"),)
            )
        )
        result = await task
        assert result["status"] == "uncertain"
        assert len(transport.sent) == 1
        assert not snapshot(instance)["can_set"]
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["newer-state", "permission"])
async def test_completion_does_not_return_stale_target_or_revoked_success(change):
    permit = [True]
    instance = await setup()
    transport = instance._runtimes["home"].transport
    original = transport.send_light

    async def send(*args, **kwargs):
        await original(*args, **kwargs)
        if change == "newer-state":
            await transport.message(DeviceInfoDocUpdate("asset", (light(False),)))
        else:
            permit[0] = False

    transport.send_light = send
    try:
        result = await command(instance, admission=lambda: permit[0])
        assert result["status"] == "uncertain"
        assert result["on"] is (change == "permission")
        assert len(transport.sent) == 1
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_readback_and_command_tokens_do_not_repeat_on_remove_reappear():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    try:
        before = snapshot(instance)
        await transport.message(DeviceInfoDocList("asset", ()))
        assert not instance._light_commands.snapshots()
        await transport.message(DeviceInfoDocList("asset", (light(False),)))
        after = snapshot(instance)
        assert after["device_id"] == before["device_id"]
        assert after["revision"] > before["revision"]
        assert after["readback_revision"] > before["readback_revision"]
        assert (await command(instance, before=before))["code"] == "state-changed"
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_distinct_lights_share_reservation_and_foreign_packets_cannot_confirm():
    instance = await setup(
        configure=lambda t: setattr(t, "initial", (light(zid="one"), light(zid="two")))
    )
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"
    first, second = instance._light_commands.snapshots()
    task = asyncio.create_task(command(instance, before=first))
    try:
        await transport.sent_event.wait()
        await transport.message(DeviceInfoDocUpdate("foreign", (light(True, zid="one"),)))
        assert not task.done()
        transport.behavior = "confirm"
        result = await command(instance, before=second)
        assert result["status"] == "rejected"
        assert result["code"] == "light-stale"
        # A fresh independent reading restores eligibility, but cannot bypass
        # the shared reservation still held by the first write.
        await transport.message(DeviceInfoDocUpdate("asset", (light(False, zid="two"),)))
        second = instance._light_commands.snapshots()[1]
        result = await command(instance, before=second)
        assert result["status"] == "busy"
        assert result["code"] == "lighting-busy"
        assert (await task)["status"] == "uncertain"
        result = await command(instance, before=second)
        assert result["status"] == "confirmed"
        assert (await task)["status"] == "uncertain"
        assert len(transport.sent) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await instance.close()


@pytest.mark.asyncio
async def test_group_or_camera_mutation_requires_explicit_new_physical_light_evidence():
    instance = await setup(configure=lambda t: setattr(t, "initial", (light(batteryLevel=70),)))
    transport = instance._runtimes["home"].transport
    reservation = instance._light_commands.reservation
    try:
        before = snapshot(instance)
        assert before["on"] is False and before["can_set"]
        token = reservation.try_acquire()
        assert token is not None and reservation.mark_sent(token)
        reservation.release(token)
        await instance._publish()

        assert snapshot(instance)["on"] is None
        assert snapshot(instance)["reason"] == "light-stale"
        result = await command(instance, before=before)
        assert not result["possibly_sent"] and not transport.sent
        record = next(row for row in instance.snapshot() if row.kind == "beams-light")
        observed = {item.name: item for item in record.observations}
        assert observed["light_on"].status == "stale"
        assert observed["light_on"].value is False
        assert observed["battery_percent"].status == "current"

        await transport.message(
            DeviceInfoDocUpdate(
                "asset",
                (
                    DeviceDocument(
                        "light", {"zid": "light", "batteryLevel": 80, "commStatus": "ok"}
                    ),
                ),
            )
        )
        assert snapshot(instance)["on"] is None
        assert not snapshot(instance)["can_set"]
        assert snapshot(instance)["readback_revision"] == before["readback_revision"]

        await transport.message(
            DeviceInfoDocUpdate("asset", (DeviceDocument("light", {"zid": "light", "on": True}),))
        )
        assert snapshot(instance)["on"] is True
        assert snapshot(instance)["can_set"]
        record = next(row for row in instance.snapshot() if row.kind == "beams-light")
        observed = {item.name: item for item in record.observations}
        assert observed["light_on"].status == "current"
        assert observed["light_on"].value is True
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_pending_own_write_preserves_previous_reading_but_only_new_on_can_confirm():
    instance = await setup()
    instance._light_commands.timeout = 0.3
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"
    task = asyncio.create_task(command(instance))
    try:
        await transport.sent_event.wait()
        assert snapshot(instance)["pending"]
        assert snapshot(instance)["on"] is False
        assert instance._light_commands._observed(snapshot(instance)["device_id"]) is None
        await transport.message(
            DeviceInfoDocUpdate(
                "asset", (DeviceDocument("light", {"zid": "light", "batteryLevel": 80}),)
            )
        )
        assert not task.done()
        assert snapshot(instance)["on"] is False
        assert not snapshot(instance)["outcome_unknown"]
        await transport.message(
            DeviceInfoDocUpdate("asset", (DeviceDocument("light", {"zid": "light", "on": True}),))
        )
        result = await task
        assert result["status"] == "confirmed" and result["on"] is True
        assert snapshot(instance)["on"] is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await instance.close()


@pytest.mark.asyncio
async def test_full_inventory_or_invalid_on_cannot_retain_old_light_proof():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    try:
        await transport.message(
            DeviceInfoDocList(
                "asset",
                (
                    DeviceDocument(
                        "light", {"zid": "light", "deviceType": "switch.beams", "commStatus": "ok"}
                    ),
                ),
            )
        )
        assert not snapshot(instance)["can_set"]
        assert snapshot(instance)["on"] is None
        assert not instance._light_commands._on_revisions
        await transport.message(DeviceInfoDocUpdate("asset", (light(True),)))
        assert snapshot(instance)["can_set"]
        await transport.message(
            DeviceInfoDocUpdate("asset", (DeviceDocument("light", {"zid": "light", "on": 1}),))
        )
        assert snapshot(instance)["on"] is None
        assert not instance._light_commands._on_revisions
        await transport.message(DeviceInfoDocList("asset", ()))
        assert not instance._light_commands.snapshots()
        assert not instance._light_commands._on_revisions
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_close_retires_pending_and_never_exposes_cached_state_as_success():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"
    task = asyncio.create_task(command(instance))
    await transport.sent_event.wait()
    await instance.close()
    result = await task
    assert result["status"] == "uncertain"
    assert result["on"] is None
    assert len(transport.sent) == 1


@pytest.mark.asyncio
async def test_authority_failure_keeps_stale_rest_evidence_from_admitting():
    instance = await setup()
    try:
        instance._gateway.devices = {}
        await instance.refresh()
        assert snapshot(instance)["reason"] == "source-unavailable"
        assert not (await command(instance))["possibly_sent"]
    finally:
        await instance.close()


@pytest.mark.parametrize(
    "target,duration", [(True, 30), (True, 60), (True, 300), (True, 900), (False, None)]
)
def test_wire_command_is_exact_light_mode_without_generic_switch_or_bypass(target, duration):
    frame = encode_light_command("asset", "light", target, duration, 12)
    body = frame["msg"]["body"]
    assert body == [
        {
            "zid": "light",
            "command": {
                "v1": [
                    {
                        "commandType": "light-mode.set",
                        "data": {"lightMode": "on", "duration": duration}
                        if target
                        else {"lightMode": "default"},
                    }
                ]
            },
        }
    ]


@pytest.mark.parametrize("doorbot_id", [None, True, -1, 0, 7.0, "7", 2**53, 7])
def test_ticket_doorbot_id_is_bounded_optional_identity_evidence(doorbot_id):
    ticket = _parse_ticket(
        {
            "host": "test.example",
            "ticket": "test",
            "assets": [
                {
                    "uuid": "asset",
                    "kind": "beams_bridge_v1",
                    "status": "online",
                    "doorbotId": doorbot_id,
                }
            ],
        }
    )
    assert ticket.assets[0].doorbot_id == (
        7 if type(doorbot_id) is int and doorbot_id == 7 else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsupported_kind", ["base_station_v1", "beams_bridge_v2", "beams_bridge_v1-unknown"]
)
async def test_real_transport_exact_asset_gate_and_admission_no_network(unsupported_kind):
    sent = []

    async def send(frame):
        sent.append(frame)

    transport = RingAlarmTransport(None, AlarmLocationSeed("home"))
    transport._websocket = SimpleNamespace(closed=False, send_str=send)
    transport._connection_epoch = 1
    transport._assets = (AlarmTicketAsset("asset", unsupported_kind, "online"),)

    @contextmanager
    def guard():
        yield

    with pytest.raises(AlarmTransportError):
        await transport.send_light(
            "asset", "light", True, duration_seconds=60, epoch=1, admission=guard
        )
    transport._assets = (AlarmTicketAsset("asset", "beams_bridge_v1", "online", doorbot_id=7),)
    with pytest.raises(AlarmTransportError):
        await transport._send_encoded(
            lambda seq: encode_light_command("asset", "light", True, 60, seq), epoch=1
        )
    assert sent == []
    await transport.send_light(
        "asset", "light", True, duration_seconds=60, epoch=1, admission=guard
    )
    assert len(sent) == 1 and '"light-mode.set"' in sent[0]

    @contextmanager
    def revoked():
        raise AlarmCommandSupersededError("policy-denied")
        yield

    with pytest.raises(AlarmCommandSupersededError):
        await transport.send_light(
            "asset", "light", False, duration_seconds=None, epoch=1, admission=revoked
        )
    assert len(sent) == 1
