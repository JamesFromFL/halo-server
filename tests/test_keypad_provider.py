"""Keypad settings and proximity qualification using synthetic packets only."""

import asyncio

import pytest

from halo_server.devices.provider import NoncameraProvider
from halo_server.ring.hub.models import AlarmConnectionStatus
from halo_server.ring.hub.protocol import (
    AssetSessionInfo,
    DeviceDocument,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    SessionInfo,
)
from halo_server.ring.hub.rest import DEVICES_ENDPOINT
from halo_server.ring.hub.transport import AlarmTicketAsset, AlarmTransportError

PROFILE = "11111111-1111-4111-8111-111111111111"


def keypad(brightness=0.6, *, zid="keypad", device_type="security-keypad", impulse=False, **patch):
    return DeviceDocument(
        zid,
        {
            "zid": zid,
            "deviceType": device_type,
            "brightness": brightness,
            "commStatus": "ok",
            **patch,
        },
        keypad_motion=impulse,
    )


class Gateway:
    def __init__(self, owner=True):
        self.devices = {"base_stations": [{"id": 7, "location_id": "home", "owned": owner}]}

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
        self.sent, self.requests = [], []
        self.before_send = None
        self.readback = None
        self.assets = (AlarmTicketAsset("asset", "base_station_v1", "online", doorbot_id=7),)
        self.initial = (keypad(),)

    async def connect_once(self, *, epoch, stop_event, on_open, on_message):
        self.epoch, self.message, self.connected = epoch, on_message, True
        await on_open(self.assets)
        await self.message(DeviceInfoDocList("asset", self.initial))
        self.ready.set()
        await stop_event.wait()

    async def send_keypad_brightness(self, asset_id, zid, target, *, epoch, admission):
        if self.before_send:
            await self.before_send()
        with admission():
            self.sent.append((asset_id, zid, target, epoch))
            self.sent_event.set()
            if self.behavior == "confirm":
                await self.message(DeviceInfoDocUpdate("asset", (keypad(target / 100, zid=zid),)))
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


async def setup(*, owner=True, configure=None, **kwargs):
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
        gateway=Gateway(owner),
        transport_factory=factory,
        command_timeout=0.05,
        on_snapshot=lambda **snapshot: batches.append(snapshot),
        **kwargs,
    )
    await instance.refresh()
    await instance._runtimes["home"].transport.ready.wait()
    instance.batches = batches
    return instance


def snapshot(instance):
    return instance._keypad_commands.snapshots()[0]


def record(instance):
    return next(item for item in instance.snapshot() if item.family == "hub")


def motion(instance):
    return next(item for item in record(instance).observations if item.name == "motion_detected")


async def command(instance, target=35, *, before=None, admission=lambda: True):
    current = before or snapshot(instance)
    return await instance.set_keypad_brightness(
        current["device_id"],
        target,
        expected_revision=current["revision"],
        expected_epoch=current["epoch"],
        admission=admission,
    )


@pytest.mark.asyncio
async def test_fresh_post_send_evidence_confirms_exact_target_and_never_changes_alarm():
    instance = await setup()
    try:
        assert snapshot(instance)["brightness"] == 60
        assert snapshot(instance)["can_set"]
        assert "keypad-brightness" in record(instance).capabilities
        assert (await command(instance, admission=None))["code"] == "policy-denied"
        assert await command(instance) == {
            "status": "confirmed",
            "code": "observed",
            "possibly_sent": True,
            "brightness": 35,
        }
        pending = [
            row["keypads"][0]
            for row in instance.batches
            if row["keypads"] and row["keypads"][0]["pending"]
        ]
        assert pending[0]["brightness"] == 60
        assert pending[0]["requested_brightness"] == 35
        assert (await command(instance, 0))["brightness"] == 0
        assert instance._runtimes["home"].transport.sent == [
            ("asset", "keypad", 35, 1),
            ("asset", "keypad", 0, 1),
        ]
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [None, True, False, "0.5", -0.1, 1.1, {}, [], float("nan"), float("inf")]
)
async def test_malformed_brightness_does_not_establish_capability_or_write_authority(value):
    instance = await setup(configure=lambda t: setattr(t, "initial", (keypad(value),)))
    try:
        assert snapshot(instance)["brightness"] is None
        assert not snapshot(instance)["can_set"]
        assert "keypad-brightness" not in record(instance).capabilities
        assert not (await command(instance))["possibly_sent"]
        assert not instance._runtimes["home"].transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, True, False, "50", 0.5, -1, 101])
