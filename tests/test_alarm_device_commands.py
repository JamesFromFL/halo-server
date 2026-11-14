"""Alarm command qualification with synthetic inventory and sockets only."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from halo_server.devices.provider import NoncameraProvider
from halo_server.ring.hub.models import AlarmConnectionStatus, AlarmMode
from halo_server.ring.hub.protocol import (
    AssetSessionInfo,
    DeviceDocument,
    DeviceInfoDocList,
    DeviceInfoDocUpdate,
    SessionInfo,
    encode_mode_command,
)
from halo_server.ring.hub.provider import AlarmCommandSupersededError, AlarmLocationSeed
from halo_server.ring.hub.rest import DEVICES_ENDPOINT
from halo_server.ring.hub.transport import AlarmTicketAsset, AlarmTransportError, RingAlarmTransport

PROFILE = "11111111-1111-4111-8111-111111111111"
RAW = {AlarmMode.DISARMED: "none", AlarmMode.HOME: "some", AlarmMode.AWAY: "all"}


def panel(mode="none", **patch):
    return DeviceDocument(
        "panel",
        {
            "zid": "panel",
            "deviceType": "security-panel",
            "mode": mode,
            "alarmInfo": {"state": "none", "faultedDevices": []},
            "commStatus": "ok",
            **patch,
        },
    )


class Gateway:
    def __init__(self, owned=True, locations=("home",)):
        self.locations = locations
        self.devices = {
            "base_stations": [
                {"id": index + 1, "location_id": name, "owned": owned}
                for index, name in enumerate(locations)
            ]
        }

    async def request_json(self, url):
        return (
            self.devices
            if url == DEVICES_ENDPOINT
            else {
                "user_locations": [{"location_id": name, "name": name} for name in self.locations]
            }
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

    async def connect_once(self, *, epoch, stop_event, on_open, on_message):
        self.epoch = epoch
        self.message = on_message
        self.connected = True
        await on_open((AlarmTicketAsset("asset", "base_station_v1", "online"),))
        await self.message(DeviceInfoDocList("asset", (panel(),)))
        self.ready.set()
        await stop_event.wait()

    async def send_mode(self, asset_id, panel_id, mode, *, epoch, admission):
        if self.before_send:
            await self.before_send()
        with admission():
            self.sent.append((asset_id, panel_id, mode, epoch))
            self.sent_event.set()
            if self.behavior == "confirm":
                await self.message(DeviceInfoDocUpdate("asset", (panel(RAW[mode]),)))
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


async def setup(*, owned=True, locations=("home",), **kwargs):
    batches = []
    instance = NoncameraProvider(
        None,
        profile_id=PROFILE,
        account_generation=2,
        provider_generation=3,
        gateway=Gateway(owned, locations),
        transport_factory=Transport,
        command_timeout=0.03,
        on_snapshot=lambda **snapshot: batches.append(snapshot),
        **kwargs,
    )
    await instance.refresh()
    await asyncio.gather(
        *(runtime.transport.ready.wait() for runtime in instance._runtimes.values())
    )
    instance.batches = batches
    return instance


def snapshot(instance, location="home"):
    key = instance._alarm_commands._identity(
        instance._store.snapshot.find_location(location).security_panel
    ).key
    return next(item for item in instance._alarm_commands.snapshots() if item["device_id"] == key)


async def command(instance, target="away", *, location="home", before=None, admission=lambda: True):
    current = before or snapshot(instance, location)
    return await instance.set_alarm_mode(
        current["device_id"],
        target,
        expected_revision=current["revision"],
        expected_epoch=current["epoch"],
        admission=admission,
    )


@pytest.mark.asyncio
async def test_owner_observation_and_explicit_admission_confirm_normal_mode():
    instance = await setup()
    try:
        initial = snapshot(instance)
        assert initial["authorization"] == "allowed"
        assert initial["authorization_evidence"] == "ring-base-station-owner"
        assert initial["allowed_targets"] == ["disarmed", "home", "away"]
        denied = await command(instance, admission=None)
        assert denied == {
            "status": "rejected",
            "code": "policy-denied",
            "possibly_sent": False,
            "mode": None,
        }
        result = await command(instance)
        assert result == {
            "status": "confirmed",
            "code": "observed",
            "possibly_sent": True,
            "mode": "away",
        }
        pending = [
            s["alarms"][0] for s in instance.batches if s["alarms"] and s["alarms"][0]["pending"]
        ]
        assert pending and pending[0]["mode"] == "disarmed"
        assert snapshot(instance)["mode"] == "away"
        assert not snapshot(instance)["pending"]
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, None, 1, "true"])
async def test_access_and_truthy_ownership_do_not_authorize(owned):
    instance = await setup(owned=owned)
    try:
        assert snapshot(instance)["authorization"] == "unknown"
        assert (await command(instance))["code"] == "authorization-unknown"
        assert not instance._runtimes["home"].transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["panic", "fire", "night", "all", {}, True])
async def test_unsupported_controls_never_send(target):
    instance = await setup()
    try:
        assert (await command(instance, target))["code"] == "invalid-request"
        current = snapshot(instance)
        with pytest.raises(TypeError):
            await instance.set_alarm_mode(
                current["device_id"],
                "away",
                expected_revision=current["revision"],
                expected_epoch=current["epoch"],
                admission=lambda: True,
                bypass_ids=["sensor"],
            )
        assert not instance._runtimes["home"].transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["admission", "panel", "epoch", "ownership", "account"])
async def test_changed_state_is_fenced_after_transport_queue(change):
    active = [True]
    permit = [True]
    instance = await setup(admission=lambda: active[0])
    transport = instance._runtimes["home"].transport

    async def before_send():
        if change == "admission":
            permit[0] = False
        elif change == "panel":
            await transport.message(DeviceInfoDocUpdate("asset", (panel("some"),)))
        elif change == "epoch":
            instance._runtimes["home"].epoch = instance._store.begin_epoch("home")
        elif change == "ownership":
            instance._gateway.devices["base_stations"][0]["owned"] = False
            await instance.refresh()
        else:
            active[0] = False

    transport.before_send = before_send
    try:
        result = await command(instance, admission=lambda: permit[0])
        assert result["status"] == "rejected" and result["possibly_sent"] is False
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["silent", "unrelated", "foreign", "pre-send", "wrong-mode"])
async def test_only_explicit_post_send_matching_panel_mode_confirms(kind):
    instance = await setup()
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"

    async def before_send():
        if kind == "pre-send":
            await transport.message(DeviceInfoDocUpdate("asset", (panel("all"),)))

    transport.before_send = before_send
    try:
        task = asyncio.create_task(command(instance))
        if kind != "pre-send":
            await transport.sent_event.wait()
            if kind == "unrelated":
                await transport.message(
                    DeviceInfoDocUpdate(
                        "asset", (DeviceDocument("panel", {"zid": "panel", "batteryLevel": 50}),)
                    )
                )
            elif kind == "foreign":
                await transport.message(DeviceInfoDocUpdate("other-asset", (panel("all"),)))
            elif kind == "wrong-mode":
                await transport.message(DeviceInfoDocUpdate("asset", (panel("some"),)))
        result = await task
        assert result["status"] == ("rejected" if kind == "pre-send" else "uncertain")
        if kind != "pre-send":
            assert snapshot(instance)["outcome_unknown"]
            assert (await command(instance))["code"] == "outcome-unknown"
            await transport.message(DeviceInfoDocUpdate("asset", (panel("all"),)))
            assert snapshot(instance)["outcome_unknown"]
            await transport.message(DeviceInfoDocList("asset", (panel("all"),)))
            assert not snapshot(instance)["outcome_unknown"]
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_readback_clears_uncertainty_but_does_not_claim_failed_write_success():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    transport.behavior = "error"
    transport.readback = DeviceInfoDocList("asset", (panel("some"),))
    try:
        result = await command(instance)
        assert result["status"] == "uncertain"
        assert result["code"] == "outcome-unknown"  # Do not infer authorization from error text.
        assert not snapshot(instance)["outcome_unknown"]
        assert snapshot(instance)["mode"] == "home"
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_per_location_busy_does_not_block_another_location():
    instance = await setup(locations=("home", "garden"))
    home = instance._runtimes["home"].transport
    home.behavior = "blocked"
    try:
        first = asyncio.create_task(command(instance))
        await home.sent_event.wait()
        assert (await command(instance))["status"] == "busy"
        assert (await command(instance, location="garden"))["status"] == "confirmed"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not snapshot(instance)["pending"]
        assert snapshot(instance)["outcome_unknown"]
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alarm_info,expected",
    [
        ({"state": "none", "faultedDevices": ["open-door"]}, "sensors-require-bypass"),
        ({"state": "none"}, "faulted-device-list-unknown"),
        ({"state": "entry-delay", "faultedDevices": []}, "transition-active"),
        ({"state": "fire-alarm", "faultedDevices": []}, "alarm-active"),
        ({"state": "future-state", "faultedDevices": []}, "panel-state-unknown"),
    ],
)
async def test_fault_transition_and_emergency_guards(alarm_info, expected):
    instance = await setup()
    transport = instance._runtimes["home"].transport
    try:
        await transport.message(DeviceInfoDocUpdate("asset", (panel(alarmInfo=alarm_info),)))
        assert (await command(instance))["code"] == expected
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_stale_inventory_and_reused_panel_revision_are_rejected():
    clock = [100.0]
    instance = await setup(monotonic_clock=lambda: clock[0])
    transport = instance._runtimes["home"].transport
    try:
        old = snapshot(instance)
        await transport.message(DeviceInfoDocList("asset", ()))
        await transport.message(DeviceInfoDocList("asset", (panel(),)))
        assert snapshot(instance)["revision"] != old["revision"]
        assert (await command(instance, before=old))["code"] == "state-changed"
        clock[0] += 121
        assert (await command(instance))["code"] == "panel-stale"
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_disconnect_marks_inflight_unknown_and_never_retries_write():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"
    try:
        task = asyncio.create_task(command(instance))
        await transport.sent_event.wait()
        await transport.message(
            SessionInfo(
                (AssetSessionInfo("asset", AlarmConnectionStatus.OFFLINE, "base_station_v1"),)
            )
        )
        assert (await task)["status"] == "uncertain"
        assert len(transport.sent) == 1
        assert snapshot(instance)["outcome_unknown"]
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_transport_only_typed_mode_schema_and_socket_boundary_guard():
    sent = []

    async def send(frame):
        sent.append(frame)

    transport = RingAlarmTransport(None, AlarmLocationSeed("home"))
    transport._websocket = SimpleNamespace(closed=False, send_str=send)
    transport._connection_epoch = 1
    transport._assets = (AlarmTicketAsset("asset", "base_station_v1", "online"),)

    @contextmanager
    def allow():
        yield

    @contextmanager
    def deny():
        raise AlarmCommandSupersededError("denied")
        yield

    for target in (AlarmMode.DISARMED, AlarmMode.HOME, AlarmMode.AWAY):
        await transport.send_mode("asset", "panel", target, epoch=1, admission=allow)
    assert len(sent) == 3 and all("bypass" not in frame for frame in sent)
    with pytest.raises(AlarmCommandSupersededError):
        await transport.send_mode("asset", "panel", AlarmMode.HOME, epoch=1, admission=deny)
    with pytest.raises(AlarmTransportError):
        await transport.send_mode("asset", "panel", AlarmMode.HOME, epoch=1, admission=None)
    with pytest.raises(AlarmTransportError):
        await transport._send_encoded(
            lambda sequence: encode_mode_command("asset", "panel", AlarmMode.HOME, sequence),
            epoch=1,
            admission=allow,
        )
    assert len(sent) == 3


@pytest.mark.parametrize("mode", ["home", "panic", AlarmMode.UNKNOWN, True])
def test_mode_encoder_rejects_non_normal_enum(mode):
    with pytest.raises(ValueError):
        encode_mode_command("asset", "panel", mode, 1)


@pytest.mark.asyncio
async def test_lighting_bridge_ownership_cannot_authorize_alarm():
    instance = await setup(owned=False)
    try:
        instance._gateway.devices["beams_bridges"] = [
            {"id": 123, "location_id": "home", "owned": True}
        ]
        await instance.refresh()
        assert snapshot(instance)["authorization"] == "unknown"
        assert (await command(instance))["code"] == "authorization-unknown"
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_confirmation_before_socket_return_is_retained_without_optimistic_state():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    try:
        assert (await command(instance, "home"))["status"] == "confirmed"
        assert transport.sent == [("asset", "panel", AlarmMode.HOME, transport.epoch)]
        before = len(transport.sent)
        assert (await command(instance, "home"))["code"] == "already-set"
        assert len(transport.sent) == before
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_cancellation_before_admission_is_safe_and_releases_location():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    queued = asyncio.Event()

    async def stall():
        queued.set()
        await asyncio.Event().wait()

    transport.before_send = stall
    try:
        task = asyncio.create_task(command(instance))
        await queued.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not snapshot(instance)["pending"]
        assert not snapshot(instance)["outcome_unknown"]
        assert not transport.sent
        transport.before_send = None
        assert (await command(instance))["status"] == "confirmed"
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_inflight_close_finishes_uncertain_without_waiting_full_timeout():
    instance = await setup()
    instance._alarm_commands.timeout = 10
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"
    task = asyncio.create_task(command(instance))
    await transport.sent_event.wait()
    await instance.close()
    result = await asyncio.wait_for(task, 0.2)
    assert result["status"] == "uncertain" and result["possibly_sent"]
    assert not instance._alarm_commands._pending


@pytest.mark.asyncio
async def test_uncertainty_requires_explicit_mode_in_full_inventory():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"
    try:
        assert (await command(instance))["status"] == "uncertain"
        await transport.message(
            DeviceInfoDocList(
                "asset",
                (
                    DeviceDocument(
                        "panel",
                        {"zid": "panel", "deviceType": "security-panel", "commStatus": "ok"},
                    ),
                ),
            )
        )
        assert snapshot(instance)["outcome_unknown"]
        await transport.message(DeviceInfoDocList("asset", (panel(),)))
        assert not snapshot(instance)["outcome_unknown"]
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_command_budget_is_total_across_send_and_observation():
    instance = await setup()
    instance._alarm_commands.timeout = 0.1
    transport = instance._runtimes["home"].transport
    transport.behavior = "silent"

    async def slow_queue():
        await asyncio.sleep(0.055)

    transport.before_send = slow_queue
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        assert (await command(instance))["status"] == "uncertain"
        assert loop.time() - start < 0.145  # Separate restarted waits would exceed 0.15.
        assert len(transport.sent) == 1
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_panel_revision_token_does_not_change_for_unrelated_sensor_patch():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    try:
        before = snapshot(instance)
        await transport.message(
            DeviceInfoDocUpdate(
                "asset",
                (
                    DeviceDocument(
                        "contact",
                        {"zid": "contact", "deviceType": "sensor.contact", "faulted": False},
                    ),
                ),
            )
        )
        assert snapshot(instance)["revision"] == before["revision"]
        assert (await command(instance, before=before))["status"] == "confirmed"
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_duplicate_security_panels_fail_closed():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    try:
        original = snapshot(instance)
        await transport.message(
            DeviceInfoDocUpdate(
                "asset",
                (
                    DeviceDocument(
                        "panel-two",
                        {"zid": "panel-two", "deviceType": "security-panel", "mode": "none"},
                    ),
                ),
            )
        )
        alarms = instance._alarm_commands.snapshots()
        assert len(alarms) == 2 and all(item["reason"] == "multiple-panels" for item in alarms)
        assert (await command(instance, before=original))["status"] == "rejected"
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_mixed_base_station_role_evidence_stays_unknown():
    instance = await setup()
    try:
        instance._gateway.devices["base_stations"].append(
            {"id": 2, "location_id": "home", "owned": False}
        )
        await instance.refresh()
        assert snapshot(instance)["authorization"] == "unknown"
        assert (await command(instance))["code"] == "authorization-unknown"
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["timeout", "cancel"])
async def test_real_transport_retires_socket_after_ambiguous_mode_send(action):
    started = asyncio.Event()
    closed = asyncio.Event()

    async def send(_frame):
        started.set()
        await asyncio.Event().wait()

    async def close():
        closed.set()

    @contextmanager
    def allow():
        yield

    transport = RingAlarmTransport(None, AlarmLocationSeed("home"), send_timeout=0.01)
    transport._websocket = SimpleNamespace(closed=False, send_str=send, close=close)
    transport._connection_epoch = 1
    transport._assets = (AlarmTicketAsset("asset", "base_station_v1", "online"),)
    task = asyncio.create_task(
        transport.send_mode("asset", "panel", AlarmMode.HOME, epoch=1, admission=allow)
    )
    await started.wait()
    if action == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(AlarmTransportError, match="websocket-send-timeout"):
            await task
    assert closed.is_set() and transport._websocket is None and transport._connection_epoch is None


@pytest.mark.asyncio
async def test_real_transport_revalidates_admission_after_lock_and_rejects_lighting_target():
    sent = []
    permitted = [True]

    async def send(frame):
        sent.append(frame)

    @contextmanager
    def guard():
        if not permitted[0]:
            raise AlarmCommandSupersededError("revoked")
        yield

    transport = RingAlarmTransport(None, AlarmLocationSeed("home"))
    transport._websocket = SimpleNamespace(closed=False, send_str=send)
    transport._connection_epoch = 1
    transport._assets = (AlarmTicketAsset("asset", "base_station_v1", "online"),)
    await transport._send_lock.acquire()
    task = asyncio.create_task(
        transport.send_mode("asset", "panel", AlarmMode.HOME, epoch=1, admission=guard)
    )
    await asyncio.sleep(0)
    permitted[0] = False
    transport._send_lock.release()
    with pytest.raises(AlarmCommandSupersededError):
        await task
    assert not sent and transport._websocket is not None
    permitted[0] = True
    transport._assets = (AlarmTicketAsset("asset", "beams_bridge_v1", "online"),)
    with pytest.raises(AlarmTransportError, match="operation-not-allowed"):
        await transport.send_mode("asset", "panel", AlarmMode.HOME, epoch=1, admission=guard)
    assert not sent


@pytest.mark.asyncio
async def test_readback_marker_requires_full_known_current_panel_inventory():
    instance = await setup()
    runtime = instance._runtimes["home"]
    transport = runtime.transport
    try:
        original = snapshot(instance)["readback_revision"]
        assert original > 0
        await transport.message(
            DeviceInfoDocUpdate(
                "asset", (DeviceDocument("panel", {"zid": "panel", "batteryLevel": 70}),)
            )
        )
        assert snapshot(instance)["readback_revision"] == original
        await transport.message(DeviceInfoDocUpdate("asset", (panel("some"),)))
        assert snapshot(instance)["readback_revision"] == original
        await transport.message(DeviceInfoDocList("other-asset", (panel("some"),)))
        assert snapshot(instance)["readback_revision"] == original
        await transport.message(DeviceInfoDocList("asset", (panel("some"),)))
        fresh = snapshot(instance)["readback_revision"]
        assert fresh > original
        await transport.message(DeviceInfoDocList("asset", (panel("not-known"),)))
        assert snapshot(instance)["readback_revision"] == fresh
        await transport.message(DeviceInfoDocList("asset", (panel(commStatus="down"),)))
        assert snapshot(instance)["readback_revision"] == fresh
        runtime.epoch = instance._store.begin_epoch("home")
        await instance._publish()
        assert snapshot(instance)["readback_revision"] == 0
        await instance._opened(
            runtime, runtime.epoch, (AlarmTicketAsset("asset", "base_station_v1", "online"),)
        )
        await instance._message(runtime, runtime.epoch, DeviceInfoDocList("asset", (panel(),)))
        assert snapshot(instance)["readback_revision"] > fresh
    finally:
        await instance.close()


def sensor(zid="contact", *, faulted=False, comm="ok", kind="sensor.contact"):
    values = {"zid": zid, "deviceType": kind, "commStatus": comm}
    if faulted is not None:
        values["faulted"] = faulted
    return DeviceDocument(zid, values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sensors,expected",
    [
        ((sensor(),), "confirmed"),
        ((sensor(), sensor("motion", kind="sensor.motion")), "confirmed"),
        ((sensor(faulted=True),), "sensors-require-bypass"),
        ((sensor(faulted=None),), "faulted-device-list-unknown"),
        ((sensor(comm="down"),), "faulted-device-list-unknown"),
        ((sensor(comm="unrecognized"),), "faulted-device-list-unknown"),
        ((sensor(), sensor("unknown", kind="future-sensor")), "faulted-device-list-unknown"),
        ((), "faulted-device-list-unknown"),
    ],
)
async def test_optional_fault_list_uses_only_complete_fresh_sensor_evidence(sensors, expected):
    instance = await setup()
    transport = instance._runtimes["home"].transport
    try:
        await transport.message(
            DeviceInfoDocList(
                "asset",
                (
                    panel(alarmInfo={"state": "none"}),
                    *sensors,
                ),
            )
        )
        result = await command(instance)
        assert (result["status"] if result["status"] == "confirmed" else result["code"]) == expected
        assert len(transport.sent) == (1 if expected == "confirmed" else 0)
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_optional_fault_list_rechecks_sensor_fault_at_send_boundary():
    instance = await setup()
    transport = instance._runtimes["home"].transport
    try:
        await transport.message(
            DeviceInfoDocList(
                "asset",
                (
                    panel(alarmInfo={"state": "none"}),
                    sensor(),
                ),
            )
        )

        async def open_door():
            await transport.message(DeviceInfoDocUpdate("asset", (sensor(faulted=True),)))

        transport.before_send = open_door
        assert (await command(instance))["status"] == "rejected"
        assert not transport.sent
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_optional_fault_list_requires_fresh_fault_receipt_not_battery_refresh():
    now = [100.0]
    instance = await setup(clock=lambda: now[0])
    transport = instance._runtimes["home"].transport
    try:
        await transport.message(
            DeviceInfoDocList(
                "asset",
                (
                    panel(alarmInfo={"state": "none"}),
                    sensor(),
                ),
            )
        )
        now[0] += 121
        await transport.message(
            DeviceInfoDocUpdate(
                "asset", (DeviceDocument("contact", {"zid": "contact", "batteryLevel": 50}),)
            )
        )
        assert (await command(instance))["code"] == "faulted-device-list-unknown"
        assert not transport.sent
    finally:
        await instance.close()
