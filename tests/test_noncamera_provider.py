"""No-account monitoring qualification: no actuator, pairing, or real network."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from halo_server.devices.provider import NoncameraProvider, ReadOnlyDeviceError
from halo_server.ring.hub.models import AlarmWriteAuthorization
from halo_server.ring.hub.protocol import DeviceDocument, DeviceInfoDocList, DeviceInfoDocUpdate
from halo_server.ring.hub.provider import AlarmLocationSeed
from halo_server.ring.hub.rest import DEVICES_ENDPOINT, AlarmRestError, RingAlarmRestGateway
from halo_server.ring.hub.transport import AlarmTicketAsset, AlarmTransportError, RingAlarmTransport
from halo_server.ring.json_transport import RingJsonError, RingJsonTransport

PROFILE = "11111111-1111-4111-8111-111111111111"


class Gateway:
    def __init__(self, devices=None, locations=("home",)):
        self.devices = devices or {}
        self.locations = {
            "user_locations": [{"location_id": item, "name": item} for item in locations]
        }
        self.error = None
        self.closed = False
        self.calls = []

    async def request_json(self, url, **_kwargs):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.devices if url == DEVICES_ENDPOINT else self.locations

    async def close(self):
        self.closed = True


class Transport:
    instances = []

    def __init__(self, gateway, seed):
        self.seed = seed
        self.connected = False
        self.closed = False
        self.requests = []
        self.ready = asyncio.Event()
        self.message = None
        self.assets = (AlarmTicketAsset("asset-" + seed.location_id, "base_station_v1", "online"),)
        self.instances.append(self)

    async def connect_once(self, *, epoch, stop_event, on_open, on_message):
        self.connected = True
        self.epoch = epoch
        self.message = on_message
        await on_open(self.assets)
        await on_message(
            DeviceInfoDocList(
                self.assets[0].asset_id,
                (
                    DeviceDocument(
                        "sensor",
                        {
                            "zid": "sensor",
                            "deviceType": "sensor.contact",
                            "name": "Door",
                            "faulted": False,
                            "commStatus": "ok",
                            "batteryLevel": 75,
                        },
                    ),
                ),
            )
        )
        self.ready.set()
        await stop_event.wait()

    async def request_inventory(self, asset, **kwargs):
        self.requests.append((asset, kwargs))

    async def close(self):
        self.closed = True
        self.connected = False


def provider(gateway, **kwargs):
    return NoncameraProvider(
        None,
        profile_id=PROFILE,
        account_generation=1,
        gateway=gateway,
        transport_factory=Transport,
        **kwargs,
    )


def records_by_kind(instance):
    return {record.kind: record for record in instance.snapshot()}


def observation(record, name):
    return next(value for value in record.observations if value.name == name)


@pytest.mark.asyncio
async def test_discovery_unions_alarm_and_lighting_locations_and_excludes_cameras():
    batches = []
    gateway = Gateway(
        {
            "base_stations": [{"id": 1, "location_id": "home", "owned": True}],
            "beams_bridges": [{"id": 2, "location_id": "garden"}],
            "chimes": [{"id": 3, "location_id": "home", "description": "Hall"}],
            "doorbots": [{"id": 4, "location_id": "home"}],
            "future_devices": [{"id": 5, "location_id": "home", "kind": "new-model"}],
        },
        ("home", "garden"),
    )
    instance = provider(gateway, on_snapshot=lambda **batch: batches.append(batch))
    try:
        await instance.refresh()
        await asyncio.gather(
            *(runtime.transport.ready.wait() for runtime in instance._runtimes.values())
        )
        assert set(instance._runtimes) == {"home", "garden"}
        assert (
            instance._runtimes["home"].seed.write_authorization is AlarmWriteAuthorization.ALLOWED
        )
        assert (
            instance._runtimes["garden"].seed.write_authorization is AlarmWriteAuthorization.UNKNOWN
        )
        kinds = records_by_kind(instance)
        assert {"base-station", "lighting-bridge", "chime", "contact-sensor", "unknown"} <= set(
            kinds
        )
        assert all(record.family != "doorbots" for record in instance.snapshot())
        assert kinds["unknown"].supported is False
        assert observation(kinds["contact-sensor"], "contact_open").value is False
        assert batches[-1]["complete"] is True
        assert all(batch["revision"] == index + 1 for index, batch in enumerate(batches))
    finally:
        await instance.close()
    assert gateway.closed
    assert all(runtime.closed for runtime in Transport.instances)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, True, "", "a" * 129, "bad\nvalue", "unknown-location"])
async def test_invalid_discovery_never_removes_known_devices(bad):
    gateway = Gateway(
        {"chimes": [{"id": 1, "location_id": "home", "alerts": {"connection": "online"}}]}
    )
    instance = provider(gateway)
    try:
        await instance.refresh()
        old_key = instance.snapshot()[0].key
        gateway.devices = {"chimes": [{"id": 2, "location_id": bad}]}
        await instance.refresh()
        assert instance.snapshot()[0].key == old_key
        assert instance.snapshot()[0].source_health == "stale"
        assert instance.status()["discovery_complete"] is False
        assert observation(instance.snapshot()[0], "connected").status == "stale"
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_permission_failure_is_sanitized_and_commands_never_reach_network():
    gateway = Gateway({"chimes": [{"id": 1, "location_id": "home"}]})
    instance = provider(gateway)
    try:
        await instance.refresh()
        gateway.error = AlarmRestError("permission-denied", status=403)
        await instance.refresh()
        assert instance.status()["error"] == "permission-denied"
        assert len(instance.snapshot()) == 1
        calls = len(gateway.calls)
        for action in ("arm", "disarm", "panic", "fire", "siren", "unlock", "bypass", "switch"):
            with pytest.raises(ReadOnlyDeviceError):
                await instance.execute(action=action)
        assert len(gateway.calls) == calls
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_late_account_result_is_fenced_and_shutdown_is_idempotent():
    gate = asyncio.Event()
    entered = asyncio.Event()
    active = True
    gateway = Gateway()
    original = gateway.request_json

    async def delayed(*args, **kwargs):
        entered.set()
        await gate.wait()
        return await original(*args, **kwargs)

    gateway.request_json = delayed
    batches = []
    instance = provider(
        gateway, admission=lambda: active, on_snapshot=lambda **batch: batches.append(batch)
    )
    task = asyncio.create_task(instance.refresh())
    await entered.wait()
    active = False
    gate.set()
    await task
    assert batches == []
    assert instance.snapshot() == ()
    await instance.close()
    await instance.close()


@pytest.mark.asyncio
async def test_bad_location_cannot_stop_good_location_and_unlisted_assets_ignored():
    gateway = Gateway(
        {"base_stations": [{"id": 1, "location_id": "home"}, {"id": 2, "location_id": "other"}]},
        ("home", "other"),
    )

    class Isolated(Transport):
        async def connect_once(self, **kwargs):
            if self.seed.location_id == "other":
                self.ready.set()
                raise AlarmTransportError("authentication-required")
            return await super().connect_once(**kwargs)

    instance = NoncameraProvider(
        None, profile_id=PROFILE, account_generation=1, gateway=gateway, transport_factory=Isolated
    )
    try:
        await instance.refresh()
        await asyncio.gather(
            *(runtime.transport.ready.wait() for runtime in instance._runtimes.values())
        )
        home = instance._runtimes["home"].transport
        assert (
            observation(records_by_kind(instance)["contact-sensor"], "contact_open").value is False
        )
        await home.message(
            DeviceInfoDocList(
                "unauthorized-asset",
                (DeviceDocument("other", {"zid": "other", "deviceType": "sensor.contact"}),),
            )
        )
        assert all(
            record.identity.asset_id != "unauthorized-asset" for record in instance.snapshot()
        )
        assert instance._runtimes["other"].error == "authentication-required"
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_per_value_receipts_not_refreshed_by_unrelated_patch_and_disconnect_stales():
    clock = [100.0]
    gateway = Gateway({"base_stations": [{"id": 1, "location_id": "home"}]})
    instance = provider(gateway, clock=lambda: clock[0])
    try:
        await instance.refresh()
        runtime = instance._runtimes["home"]
        await runtime.transport.ready.wait()
        clock[0] = 200.0
        await runtime.transport.message(
            DeviceInfoDocUpdate(
                "asset-home", (DeviceDocument("sensor", {"zid": "sensor", "faulted": True}),)
            )
        )
        sensor = records_by_kind(instance)["contact-sensor"]
        assert observation(sensor, "contact_open").observed_at == 200.0
        assert observation(sensor, "battery_percent").observed_at == 100.0
        assert observation(sensor, "smoke_detected").value is None
        instance._store.mark_location_stale("home", runtime.epoch)
        await instance._publish()
        sensor = records_by_kind(instance)["contact-sensor"]
        assert observation(sensor, "contact_open").status == "stale"
        assert observation(sensor, "contact_open").value is True
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_start_returns_without_waiting_on_discovery_and_close_retires_work():
    gateway = Gateway()
    entered = asyncio.Event()

    async def stalled(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    gateway.request_json = stalled
    instance = provider(gateway)
    await asyncio.wait_for(instance.start(), timeout=0.2)
    await entered.wait()
    await asyncio.wait_for(instance.close(), timeout=0.2)
    assert instance._task.done()


@pytest.mark.asyncio
async def test_clap_low_level_sender_allows_only_inventory_even_if_called_directly():
    transport = RingAlarmTransport(None, AlarmLocationSeed("home"))
    sent = []

    async def send(frame):
        sent.append(json.loads(frame))

    transport._websocket = SimpleNamespace(closed=False, send_str=send)
    transport._connection_epoch = 1
    transport._assets = (AlarmTicketAsset("asset", "base_station_v1", "online"),)
    # Generic encoded operations remain read-only; normal modes require the
    # separate typed method and an explicit send-boundary admission guard.
    await transport.request_inventory("asset", epoch=1)
    for kind in ("DeviceInfoSet", "HubDisconnectionEvent", "security-panel.switch-mode"):
        with pytest.raises(AlarmTransportError, match="operation-not-allowed"):
            await transport._send_encoded(
                lambda seq, kind=kind: {
                    "channel": "message",
                    "msg": {"msg": kind, "dst": "asset", "seq": seq},
                },
                epoch=1,
            )
    with pytest.raises(AlarmTransportError, match="operation-not-allowed"):
        await transport.request_inventory("not-in-ticket", epoch=1)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_http_core_and_gateway_deny_writes_and_noninventory_routes():
    auth = SimpleNamespace()
    core = RingJsonTransport(auth)
    gateway = RingAlarmRestGateway(auth, transport=core)
    with pytest.raises(RingJsonError, match="operation-not-allowed"):
        await core.request_write(DEVICES_ENDPOINT, method="POST", json={})
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(AlarmRestError, match="operation-not-allowed"):
            await gateway.request_json(DEVICES_ENDPOINT, method=method)
    for url in (
        "https://api.ring.com/clients_api/doorbots/1/siren_on",
        "https://api.ring.com/devices/v1/locations?redirect=other",
        "https://evil.test/clients_api/ring_devices",
    ):
        with pytest.raises(RingJsonError, match="endpoint-not-allowed"):
            await core.request_json(url)
    await gateway.close()
    await core.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connected,health", [(False, "offline"), (None, "unknown"), (True, "online")]
)
async def test_rest_device_connectivity_is_not_inferred_from_successful_http(connected, health):
    from halo_server.devices.catalog import DeviceCatalog

    gateway = Gateway(
        {"chimes": [{"id": 1, "location_id": "home", "alerts": {"connection": health}}]}
    )
    instance = provider(gateway, clock=lambda: 100.0)
    try:
        await instance.refresh()
        record = instance.snapshot()[0]
        assert record.source_health == health
        catalog = DeviceCatalog(PROFILE, 1, 1)
        catalog.apply(instance.snapshot(), complete=True, revision=1, observed_at=100.0)
        row = catalog.snapshot(now=100.0)["devices"][0]
        assert row["available"] is connected
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_complete_empty_inventory_requires_valid_envelope_and_retires_locations():
    gateway = Gateway({"base_stations": [{"id": 1, "location_id": "home"}]})
    batches = []
    instance = provider(gateway, on_snapshot=lambda **batch: batches.append(batch))
    try:
        await instance.refresh()
        runtime = instance._runtimes["home"]
        await runtime.transport.ready.wait()
        gateway.devices = {}
        await instance.refresh()
        assert instance._runtimes["home"] is runtime
        assert batches[-1]["complete"] is False
        gateway.devices = {"doorbots": [], "stickup_cams": [], "chimes": []}
        await instance.refresh()
        assert not instance._runtimes
        assert runtime.transport.closed
        assert instance.snapshot() == ()
        assert batches[-1]["complete"] is True
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_old_connection_callback_cannot_overwrite_new_epoch():
    gateway = Gateway({"base_stations": [{"id": 1, "location_id": "home"}]})
    instance = provider(gateway)
    try:
        await instance.refresh()
        runtime = instance._runtimes["home"]
        await runtime.transport.ready.wait()
        old_callback = runtime.transport.message
        runtime.epoch = instance._store.begin_epoch("home")
        await old_callback(
            DeviceInfoDocUpdate(
                "asset-home", (DeviceDocument("sensor", {"zid": "sensor", "faulted": True}),)
            )
        )
        assert (
            observation(records_by_kind(instance)["contact-sensor"], "contact_open").value is False
        )
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_concurrent_close_waiters_both_wait_until_owned_resources_retire():
    gateway = Gateway()
    entered = asyncio.Event()
    released = asyncio.Event()

    async def delayed_close():
        entered.set()
        await released.wait()

    gateway.close = delayed_close
    instance = provider(gateway)
    first = asyncio.create_task(instance.close())
    await entered.wait()
    second = asyncio.create_task(instance.close())
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    released.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_hub_inventory_limit_is_rejected_before_retaining_or_truncating_state():
    gateway = Gateway({"base_stations": [{"id": 1, "location_id": "home"}]})
    instance = provider(gateway)
    try:
        await instance.refresh()
        runtime = instance._runtimes["home"]
        await runtime.transport.ready.wait()
        before = instance.snapshot()
        oversized = DeviceInfoDocList(
            "asset-home",
            tuple(
                DeviceDocument(
                    f"sensor-{index}", {"zid": f"sensor-{index}", "deviceType": "sensor.contact"}
                )
                for index in range(1024)
            ),
        )
        with pytest.raises(AlarmTransportError, match="device-limit-exceeded"):
            await runtime.transport.message(oversized)
        assert instance.snapshot() == before
        assert len(instance._observation_times) == 1
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_confirmed_source_removal_is_unavailable_while_other_location_is_offline():
    from halo_server.devices.catalog import DeviceCatalog

    gateway = Gateway(
        {
            "base_stations": [{"id": 1, "location_id": "home"}, {"id": 2, "location_id": "other"}],
            "chimes": [{"id": 3, "location_id": "home", "alerts": {"connection": "online"}}],
        },
        ("home", "other"),
    )

    class Isolated(Transport):
        async def connect_once(self, **kwargs):
            if self.seed.location_id == "other":
                self.ready.set()
                raise AlarmTransportError("authentication-required")
            return await super().connect_once(**kwargs)

    catalog = DeviceCatalog(PROFILE, 1, 1)
    instance = NoncameraProvider(
        None,
        profile_id=PROFILE,
        account_generation=1,
        gateway=gateway,
        transport_factory=Isolated,
        clock=lambda: 100.0,
        on_snapshot=lambda alarms, lights, chimes, **snapshot: catalog.apply(**snapshot),
    )
    try:
        await instance.refresh()
        await asyncio.gather(
            *(runtime.transport.ready.wait() for runtime in instance._runtimes.values())
        )
        sensor = records_by_kind(instance)["contact-sensor"]
        chime = records_by_kind(instance)["chime"]
        before = {row["id"]: row for row in catalog.snapshot(now=100.0)["devices"]}
        assert before[sensor.key]["available"] is True
        assert before[chime.key]["available"] is True
        assert catalog.complete is False

        # Exercise another location publishing while removal awaits socket close.
        old_close = instance._runtimes["home"].transport.close

        async def publish_during_close():
            await instance._publish()
            await old_close()

        instance._runtimes["home"].transport.close = publish_during_close

        # This REST result is authoritative for the removed source even though
        # the surviving location's failed ticket keeps the overall batch partial.
        gateway.devices = {"base_stations": [{"id": 2, "location_id": "other"}], "chimes": []}
        await instance.refresh()
        rows = {row["id"]: row for row in catalog.snapshot(now=100.0)["devices"]}
        assert catalog.complete is False
        assert rows[sensor.key]["available"] is False
        assert rows[chime.key]["available"] is False
        removed_sensor = next(record for record in instance.snapshot() if record.key == sensor.key)
        assert removed_sensor.unavailable_reason == "source-removed"
        assert observation(removed_sensor, "contact_open").status == "stale"
        assert observation(removed_sensor, "contact_open").value is False

        # A restored source supersedes the tombstone without changing identity.
        gateway.devices = {
            "base_stations": [{"id": 1, "location_id": "home"}, {"id": 2, "location_id": "other"}]
        }
        await instance.refresh()
        await instance._runtimes["home"].transport.ready.wait()
        restored = {row["id"]: row for row in catalog.snapshot(now=100.0)["devices"]}
        assert restored[sensor.key]["available"] is True

        # Once every location is authoritatively absent, ordinary complete
        # reconciliation removes tombstones from the published provider batch.
        gateway.devices = {"doorbots": [], "chimes": []}
        await instance.refresh()
        assert catalog.complete is True
        assert instance.snapshot() == ()
        assert not instance._retired_records
    finally:
        await instance.close()
