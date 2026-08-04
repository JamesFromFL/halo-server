import asyncio
import concurrent.futures
from types import SimpleNamespace

import pytest
from ring_doorbell import AuthenticationError, Requires2FAError

from halo_server.api.admin import AdminProtocolError
from halo_server.api.plugin import PluginIntercomOpenError
from halo_server.media.session import MediaSessionState
from halo_server.paths import ServerPaths
from halo_server.ring.credentials import CredentialStoreError
from halo_server.ring.events import (
    EventConnectionState,
    NormalizedRingEvent,
    RingEventType,
)
from halo_server.service import HaloService


class FakeClient:
    is_authenticated = True
    all_devices = []


class FakeSessions:
    def __init__(self):
        self.active_client = None
        self.account_email = None
        self.login_error = None
        self.logout_error = None
        self.client = FakeClient()
        self.shutdown_called = False
        self.stored_credentials = False
        self.restore_calls = 0
        self.restore_results = []

    def restore(self):
        self.restore_calls += 1
        if self.restore_results:
            result = self.restore_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            if result is not None:
                self.active_client = result
            return result
        return None

    def has_stored_credentials(self):
        return self.stored_credentials

    def login(self, email, _password, _otp):
        if self.login_error is not None:
            raise self.login_error
        self.active_client = self.client
        self.account_email = email
        return self.client

    def logout(self):
        self.active_client = None
        self.account_email = None
        if self.logout_error is not None:
            raise self.logout_error

    def shutdown(self):
        self.shutdown_called = True
        self.active_client = None


class FakeListener:
    def __init__(self, client, on_event, on_state):
        self.client = client
        self.on_event = on_event
        self.on_state = on_state
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeMediaSession:
    def __init__(self, _client, device):
        self.camera_id = str(device.id)
        self.acquired = []
        self.released = []
        self.expire_calls = 0
        self.stop_reasons = []
        self.intercom_frames = []
        self.state = MediaSessionState.STARTING
        self.failure = None
        self.leases = set()
        self.buffer = object()

    def acquire(self, purpose, *, lease_id):
        self.acquired.append((purpose, lease_id))
        self.leases.add(lease_id)
        return SimpleNamespace(id=lease_id)

    def release(self, lease_id):
        self.released.append(lease_id)
        self.leases.discard(lease_id)
        if not self.leases:
            self.state = MediaSessionState.IDLE

    def renew(self, lease_id):
        return SimpleNamespace(id=lease_id) if lease_id in self.leases else None

    def intercom_generation(self, lease_id):
        return lease_id if lease_id in self.leases else None

    def push_intercom_generation(self, generation, payload):
        if generation not in self.leases:
            raise RuntimeError("intercom lease is not active")
        self.intercom_frames.append((generation, payload))

    def wait_intercom_generation_ready(self, generation, _timeout):
        return generation in self.leases

    def expire(self):
        self.expire_calls += 1

    def stop_all(self, reason):
        self.stop_reasons.append(reason)
        self.leases.clear()
        self.state = MediaSessionState.IDLE

    def snapshot(self):
        return SimpleNamespace(
            state=self.state,
            generation=1,
            lease_count=len(self.leases),
            lease_purposes=(),
            buffer_ready=self.state is MediaSessionState.READY,
            buffer_packets=1 if self.state is MediaSessionState.READY else 0,
            buffer_bytes=100 if self.state is MediaSessionState.READY else 0,
            opus_frame_duration_ms=20,
            failure=self.failure,
        )


def _service(tmp_path, sessions=None, media_session_factory=FakeMediaSession):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    return HaloService(
        paths,
        session_manager=sessions or FakeSessions(),
        listener_factory=FakeListener,
        media_session_factory=media_session_factory,
    )


