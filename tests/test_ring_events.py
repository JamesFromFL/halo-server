import asyncio
import concurrent.futures
from types import SimpleNamespace

import pytest
import ring_doorbell
from ring_doorbell.const import (
    PUSH_ACTION_DING,
    PUSH_ACTION_MOTION,
    PUSH_NOTIFICATION_DING,
    PUSH_NOTIFICATION_MOTION,
)

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


@pytest.mark.parametrize(
    ("source_kind", "event_type"),
    [
        (PUSH_ACTION_DING, RingEventType.DOORBELL),
        (PUSH_ACTION_MOTION, RingEventType.MOTION),
        (PUSH_NOTIFICATION_DING, RingEventType.DOORBELL),
        (PUSH_NOTIFICATION_MOTION, RingEventType.MOTION),
    ],
)
def test_normalize_maps_exact_ring_push_aliases(source_kind, event_type):
    normalized = normalize_ring_event(_event(kind=source_kind), clock=lambda: 1)

    assert normalized.type is event_type
    assert normalized.source_kind == source_kind.casefold()


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
        def __init__(
            self,
            ring,
            credentials=None,
            credentials_updated_callback=None,
            *,
            config,
        ):
            assert ring == "ring"
            assert credentials is None
            assert callable(credentials_updated_callback)
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
        def __init__(
            self,
            _ring,
            _credentials=None,
            _credentials_updated_callback=None,
            *,
            config,
        ):
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


@pytest.mark.asyncio
async def test_listener_retains_updated_credentials_when_persistence_fails(monkeypatch, caplog):
    instances = []
    first_credentials = {"gcm": {"android_id": "original-secret"}}
    replacement = {"gcm": {"android_id": "replacement-secret"}}

    class FakeConfig:
        @classmethod
        def default_config(cls):
            return cls()

    class FakeSdkListener:
        def __init__(
            self,
            _ring,
            credentials=None,
            credentials_updated_callback=None,
            *,
            config,
        ):
            self.credentials = credentials
            self.credentials_updated_callback = credentials_updated_callback
            self.config = config
            self.stopped = False
            instances.append(self)

        def add_notification_callback(self, _callback):
            pass

        async def start(self):
            if len(instances) == 1:
                self.credentials_updated_callback(replacement)
                return False
            listener._stop.set()
            return True

        async def stop(self):
            self.stopped = True

    def fail_persistence(_credentials):
        raise RuntimeError("must-not-appear-secret")

    monkeypatch.setattr(ring_doorbell, "RingEventListenerConfig", FakeConfig)
    monkeypatch.setattr(ring_doorbell, "RingEventListener", FakeSdkListener)
    monkeypatch.setattr("halo_server.ring.events.RECONNECT_MIN", 0)
    listener = EventListener(
        SimpleNamespace(ring="ring"),
        lambda _event: None,
        credentials=first_credentials,
        credentials_updated_callback=fail_persistence,
    )

    await asyncio.wait_for(listener._run(), timeout=1)

    assert instances[0].credentials == first_credentials
    assert instances[1].credentials == replacement
    assert all(instance.stopped for instance in instances)
    assert "must-not-appear-secret" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_listener_ignores_invalid_credential_callback_without_logging_value(caplog):
    persisted = []
    listener = EventListener(
        SimpleNamespace(),
        lambda _event: None,
        credentials_updated_callback=persisted.append,
    )

    listener._credentials_updated({"secret-value": object()})

    assert persisted == []
    assert listener._credentials is None
    assert "secret-value" not in caplog.text


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


def test_listener_startup_wait_surfaces_submitted_task_failure():
    failure = RuntimeError("injected listener bootstrap failure")

    class FakeClient:
        _loop_thread = None

        def submit(self, coroutine):
            coroutine.close()
            future = concurrent.futures.Future()
            future.set_exception(failure)
            return future

    listener = EventListener(FakeClient(), lambda _event: None)

    future = listener.start()

    assert future.done() is True
    assert listener.running is False
    with pytest.raises(RuntimeError, match="injected listener bootstrap failure"):
        listener.wait_until_started(timeout=0.01)
    listener.stop()


