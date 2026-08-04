import asyncio
from types import SimpleNamespace

import pytest
import ring_doorbell

from halo_server.ring.events import (
    EventConnectionState,
    EventListener,
    EventNormalizationError,
    RingEventType,
    normalize_ring_event,
)


def _event(**overrides):
    values = {
        "id": 101,
        "doorbot_id": 42,
        "device_name": "Front Door",
        "device_kind": "lpd_v4",
        "now": 1_700_000_000.5,
        "expires_in": 180,
        "kind": "motion",
        "state": "active",
        "is_update": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_normalize_motion_event_uses_stable_serializable_identity():
    normalized = normalize_ring_event(_event(), clock=lambda: 12.5)

    assert normalized.event_id == "101"
    assert normalized.camera_id == "42"
    assert normalized.camera_name == "Front Door"
    assert normalized.camera_kind == "lpd_v4"
    assert normalized.type is RingEventType.MOTION
    assert normalized.source_kind == "motion"
    assert normalized.occurred_at == 1_700_000_000.5
    assert normalized.received_at == 12.5
    assert normalized.source_expires_in == 180
    assert normalized.is_update is False


def test_normalize_maps_ring_ding_to_doorbell_without_losing_source_kind():
    normalized = normalize_ring_event(
        _event(id="ding-1", kind="DING", is_update=True),
        clock=lambda: 1,
    )

    assert normalized.type is RingEventType.DOORBELL
    assert normalized.source_kind == "ding"
    assert normalized.is_update is True


def test_normalize_preserves_unknown_event_as_other():
    normalized = normalize_ring_event(_event(kind="alarm"), clock=lambda: 1)

    assert normalized.type is RingEventType.OTHER
    assert normalized.source_kind == "alarm"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": None}, "event identifier"),
        ({"doorbot_id": ""}, "camera identifier"),
        ({"kind": ""}, "event kind"),
        ({"now": float("nan")}, "event timestamp"),
        ({"expires_in": -1}, "expiry duration"),
    ],
)
def test_normalize_rejects_malformed_identity_and_time(overrides, message):
    with pytest.raises(EventNormalizationError, match=message):
        normalize_ring_event(_event(**overrides), clock=lambda: 1)


@pytest.mark.asyncio
async def test_listener_normalizes_events_and_reports_state(monkeypatch):
    received = []
    states = []
    holders = {}

    class FakeConfig:
        connection_retry_count = 0
        abort_on_sequential_error_count = 1

        @classmethod
        def default_config(cls):
            return cls()

    class FakeSdkListener:
        def __init__(self, ring, *, config):
            assert ring == "ring"
            assert config.connection_retry_count == 1_000_000
            assert config.abort_on_sequential_error_count is None
            self.callback = None
            self.stopped = False
            self._receiver = SimpleNamespace(run_state=SimpleNamespace(name="STARTED"))
            holders["sdk"] = self

        def add_notification_callback(self, callback):
            self.callback = callback

        async def start(self):
            self.callback(_event(kind="ding"))
            listener._stop.set()
            return True

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(ring_doorbell, "RingEventListenerConfig", FakeConfig)
    monkeypatch.setattr(ring_doorbell, "RingEventListener", FakeSdkListener)
    client = SimpleNamespace(ring="ring")
    listener = EventListener(client, received.append, states.append)

    await listener._run()

    assert [event.type for event in received] == [RingEventType.DOORBELL]
    assert states == [
        EventConnectionState.CONNECTING,
        EventConnectionState.CONNECTED,
        EventConnectionState.OFFLINE,
    ]
    assert holders["sdk"].stopped is True


@pytest.mark.asyncio
async def test_listener_retries_failed_start_and_stops_each_sdk_listener(monkeypatch):
    instances = []
    states = []

    class FakeConfig:
        @classmethod
        def default_config(cls):
            return cls()

    class FakeSdkListener:
        def __init__(self, _ring, *, config):
            self.config = config
            self.callback = None
            self.stopped = False
            instances.append(self)

        def add_notification_callback(self, callback):
            self.callback = callback

        async def start(self):
            if len(instances) == 1:
                return False
            listener._stop.set()
            return True

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(ring_doorbell, "RingEventListenerConfig", FakeConfig)
    monkeypatch.setattr(ring_doorbell, "RingEventListener", FakeSdkListener)
    monkeypatch.setattr("halo_server.ring.events.RECONNECT_MIN", 0)
    client = SimpleNamespace(ring="ring")
    listener = EventListener(client, lambda _event: None, states.append)

    await asyncio.wait_for(listener._run(), timeout=1)

    assert len(instances) == 2
    assert all(instance.stopped for instance in instances)
    assert states[-1] is EventConnectionState.OFFLINE


def test_start_is_idempotent_and_stop_waits_for_owned_future():
    class FakeFuture:
        def __init__(self):
            self.result_calls = 0

        def done(self):
            return False

        def result(self, *, timeout):
            assert timeout > 0
            self.result_calls += 1

    class FakeClient:
        _loop_thread = None

        def __init__(self):
            self.submissions = []
            self.future = FakeFuture()

        def submit(self, coroutine):
            self.submissions.append(coroutine)
            coroutine.close()
            return self.future

    client = FakeClient()
    listener = EventListener(client, lambda _event: None)

    listener.start()
    listener.start()
    listener.stop()

    assert len(client.submissions) == 1
    assert client.future.result_calls == 1
    assert listener.state is EventConnectionState.OFFLINE


def test_malformed_event_is_ignored_without_invoking_callback():
    received = []
    listener = EventListener(SimpleNamespace(), received.append)

    listener._emit(_event(id=None))

    assert received == []