class FakeGateway:
    def __init__(self, *, host, port, open_consumer):
        self.host = host
        self.port = port
        self.bound_port = None
        self.open_consumer = open_consumer
        self.started = False
        self.stopped = False
        self.camera_updates = []
        self.rotations = 0
        self.rotation_error = None

    def start(self):
        self.started = True
        self.bound_port = self.port or 18555

    def stop(self):
        self.stopped = True
        self.bound_port = None

    def sync_cameras(self, camera_ids):
        self.camera_updates.append(list(camera_ids))

    def stream_descriptor(self, camera_id):
        return {"id": "main", "url": f"rtsp://127.0.0.1/{camera_id}"}

    def rotate_credentials(self):
        self.rotations += 1
        if self.rotation_error is not None:
            raise self.rotation_error


@pytest.mark.asyncio
async def test_admin_login_attaches_event_listener_without_exposing_password(tmp_path):
    sessions = FakeSessions()
    service = _service(tmp_path, sessions)

    result = await service.handle_admin(
        "login",
        {
            "email": "person@example.com",
            "password": "secret",
            "otp_code": "123456",
        },
    )

    assert result["account"]["authenticated"] is True
    assert result["account"]["email"] == "person@example.com"
    assert service._listener.started is True
    assert "secret" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (Requires2FAError(), "two_factor_required"),
        (AuthenticationError("bad"), "authentication_failed"),
        (CredentialStoreError("disk"), "credential_storage_failed"),
        (RuntimeError("network"), "ring_unavailable"),
    ],
)
async def test_admin_login_returns_stable_safe_error_codes(tmp_path, error, code):
    sessions = FakeSessions()
    sessions.login_error = error
    service = _service(tmp_path, sessions)

    with pytest.raises(AdminProtocolError) as raised:
        await service.handle_admin(
            "login",
            {"email": "person@example.com", "password": "secret"},
        )

    assert raised.value.code == code
    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_admin_validates_login_parameters_before_session_call(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(AdminProtocolError) as raised:
        await service.handle_admin(
            "login",
            {"email": "person@example.com", "password": "secret", "extra": True},
        )

    assert raised.value.code == "bad_params"


def test_ring_event_enters_per_camera_incident_and_is_visible_in_status(tmp_path):
    service = _service(tmp_path)
    event = NormalizedRingEvent(
        event_id="101",
        camera_id="42",
        camera_name="Front Door",
        camera_kind="lpd_v4",
        type=RingEventType.MOTION,
        source_kind="motion",
        source_state="active",
        occurred_at=100,
        received_at=10,
        source_expires_in=180,
        is_update=False,
    )

    service._process_event(event)
    status = service.status()

    assert status["events"]["recent"][0]["event_id"] == "101"
    assert status["incidents"]["42"]["phase"] == "warming"
    assert status["recent_actions"][0]["type"] == "start_capture"
    assert status["media"]["control"] == "lease_controlled"


@pytest.mark.asyncio
async def test_logout_stops_listener_and_clears_incident_state(tmp_path):
    sessions = FakeSessions()
    service = _service(tmp_path, sessions)
    await service.handle_admin(
        "login",
        {"email": "person@example.com", "password": "secret"},
    )
    listener = service._listener
    service._process_event(
        NormalizedRingEvent(
            event_id="ding-1",
            camera_id="42",
            camera_name="Front Door",
            camera_kind="lpd_v4",
            type=RingEventType.DOORBELL,
            source_kind="ding",
            source_state="active",
            occurred_at=100,
            received_at=10,
            source_expires_in=180,
            is_update=False,
        )
    )

    result = await service.handle_admin("logout", {})

    assert listener.stopped is True
    assert result["account"]["authenticated"] is False
    assert result["events"]["connection"] is EventConnectionState.OFFLINE
    assert result["incidents"] == {}


@pytest.mark.asyncio
async def test_logout_failure_still_detaches_listener(tmp_path):
    sessions = FakeSessions()
    sessions.active_client = sessions.client
    sessions.account_email = "person@example.com"
    sessions.logout_error = CredentialStoreError("disk")
    service = _service(tmp_path, sessions)
    service._listener = FakeListener(
        sessions.client,
        lambda _event: None,
        lambda _state: None,
    )
    listener = service._listener

    with pytest.raises(AdminProtocolError) as raised:
        await service.handle_admin("logout", {})

    assert raised.value.code == "credential_storage_failed"
    assert listener.stopped is True
    assert service._listener is None
    assert service._event_state is EventConnectionState.OFFLINE


def test_event_state_callback_is_marshaled_to_daemon_loop(tmp_path):
    service = _service(tmp_path)
    calls = []
    service._main_loop = SimpleNamespace(
        is_closed=lambda: False,
        call_soon_threadsafe=lambda callback, *values: calls.append((callback, *values)),
    )

    service._listener_generation = 1
    service._receive_event_state(1, EventConnectionState.CONNECTED)

    assert calls == [
        (
            service._set_event_state,
            1,
            EventConnectionState.CONNECTED,
        )
    ]


@pytest.mark.asyncio
async def test_motion_event_acquires_generation_named_media_lease(tmp_path):
    sessions = FakeSessions()
    camera = SimpleNamespace(id=42, name="Front Door")
    sessions.client.all_devices = [camera]
    sessions.active_client = sessions.client
    sessions.account_email = "person@example.com"
    service = _service(tmp_path, sessions)
    service._main_loop = asyncio.get_running_loop()
    service._media_client = sessions.client
    event = NormalizedRingEvent(
        event_id="101",
        camera_id="42",
        camera_name="Front Door",
        camera_kind="lpd_v4",
        type=RingEventType.MOTION,
        source_kind="motion",
        source_state="active",
        occurred_at=100,
        received_at=10,
        source_expires_in=180,
        is_update=False,
    )

    service._process_event(event)
    await service._drain_media_tasks()

    media = service._media_sessions["42"]
    assert [(str(purpose), lease_id) for purpose, lease_id in media.acquired] == [
        ("event", "incident-1")
    ]
    assert service.status()["media"]["active_upstreams"] == 1


def test_failed_media_session_is_not_reported_as_an_active_upstream(tmp_path):
    service = _service(tmp_path)
    media = FakeMediaSession(None, SimpleNamespace(id=42))
    media.state = MediaSessionState.FAILED
    media.failure = "receiver_stopped"
    media.leases.add("event")
    service._media_sessions["42"] = media

    status = service.status()["media"]

    assert status["active_upstreams"] == 0
    assert status["sessions"]["42"]["state"] is MediaSessionState.FAILED


@pytest.mark.asyncio
async def test_media_readiness_publishes_incident_motion_and_quiet_stop_releases_lease(
    tmp_path,
):
    sessions = FakeSessions()
    camera = SimpleNamespace(id=42, name="Front Door")
    sessions.client.all_devices = [camera]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions)
    service._main_loop = asyncio.get_running_loop()
    service._media_client = sessions.client
    service._process_event(
        NormalizedRingEvent(
            event_id="101",
            camera_id="42",
            camera_name="Front Door",
            camera_kind="lpd_v4",
            type=RingEventType.MOTION,
            source_kind="motion",
            source_state="active",
            occurred_at=100,
            received_at=10,
            source_expires_in=180,
            is_update=False,
        )
    )
    await service._drain_media_tasks()
    media = service._media_sessions["42"]
    media.state = MediaSessionState.READY

    service._reconcile_media_state()

    incident = service._incidents["42"].snapshot()
    assert incident.media_ready is True
    assert incident.motion_published is True
    assert [str(action["type"]) for action in service._recent_actions][-2:] == [
        "media_ready",
        "motion_on",
    ]

    stop_actions = service._incidents["42"].tick(now=incident.quiet_deadline)
    service._handle_incident_actions("42", stop_actions)
    await service._drain_media_tasks()

    assert media.released == ["incident-1"]
    assert service._incidents["42"].snapshot().phase == "idle"