async def test_invalid_targets_are_rejected_without_transport(target):
    instance = await setup()
    try:
        assert (await command(instance, target))["code"] == "invalid-request"
        assert not instance._runtimes["home"].transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, False, 1, "owner"])
async def test_unproven_owner_has_no_keypad_write(owner):
    instance = await setup(owner=owner)
    try:
        assert snapshot(instance)["authorization"] == "unknown"
        assert (await command(instance))["code"] == "authorization-unknown"
        assert not instance._runtimes["home"].transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["silent", "error"])
async def test_socket_ack_or_failure_is_not_observed_confirmation_and_no_retries(behavior):
    instance = await setup(configure=lambda t: setattr(t, "behavior", behavior))
    try:
        # Even a target already observed before sending needs a new receipt.
        result = await command(instance, 60)
        assert result == {
            "status": "uncertain",
            "code": "outcome-unknown",
            "possibly_sent": True,
            "brightness": None,
        }
        assert snapshot(instance)["outcome_unknown"]
        assert (await command(instance))["code"] == "outcome-unknown"
        transport = instance._runtimes["home"].transport
        assert len(transport.sent) == 1
        await transport.message(DeviceInfoDocList("asset", (keypad(0.4),)))
        assert not snapshot(instance)["outcome_unknown"]
        assert snapshot(instance)["brightness"] == 40
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["policy", "epoch", "state", "offline", "owner"])
async def test_final_send_guard_blocks_crossed_policy_context_and_state(change):
    allowed = [True]
    instance = await setup()
    transport = instance._runtimes["home"].transport

    async def before_send():
        if change == "policy":
            allowed[0] = False
        elif change == "epoch":
            instance._store.begin_epoch("home")
        elif change == "state":
            await transport.message(DeviceInfoDocUpdate("asset", (keypad(0.7),)))
        elif change == "offline":
            transport.connected = False
        else:
            instance._gateway.devices["base_stations"][0]["owned"] = False
            await instance.refresh()

    transport.before_send = before_send
    try:
        result = await command(instance, admission=lambda: allowed[0])
        assert result["status"] == "rejected" and not result["possibly_sent"]
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_busy_and_cancellation_leave_uncertainty_without_resend():
    instance = await setup(configure=lambda t: setattr(t, "behavior", "blocked"))
    task = asyncio.create_task(command(instance))
    transport = instance._runtimes["home"].transport
    try:
        await transport.sent_event.wait()
        assert (await command(instance))["status"] == "busy"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert snapshot(instance)["outcome_unknown"]
        assert len(transport.sent) == 1
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_unrelated_patch_does_not_refresh_stale_brightness():
    clock = [100.0]
    instance = await setup(monotonic_clock=lambda: clock[0])
    try:
        clock[0] += 121
        transport = instance._runtimes["home"].transport
        await transport.message(
            DeviceInfoDocUpdate(
                "asset", (DeviceDocument("keypad", {"zid": "keypad", "commStatus": "ok"}),)
            )
        )
        assert (await command(instance))["code"] == "keypad-stale"
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_only_realtime_known_keypad_impulse_establishes_proximity():
    instance = await setup(
        configure=lambda t: setattr(t, "initial", (keypad(impulse=True, motionStatus="faulted"),))
    )
    try:
        assert motion(instance).value is None
        assert "motion" not in record(instance).capabilities
        transport = instance._runtimes["home"].transport
        await transport.message(DeviceInfoDocUpdate("unknown-asset", (keypad(impulse=True),)))
        assert motion(instance).value is None
        await transport.message(DeviceInfoDocUpdate("asset", (keypad(impulse=True),)))
        assert motion(instance).value is True
        assert motion(instance).source == "hub-keypad-motion-impulse"
        assert "motion" in record(instance).capabilities
        assert "ring-keypad-motion-impulse" in record(instance).capability_evidence
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_retrigger_expiry_is_timely_labeled_and_does_not_use_health_loop(monkeypatch):
    monkeypatch.setattr("halo_server.devices.provider._KEYPAD_MOTION_SECONDS", 0.06)
    instance = await setup(health_interval=600)
    try:
        transport = instance._runtimes["home"].transport
        await transport.message(DeviceInfoDocUpdate("asset", (keypad(impulse=True),)))
        await asyncio.sleep(0.04)
        await transport.message(DeviceInfoDocUpdate("asset", (keypad(impulse=True),)))
        await asyncio.sleep(0.04)
        assert motion(instance).value is True
        await asyncio.sleep(0.04)
        assert motion(instance).value is False
        assert motion(instance).source == "hub-keypad-motion-expiry"
        assert "motion" in record(instance).capabilities
        # A full inventory does not forget observed capability or reset the pulse.
        await transport.message(DeviceInfoDocList("asset", (keypad(),)))
        assert motion(instance).value is False
        assert "motion" in record(instance).capabilities
    finally:
        await instance.close()
    assert not instance._keypad_motion_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["offline", "epoch", "removed", "type-changed"])