def test_malformed_event_is_ignored_without_invoking_callback():
    received = []
    listener = EventListener(SimpleNamespace(), received.append)

    listener._emit(_event(id=None))

    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", ["STOPPING", "STOPPED"])
@pytest.mark.parametrize("owner_shutdown", [False, True], ids=["recover", "owner-shutdown"])
async def test_listener_restarts_terminal_receiver_unless_owner_stops(
    monkeypatch, terminal_state, owner_shutdown
):
    instances = []
    operations = []
    received = []
    original_credentials = {"gcm": {"android_id": "original"}}
    replacement_credentials = {"gcm": {"android_id": "replacement"}}

    class FakeSdkListener:
        def __init__(self, _ring, credentials, credentials_updated, *, config):
            self.index = len(instances)
            self.credentials = credentials
            self.credentials_updated = credentials_updated
            self._receiver = SimpleNamespace(run_state=SimpleNamespace(name=terminal_state))
            instances.append(self)
            operations.append((self.index, "create"))

        def add_notification_callback(self, callback):
            self.callback = callback

        async def start(self):
            operations.append((self.index, "start"))
            if self.index == 0:
                self.credentials_updated(replacement_credentials)
                if owner_shutdown:
                    listener._stop.set()
            else:
                self.callback(_event())
                listener._stop.set()
            return True

        async def stop(self):
            operations.append((self.index, "stop"))

    monkeypatch.setattr(ring_doorbell, "RingEventListener", FakeSdkListener)
    monkeypatch.setattr("halo_server.ring.events.MONITOR_INTERVAL", 0.001)
    listener = EventListener(
        SimpleNamespace(ring="ring"), received.append, credentials=original_credentials
    )

    await asyncio.wait_for(listener._run(), timeout=1)

    expected_count = 1 if owner_shutdown else 2
    assert operations == [
        (index, action) for index in range(expected_count) for action in ("create", "start", "stop")
    ]
    assert instances[0].credentials == original_credentials
    if not owner_shutdown:
        assert instances[1].credentials == replacement_credentials
        assert [event.type for event in received] == [RingEventType.MOTION]
    else:
        assert not received


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transient_state", ["STARTING_TASKS", "STARTING_CONNECTION", "STARTING_LOGIN", "RESETTING"]
)
async def test_listener_keeps_receiver_during_transient_reconnect(monkeypatch, transient_state):
    instances = []
    states = []
    polls = 0

    class FakeSdkListener:
        def __init__(self, _ring, _credentials, _credentials_updated, *, config):
            self._receiver = SimpleNamespace(run_state=SimpleNamespace(name=transient_state))
            self.stopped = False
            instances.append(self)

        def add_notification_callback(self, _callback):
            pass

        async def start(self):
            return True

        async def stop(self):
            self.stopped = True

    async def monitor_tick(_duration):
        nonlocal polls
        polls += 1
        assert len(instances) == 1
        if polls == 2:
            instances[0]._receiver.run_state.name = "STARTED"
        elif polls == 3:
            listener._stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(ring_doorbell, "RingEventListener", FakeSdkListener)
    listener = EventListener(SimpleNamespace(ring="ring"), lambda _event: None, states.append)
    monkeypatch.setattr(listener, "_sleep_until_stopped", monitor_tick)

    await asyncio.wait_for(listener._run(), timeout=1)

    assert polls == 3
    assert len(instances) == 1 and instances[0].stopped
    assert states == [
        EventConnectionState.CONNECTING,
        EventConnectionState.CONNECTED,
        EventConnectionState.CONNECTING,
        EventConnectionState.CONNECTED,
        EventConnectionState.OFFLINE,
    ]