@pytest.mark.asyncio
async def test_callbacks_from_replaced_listener_are_ignored(tmp_path):
    sessions = FakeSessions()
    service = _service(tmp_path, sessions)
    service._main_loop = asyncio.get_running_loop()
    await service.handle_admin(
        "login",
        {"email": "first@example.com", "password": "secret"},
    )
    replaced = service._listener
    await service.handle_admin(
        "login",
        {"email": "second@example.com", "password": "secret"},
    )
    current = service._listener
    event = NormalizedRingEvent(
        event_id="101",
        camera_id="42",
        camera_name="Front Door",
        camera_kind="lpd_v4",
        type=RingEventType.MOTION,
        source_kind="motion",
        source_state="active",
        occurred_at=100,
        received_at=10,
        source_expires_in=180,
        is_update=False,
    )

    replaced.on_event(event)
    replaced.on_state(EventConnectionState.OFFLINE)
    await asyncio.sleep(0)
    assert service.status()["events"]["recent"] == []

    current.on_event(event)
    current.on_state(EventConnectionState.CONNECTED)
    await asyncio.sleep(0)
    assert service.status()["events"]["recent"][0]["event_id"] == "101"
    assert service.status()["events"]["connection"] == "connected"
    await service._drain_media_tasks()


@pytest.mark.asyncio
async def test_plugin_pair_list_and_revoke_never_list_token(tmp_path):
    service = _service(tmp_path)

    pairing = await service.handle_admin("plugin_pair", {"label": "Scrypted"})
    status = await service.handle_admin("plugin_list", {})

    assert pairing["token"].startswith(f"halo1.{pairing['client']['id']}.")
    assert status["clients"] == [pairing["client"]]
    assert "token" not in repr(status)
    assert service._plugin_tokens.verify(pairing["token"]) is not None

    revoked = await service.handle_admin(
        "plugin_revoke",
        {"client_id": pairing["client"]["id"]},
    )
    assert revoked["revoked"] is True
    assert service._plugin_tokens.verify(pairing["token"]) is None