async def test_proximity_evidence_retires_with_its_source(loss):
    instance = await setup()
    try:
        transport = instance._runtimes["home"].transport
        await transport.message(DeviceInfoDocUpdate("asset", (keypad(impulse=True),)))
        if loss == "offline":
            await transport.message(
                SessionInfo((AssetSessionInfo("asset", AlarmConnectionStatus.OFFLINE),))
            )
        elif loss == "epoch":
            instance._store.begin_epoch("home")
            await instance._publish()
        elif loss == "removed":
            await transport.message(DeviceInfoDocList("asset", ()))
        else:
            await transport.message(
                DeviceInfoDocUpdate("asset", (keypad(device_type="sensor.contact"),))
            )
        assert not instance._keypad_motion
        if loss != "removed":
            assert motion(instance).value is None
            assert "ring-keypad-motion-impulse" not in record(instance).capability_evidence
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_same_value_receipt_before_handoff_cannot_confirm_a_later_write():
    instance = await setup(configure=lambda t: setattr(t, "behavior", "silent"))
    transport = instance._runtimes["home"].transport

    async def before_send():
        await transport.message(DeviceInfoDocUpdate("asset", (keypad(0.6),)))

    transport.before_send = before_send
    try:
        result = await command(instance, 60)
        assert result["status"] == "uncertain" and result["brightness"] is None
        assert len(transport.sent) == 1
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_later_conflicting_observation_overrides_a_matching_acknowledgment():
    instance = await setup()
    transport = instance._runtimes["home"].transport

    async def send(asset_id, zid, target, *, epoch, admission):
        with admission():
            transport.sent.append(target)
            await transport.message(DeviceInfoDocUpdate("asset", (keypad(target / 100),)))
            await transport.message(DeviceInfoDocUpdate("asset", (keypad(0.9),)))

    transport.send_keypad_brightness = send
    try:
        result = await command(instance)
        assert result["status"] == "uncertain" and result["brightness"] is None
        assert snapshot(instance)["brightness"] == 90
        assert transport.sent == [35]
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_error_after_handoff_is_uncertain_even_if_transport_calls_it_superseded():
    from halo_server.ring.hub.provider import AlarmCommandSupersededError

    instance = await setup()
    transport = instance._runtimes["home"].transport

    async def send(asset_id, zid, target, *, epoch, admission):
        with admission():
            transport.sent.append(target)
            raise AlarmCommandSupersededError("connection-superseded")

    transport.send_keypad_brightness = send
    try:
        result = await command(instance)
        assert result["status"] == "uncertain" and result["possibly_sent"]
        assert snapshot(instance)["outcome_unknown"]
        assert transport.sent == [35]
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_newly_discovered_keypad_and_wrong_epoch_impulses_are_not_motion():
    instance = await setup()
    try:
        runtime = instance._runtimes["home"]
        transport = runtime.transport
        await instance._message(
            runtime, runtime.epoch + 1, DeviceInfoDocUpdate("asset", (keypad(impulse=True),))
        )
        assert motion(instance).value is None
        await transport.message(DeviceInfoDocUpdate("asset", (keypad(zid="new", impulse=True),)))
        new = next(item for item in instance.snapshot() if item.identity.device_id == "new")
        assert "ring-keypad-motion-impulse" not in new.capability_evidence
        assert not instance._keypad_motion
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_motion_identity_and_retirement_are_isolated_between_locations():
    class MultiGateway(Gateway):
        def __init__(self):
            self.devices = {
                "base_stations": [
                    {"id": 7, "location_id": "home", "owned": True},
                    {"id": 8, "location_id": "other", "owned": True},
                ]
            }

        async def request_json(self, url):
            return (
                self.devices
                if url == DEVICES_ENDPOINT
                else {"user_locations": [{"location_id": name} for name in ("home", "other")]}
            )

    instance = NoncameraProvider(
        None,
        profile_id=PROFILE,
        account_generation=1,
        gateway=MultiGateway(),
        transport_factory=Transport,
    )
    try:
        await instance.refresh()
        await asyncio.gather(*(r.transport.ready.wait() for r in instance._runtimes.values()))
        home = instance._runtimes["home"].transport
        other = instance._runtimes["other"].transport
        await home.message(DeviceInfoDocUpdate("asset", (keypad(impulse=True),)))
        by_location = {r.identity.location_id: r for r in instance.snapshot() if r.family == "hub"}
        assert "ring-keypad-motion-impulse" in by_location["home"].capability_evidence
        assert "ring-keypad-motion-impulse" not in by_location["other"].capability_evidence
        await other.message(
            SessionInfo((AssetSessionInfo("asset", AlarmConnectionStatus.OFFLINE),))
        )
        assert set(instance._keypad_motion) == {("home", "asset", "keypad")}
        assert not home.sent and not other.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_idle_expired_window_stays_current_without_inventing_new_source_events(monkeypatch):
    monkeypatch.setattr("halo_server.devices.provider._KEYPAD_MOTION_SECONDS", 0.01)
    clock = [100.0]
    instance = await setup(clock=lambda: clock[0], monotonic_clock=lambda: clock[0])
    try:
        transport = instance._runtimes["home"].transport
        await transport.message(DeviceInfoDocUpdate("asset", (keypad(impulse=True),)))
        clock[0] = 100.02
        await asyncio.sleep(0.02)
        assert motion(instance).value is False
        clock[0] = 501.0
        # The continuous socket renews the complete inventory, not the event.
        await transport.message(DeviceInfoDocList("asset", (keypad(),)))
        reading = motion(instance)
        assert reading.value is False and reading.source == "hub-keypad-motion-expiry"
        assert reading.observed_at == 501.0
        assert reading.as_dict(now=501.0, max_age=360)["status"] == "current"
        assert instance._keypad_motion[("home", "asset", "keypad")].observed_at == 100.0
        await transport.message(
            SessionInfo((AssetSessionInfo("asset", AlarmConnectionStatus.OFFLINE),))
        )
        assert motion(instance).value is None
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_timer_publishes_unknown_if_inventory_ages_out_while_waiting(monkeypatch):
    monkeypatch.setattr("halo_server.devices.provider._KEYPAD_MOTION_SECONDS", 0.01)
    clock = [100.0]
    instance = await setup(
        clock=lambda: clock[0], monotonic_clock=lambda: clock[0], health_interval=600
    )
    try:
        transport = instance._runtimes["home"].transport
        clock[0] = 219.0
        await transport.message(DeviceInfoDocUpdate("asset", (keypad(impulse=True),)))
        assert motion(instance).value is True
        clock[0] = 239.0
        await asyncio.sleep(0.02)
        assert motion(instance).value is None
        assert not instance._keypad_motion
        assert not transport.sent
    finally:
        await instance.close()