def test_only_downstream_incident_actions_enter_plugin_event_journal(tmp_path):
    service = _service(tmp_path)
    service._process_event(
        NormalizedRingEvent(
            event_id="101",
            camera_id="42",
            camera_name="Front Door",
            camera_kind="lpd_v4",
            type=RingEventType.MOTION,
            source_kind="motion",
            source_state="active",
            occurred_at=100,
            received_at=10,
            source_expires_in=180,
            is_update=False,
        )
    )

    assert service._plugin_events.latest_sequence == 0

    service._handle_incident_actions(
        "42",
        service._incidents["42"].on_media_ready(1),
    )
    events = service._plugin_events.replay(
        epoch=service._plugin_events.epoch,
        after_sequence=0,
    )

    assert [event.type for event in events] == ["media_ready", "motion_on"]
    assert all(event.camera_id == "42" for event in events)


def test_doorbell_enters_plugin_journal_immediately(tmp_path):
    service = _service(tmp_path)

    service._process_event(
        NormalizedRingEvent(
            event_id="ding-1",
            camera_id="42",
            camera_name="Front Door",
            camera_kind="lpd_v4",
            type=RingEventType.DOORBELL,
            source_kind="ding",
            source_state="active",
            occurred_at=100,
            received_at=10,
            source_expires_in=180,
            is_update=False,
        )
    )

    events = service._plugin_events.replay(
        epoch=service._plugin_events.epoch,
        after_sequence=0,
    )
    assert [event.type for event in events] == ["doorbell"]
    assert events[0].payload["event_id"] == "ding-1"


@pytest.mark.asyncio
async def test_service_starts_stops_and_reports_optional_rtsp_gateway(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=FakeSessions(),
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        rtsp_host="127.0.0.1",
        rtsp_port=0,
        rtsp_gateway_factory=FakeGateway,
    )

    await service.start()
    status = service.status()
    await service.stop()

    assert service._rtsp_gateway.started is True
    assert service._rtsp_gateway.stopped is True
    assert status["media"]["gateway"] == {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 18555,
    }


@pytest.mark.asyncio
async def test_service_retries_transient_cached_session_restore(tmp_path):
    sessions = FakeSessions()
    sessions.stored_credentials = True
    sessions.restore_results = [None, sessions.client]
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        restore_retry_initial=0.01,
        restore_retry_max=0.02,
    )

    await service.start()
    assert service.status()["account"]["authenticated"] is False
    assert "retrying" in service.status()["account"]["last_error"]

    for _ in range(20):
        if service.status()["account"]["authenticated"]:
            break
        await asyncio.sleep(0.01)

    assert sessions.restore_calls == 2
    assert service.status()["account"]["authenticated"] is True
    assert service.status()["account"]["last_error"] is None
    assert service._listener.started is True
    await service.stop()


@pytest.mark.asyncio
async def test_service_repairs_rtsp_sync_without_reauthenticating(tmp_path):
    class FailsOnceGateway(FakeGateway):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.sync_failures = 1

        def sync_cameras(self, camera_ids):
            self.camera_updates.append(list(camera_ids))
            if self.sync_failures:
                self.sync_failures -= 1
                raise RuntimeError("injected RTSP sync failure")

    sessions = FakeSessions()
    sessions.client.all_devices = [
        SimpleNamespace(
            id=42,
            name="Front Door",
            family="doorbots",
            kind="lpd_v4",
            model="Doorbell",
            subscribed=True,
            battery_life=90,
            motion_detection=True,
            has_capability=lambda _capability: False,
        )
    ]
    sessions.restore_results = [sessions.client]
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        rtsp_host="127.0.0.1",
        rtsp_port=0,
        rtsp_gateway_factory=FailsOnceGateway,
        restore_retry_initial=0.01,
        restore_retry_max=0.02,
    )

    await service.start()
    try:
        for _ in range(30):
            if service._listener is not None:
                break
            await asyncio.sleep(0.01)

        assert sessions.restore_calls == 1
        assert service._listener is not None
        assert service._listener.started is True
        assert service._rtsp_gateway.camera_updates == [["42"], ["42"]]
        assert service.status()["account"]["last_error"] is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_service_repairs_listener_start_without_reauthenticating(tmp_path):
    listeners = []

    class FailsOnceListener(FakeListener):
        def __init__(self, *args):
            super().__init__(*args)
            listeners.append(self)

        def start(self):
            if len(listeners) == 1:
                raise RuntimeError("injected listener start failure")
            super().start()

    sessions = FakeSessions()
    sessions.restore_results = [sessions.client]
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=FailsOnceListener,
        media_session_factory=FakeMediaSession,
        restore_retry_initial=0.01,
        restore_retry_max=0.02,
    )

    await service.start()
    try:
        for _ in range(30):
            if len(listeners) >= 2 and listeners[-1].started:
                break
            await asyncio.sleep(0.01)

        assert sessions.restore_calls == 1
        assert len(listeners) == 2
        assert listeners[-1].started is True
        assert service._listener is listeners[-1]
        assert service.status()["account"]["last_error"] is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_login_repairs_listener_start_without_repeating_login(tmp_path):
    listeners = []

    class FailsOnceListener(FakeListener):
        def __init__(self, *args):
            super().__init__(*args)
            listeners.append(self)

        def start(self):
            if len(listeners) == 1:
                raise RuntimeError("injected listener start failure")
            super().start()

    sessions = FakeSessions()
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=FailsOnceListener,
        media_session_factory=FakeMediaSession,
        restore_retry_initial=0.01,
        restore_retry_max=0.02,
    )

    await service.start()
    try:
        result = await service.handle_admin(
            "login",
            {"email": "person@example.com", "password": "secret"},
        )
        assert result["account"]["authenticated"] is True

        for _ in range(30):
            if len(listeners) >= 2 and listeners[-1].started:
                break
            await asyncio.sleep(0.01)

        assert len(listeners) == 2
        assert listeners[-1].started is True
        assert service._listener is listeners[-1]
        assert service.status()["account"]["last_error"] is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_service_repairs_listener_that_terminates_after_start(tmp_path):
    listeners = []

    class TerminatingListener(FakeListener):
        def __init__(self, *args):
            super().__init__(*args)
            self.future = concurrent.futures.Future()
            listeners.append(self)

        @property
        def running(self):
            return not self.future.done()

        def start(self):
            super().start()
            return self.future

        def wait_until_started(self, timeout=5):
            assert timeout > 0

        def stop(self):
            super().stop()
            if not self.future.done():
                self.future.set_result(None)

    sessions = FakeSessions()
    sessions.restore_results = [sessions.client]
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=TerminatingListener,
        media_session_factory=FakeMediaSession,
        restore_retry_initial=0.01,
        restore_retry_max=0.02,
    )

    await service.start()
    try:
        assert service.status()["account"]["integrations_ready"] is True
        listeners[0].future.set_exception(RuntimeError("injected listener termination"))

        for _ in range(30):
            if len(listeners) >= 2 and service._listener is listeners[-1]:
                break
            await asyncio.sleep(0.01)

        assert sessions.restore_calls == 1
        assert len(listeners) == 2
        assert service._listener is listeners[-1]
        assert service.status()["account"]["integrations_ready"] is True
        assert service.status()["account"]["last_error"] is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_service_repairs_listener_that_fails_during_async_startup(tmp_path):
    listeners = []

    class StartupFailingListener(FakeListener):
        def __init__(self, *args):
            super().__init__(*args)
            self.future = concurrent.futures.Future()
            listeners.append(self)

        def start(self):
            super().start()
            if len(listeners) == 1:
                self.future.set_exception(RuntimeError("injected asynchronous startup failure"))
            return self.future

        def wait_until_started(self, timeout=5):
            assert timeout > 0
            if self.future.done():
                self.future.result()

        def stop(self):
            super().stop()
            if not self.future.done():
                self.future.set_result(None)

    sessions = FakeSessions()
    sessions.restore_results = [sessions.client]
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=StartupFailingListener,
        media_session_factory=FakeMediaSession,
        restore_retry_initial=0.01,
        restore_retry_max=0.02,
    )

    await service.start()
    try:
        for _ in range(30):
            if len(listeners) >= 2 and service._listener is listeners[-1]:
                break
            await asyncio.sleep(0.01)

        assert sessions.restore_calls == 1
        assert len(listeners) == 2
        assert listeners[0].stopped is True
        assert service._listener is listeners[-1]
        assert service.status()["account"]["integrations_ready"] is True
        assert service.status()["account"]["last_error"] is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_service_does_not_retry_without_cached_credentials(tmp_path):
    sessions = FakeSessions()
    service = _service(tmp_path, sessions)

    await service.start()
    await asyncio.sleep(0.02)

    assert sessions.restore_calls == 1
    assert service._restore_task is None
    assert service.status()["account"]["last_error"] is None
    await service.stop()


@pytest.mark.asyncio
async def test_service_releases_remaining_resources_when_one_stop_fails(tmp_path):
    class FailingPluginApi:
        async def stop(self):
            raise RuntimeError("plugin stop failed")

    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    sessions = FakeSessions()
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        rtsp_host="127.0.0.1",
        rtsp_port=0,
        rtsp_gateway_factory=FakeGateway,
    )
    await service.start()
    service._plugin_api = FailingPluginApi()

    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await service.stop()

    assert service._rtsp_gateway.stopped is True
    assert sessions.shutdown_called is True
    assert service._started_at is None
    assert service._main_loop is None


@pytest.mark.asyncio
async def test_login_and_logout_sync_rtsp_camera_inventory(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    sessions = FakeSessions()
    sessions.client.all_devices = [
        SimpleNamespace(
            id=42,
            name="Front Door",
            family="doorbots",
            kind="lpd_v4",
            model="Doorbell",
            subscribed=True,
            battery_life=90,
            motion_detection=True,
            has_capability=lambda _capability: False,
        )
    ]
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        rtsp_host="127.0.0.1",
        rtsp_gateway_factory=FakeGateway,
    )

    await service.handle_admin(
        "login",
        {"email": "person@example.com", "password": "secret"},
    )
    await service.handle_admin("logout", {})

    assert service._rtsp_gateway.camera_updates == [[], ["42"], []]


@pytest.mark.asyncio
async def test_rtsp_consumer_uses_bounded_scrypted_lease(tmp_path):
    sessions = FakeSessions()
    camera = SimpleNamespace(id=42, name="Front Door")
    sessions.client.all_devices = [camera]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions)
    service._main_loop = asyncio.get_running_loop()
    service._media_client = sessions.client

    consumer = await asyncio.to_thread(service._open_rtsp_consumer, "42")
    media = service._media_sessions["42"]
    assert [
        (str(purpose), lease_id.startswith("rtsp-")) for purpose, lease_id in media.acquired
    ] == [("scrypted_live", True)]
    assert await asyncio.to_thread(consumer.renew) is True

    await asyncio.to_thread(consumer.close)

    assert len(media.released) == 1
    assert media.released[0].startswith("rtsp-")


@pytest.mark.asyncio
async def test_rtsp_consumer_can_renew_while_session_waits_to_recover(tmp_path):
    sessions = FakeSessions()
    camera = SimpleNamespace(id=42, name="Front Door")
    sessions.client.all_devices = [camera]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions)
    service._main_loop = asyncio.get_running_loop()
    service._media_client = sessions.client

    consumer = await asyncio.to_thread(service._open_rtsp_consumer, "42")
    media = service._media_sessions["42"]
    media.state = MediaSessionState.FAILED

    assert await asyncio.to_thread(consumer.renew) is True
    await asyncio.to_thread(consumer.close)


@pytest.mark.asyncio
async def test_plugin_intercom_is_single_owner_and_releases_bounded_lease(tmp_path):
    sessions = FakeSessions()

    class CompatibleCamera:
        id = 42
        name = "Front Door"

        def has_capability(self, capability):
            return capability == "video"

        async def generate_async_webrtc_stream(self):
            return None

        async def on_webrtc_candidate(self):
            return None

        async def close_webrtc_stream(self):
            return None

        def get_ice_servers(self):
            return []

    camera = CompatibleCamera()
    sessions.client.all_devices = [camera]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions)
    service._media_client = sessions.client

    stream = await service._open_plugin_intercom("42")
    media = service._media_sessions["42"]
    payload = b"\x01\x00" * 960
    await stream.write(payload)

    with pytest.raises(PluginIntercomOpenError) as raised:
        await service._open_plugin_intercom("42")
    assert raised.value.code == "intercom_busy"
    assert str(media.acquired[0][0]) == "intercom"
    assert media.intercom_frames == [(media.acquired[0][1], payload)]
    assert await stream.wait_ready(1) is True
    assert await stream.renew() is True

    await stream.close()

    assert media.released == [media.acquired[0][1]]
    replacement = await service._open_plugin_intercom("42")
    await replacement.close()


@pytest.mark.asyncio
async def test_plugin_intercom_rejects_camera_without_validated_support(tmp_path):
    sessions = FakeSessions()
    camera = SimpleNamespace(id=42, name="Front Door", has_capability=lambda _name: False)
    sessions.client.all_devices = [camera]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions)
    service._media_client = sessions.client

    with pytest.raises(PluginIntercomOpenError) as raised:
        await service._open_plugin_intercom("42")

    assert raised.value.code == "intercom_unsupported"
    assert "42" not in service._media_sessions


@pytest.mark.asyncio
async def test_plugin_revocation_rotates_rtsp_credentials(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=FakeSessions(),
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        rtsp_host="127.0.0.1",
        rtsp_gateway_factory=FakeGateway,
    )
    pairing = await service.handle_admin("plugin_pair", {"label": "Scrypted"})

    await service.handle_admin(
        "plugin_revoke",
        {"client_id": pairing["client"]["id"]},
    )

    assert service._rtsp_gateway.rotations == 1


@pytest.mark.asyncio
async def test_plugin_revocation_stops_rtsp_gateway_when_rotation_fails(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=FakeSessions(),
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        rtsp_host="127.0.0.1",
        rtsp_gateway_factory=FakeGateway,
    )
    pairing = await service.handle_admin("plugin_pair", {"label": "Scrypted"})
    service._rtsp_gateway.rotation_error = RuntimeError("rotation failed")

    with pytest.raises(
        AdminProtocolError,
        match="media credentials could not be revoked safely",
    ):
        await service.handle_admin(
            "plugin_revoke",
            {"client_id": pairing["client"]["id"]},
        )

    assert service._rtsp_gateway.rotations == 1
    assert service._rtsp_gateway.stopped is True
