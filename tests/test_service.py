import asyncio
import concurrent.futures
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ring_doorbell import AuthenticationError, Requires2FAError

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.api.plugin import PluginCameraOperationError, PluginIntercomOpenError
from halo_server.camera_privacy import ring_privacy_receipt
from halo_server.media.session import MediaSessionState
from halo_server.paths import ServerPaths
from halo_server.ring.credentials import CredentialStoreError
from halo_server.ring.events import (
    EventConnectionState,
    NormalizedRingEvent,
    RingEventType,
)
from halo_server.service import SIREN_ON_DURATION_SECONDS, HaloService


class FakeClient:
    is_authenticated = True

    def __init__(self):
        self.all_devices = []


def _fixture_privacy(client):
    """An explicit successful synthetic inventory read, never a production fallback."""
    for device in client.all_devices:
        if not hasattr(device, "family"):
            device.family = "doorbots"
        if not hasattr(device, "_attrs"):
            device._attrs = {
                "settings": {
                    "live_view_disabled": False,
                    "motion_detection_enabled": True,
                    "enable_audio_recording": True,
                }
            }
    client._camera_privacy_receipt = ring_privacy_receipt(client.all_devices, time.monotonic())


def _install_fixture_privacy(service, client):
    _fixture_privacy(client)
    service._media_scope = service._sessions.active_scope
    service._camera_privacy.observe(
        service._media_scope, client, client.all_devices, client._camera_privacy_receipt
    )


class FakeAccessoryDevice:
    id = 42
    family = "stickup_cams"
    kind = "stickup_cam_mini"

    def __init__(self, capabilities=("light", "siren", "video")):
        self.capabilities = set(capabilities)
        self.light_values = []
        self.siren_values = []

    def has_capability(self, capability):
        return capability in self.capabilities

    async def async_set_light(self, value):
        self.light_values.append(value)

    async def async_set_siren(self, value):
        self.siren_values.append(value)

    async def async_get_snapshot(self, **_kwargs):
        return b"\xff\xd8fresh\xff\xd9"


class FakeAccessoryClient:
    is_authenticated = True

    def __init__(self, device, *, snapshot=b"\xff\xd8cached\xff\xd9"):
        self.all_devices = [device]
        _fixture_privacy(self)
        self.snapshot = snapshot
        self.snapshot_calls = []
        self.submissions = 0

    def submit(self, coroutine):
        self.submissions += 1
        future = concurrent.futures.Future()
        try:
            coroutine.send(None)
        except StopIteration as completed:
            future.set_result(completed.value)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_exception(RuntimeError("fake coroutine unexpectedly suspended"))
            coroutine.close()
        return future

    def submit_snapshot_for_device(self, device, *, fresh=False):
        self.snapshot_calls.append((device, fresh))
        future = concurrent.futures.Future()
        future.set_result(self.snapshot)
        return future


class FakeSessions:
    def __init__(self):
        self.active_client = None
        self._fixture_scope = AccountScope(str(uuid4()), 1)
        self._scope_client = None
        self.account_email = None
        self.login_error = None
        self.logout_error = None
        self.client = FakeClient()
        self.shutdown_called = False
        self.stored_credentials = False
        self.restore_calls = 0
        self.restore_results = []

    @property
    def active_scope(self):
        if self.active_client is not self._scope_client:
            self._scope_client = self.active_client
            self._fixture_scope = AccountScope(
                self._fixture_scope.profile_id, self._fixture_scope.generation + 1
            )
        return self._fixture_scope if self.active_client is not None else None

    @property
    def active_context(self):
        return (self.active_client, self.active_scope) if self.active_client is not None else None

    def restore(self):
        self.restore_calls += 1
        if self.restore_results:
            result = self.restore_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            if result is not None:
                _fixture_privacy(result)
                self.active_client = result
            return result
        return None

    def has_stored_credentials(self):
        return self.stored_credentials

    def login(self, email, _password, _otp):
        if self.login_error is not None:
            raise self.login_error
        _fixture_privacy(self.client)
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
    def __init__(self, _client, device, *, legacy_audio_enabled=True):
        self.legacy_audio_enabled = legacy_audio_enabled
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

    def push_intercom_generation(self, generation, payload, *, admission=None):
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
    service = HaloService(
        paths,
        session_manager=sessions or FakeSessions(),
        listener_factory=FakeListener,
        media_session_factory=media_session_factory,
    )
    if service._sessions.active_client is not None:
        _install_fixture_privacy(service, service._sessions.active_client)
        service._account_integrations_ready = True
    return service


async def _start_service(service):
    """Wait for fake restore in tests that assert post-restore state."""
    await service.start()
    if service._initial_restore_task is not None:
        await asyncio.wait_for(asyncio.shield(service._initial_restore_task), 1)


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
    assert result["events"]["recent"] == []
    assert result["recent_actions"] == []
    assert service._incidents == {}
    assert service._plugin_events.latest_sequence == 0


@pytest.mark.asyncio
async def test_service_passes_session_owned_credentials_to_event_listener(tmp_path):
    persisted = []

    class CredentialAwareSessions(FakeSessions):
        def prepare_event_listener_credentials(self, client):
            assert client is self.active_client
            return {"gcm": {"android_id": "persisted"}}, persisted.append

    class CredentialAwareListener(FakeListener):
        def __init__(
            self,
            client,
            on_event,
            on_state,
            *,
            credentials,
            credentials_updated_callback,
        ):
            super().__init__(client, on_event, on_state)
            self.credentials = credentials
            self.credentials_updated_callback = credentials_updated_callback

    sessions = CredentialAwareSessions()
    service = HaloService(
        ServerPaths(tmp_path / "state", tmp_path / "runtime"),
        session_manager=sessions,
        listener_factory=CredentialAwareListener,
        media_session_factory=FakeMediaSession,
    )

    await asyncio.wait_for(
        service.handle_admin(
            "login",
            {"email": "person@example.com", "password": "secret"},
        ),
        timeout=1,
    )

    listener = service._listener
    assert listener.credentials == {"gcm": {"android_id": "persisted"}}
    listener.credentials_updated_callback({"gcm": {"android_id": "updated"}})
    assert persisted == [{"gcm": {"android_id": "updated"}}]


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


@pytest.mark.asyncio
async def test_media_actions_for_different_cameras_do_not_share_a_lock(tmp_path):
    first_acquire_entered = threading.Event()
    allow_first_acquire = threading.Event()

    class BlockingFirstCameraSession(FakeMediaSession):
        def acquire(self, purpose, *, lease_id):
            if self.camera_id == "1":
                first_acquire_entered.set()
                assert allow_first_acquire.wait(2)
            return super().acquire(purpose, lease_id=lease_id)

    sessions = FakeSessions()
    sessions.client.all_devices = [
        SimpleNamespace(id=1, name="First"),
        SimpleNamespace(id=2, name="Second"),
    ]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions, BlockingFirstCameraSession)
    service._media_client = sessions.client

    first = asyncio.create_task(service._acquire_rtsp_consumer("1"))
    try:
        for _ in range(100):
            if first_acquire_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert first_acquire_entered.is_set()

        second_session, second_lease = await asyncio.wait_for(
            service._acquire_rtsp_consumer("2"),
            timeout=0.5,
        )
    finally:
        allow_first_acquire.set()

    first_session, first_lease = await first
    await service._close_rtsp_consumer_async("1", first_lease)
    await service._close_rtsp_consumer_async("2", second_lease)

    assert first_session.camera_id == "1"
    assert second_session.camera_id == "2"


@pytest.mark.asyncio
async def test_account_invalidation_releases_in_flight_event_acquire(tmp_path):
    acquire_entered = threading.Event()
    allow_acquire = threading.Event()
    created_sessions = []

    class BlockingMediaSession(FakeMediaSession):
        def __init__(self, client, device, *, legacy_audio_enabled=True):
            super().__init__(client, device, legacy_audio_enabled=legacy_audio_enabled)
            created_sessions.append(self)

        def acquire(self, purpose, *, lease_id):
            acquire_entered.set()
            assert allow_acquire.wait(2)
            return super().acquire(purpose, lease_id=lease_id)

    sessions = FakeSessions()
    sessions.client.all_devices = [SimpleNamespace(id=1, name="First")]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions, BlockingMediaSession)
    service._media_client = sessions.client

    acquire = asyncio.create_task(service._execute_media_actions("1", [], 1))
    for _ in range(100):
        if acquire_entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert acquire_entered.is_set()

    stop = asyncio.create_task(service._stop_media_sessions("account_replaced"))
    await asyncio.sleep(0)
    assert service._media_sessions == {}
    assert service._media_client is None

    allow_acquire.set()
    await acquire
    await stop

    media = created_sessions[0]
    assert media.released == ["incident-1"]
    assert media.stop_reasons == ["account_replaced"]


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
async def test_source_motion_precedes_media_readiness_and_quiet_stop_releases_lease(
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
    published_types = [str(action["type"]) for action in service._recent_actions]
    assert published_types.count("motion_on") == 1
    assert published_types.index("motion_on") < published_types.index("media_ready")

    stop_actions = service._incidents["42"].tick(now=incident.quiet_deadline)
    service._handle_incident_actions("42", stop_actions)
    await service._drain_media_tasks()

    assert media.released == ["incident-1"]
    assert service._incidents["42"].snapshot().phase == "idle"


@pytest.mark.asyncio
async def test_callbacks_from_replaced_listener_are_ignored(tmp_path):
    sessions = FakeSessions()
    sessions.client.all_devices = [SimpleNamespace(id=42, name="Front Door")]
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


def test_service_injects_camera_operation_providers_into_plugin_api(tmp_path):
    captured = {}

    def plugin_api_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(host=kwargs["host"], bound_port=None)

    service = HaloService(
        ServerPaths(tmp_path / "state", tmp_path / "runtime"),
        session_manager=FakeSessions(),
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        plugin_host="127.0.0.1",
        plugin_api_factory=plugin_api_factory,
    )

    assert captured["control_provider"].__self__ is service
    assert captured["snapshot_provider"].__self__ is service


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

    assert service._plugin_events.latest_sequence == 1

    service._handle_incident_actions(
        "42",
        service._incidents["42"].on_media_ready(1),
    )
    events = service._plugin_events.replay(
        epoch=service._plugin_events.epoch,
        after_sequence=0,
    )

    assert [event.type for event in events] == ["motion_on", "media_ready"]
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

    await _start_service(service)
    status = service.status()
    inventory_task = service._inventory_task
    await service.stop()

    assert service._rtsp_gateway.started is True
    assert service._rtsp_gateway.stopped is True
    assert inventory_task is not None and inventory_task.done()
    assert service._inventory_task is None
    assert status["media"]["gateway"] == {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 18555,
    }


@pytest.mark.asyncio
async def test_inventory_refresh_updates_gateway_and_retires_stale_media_sessions(tmp_path):
    def camera(device_id, name):
        return SimpleNamespace(
            id=device_id,
            name=name,
            family="doorbots",
            kind="lpd_v4",
            model="Doorbell",
            subscribed=True,
            battery_life=90,
            motion_detection=True,
            has_capability=lambda _capability: False,
        )

    class RefreshingClient:
        is_authenticated = True

        def __init__(self):
            self.all_devices = [camera(1, "Old Camera")]
            self.refresh_calls = 0

        def refresh_devices(self):
            self.refresh_calls += 1
            self.all_devices = [camera(2, "New Camera")]
            return list(self.all_devices)

    client = RefreshingClient()
    sessions = FakeSessions()
    sessions.client = client
    sessions.active_client = client
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        rtsp_host="127.0.0.1",
        rtsp_gateway_factory=FakeGateway,
    )
    service._media_client = client
    removed = FakeMediaSession(None, SimpleNamespace(id=1))
    refreshed = FakeMediaSession(None, SimpleNamespace(id=2))
    refreshed.state = MediaSessionState.IDLE
    service._media_sessions.update({"1": removed, "2": refreshed})

    await service._refresh_active_inventory()

    assert client.refresh_calls == 1
    assert service._rtsp_gateway.camera_updates == [["2"]]
    assert service._media_sessions == {}
    assert removed.stop_reasons == ["inventory_refreshed"]
    assert refreshed.stop_reasons == ["inventory_refreshed"]
    assert [item["id"] for item in service.status()["cameras"]] == ["2"]


@pytest.mark.asyncio
async def test_stale_inventory_refresh_cannot_republish_previous_account(tmp_path):
    refresh_started = threading.Event()
    allow_refresh = threading.Event()

    class BlockingClient:
        is_authenticated = True

        def __init__(self, device_id):
            self.all_devices = [SimpleNamespace(id=device_id)]

        def refresh_devices(self):
            refresh_started.set()
            assert allow_refresh.wait(2)
            return list(self.all_devices)

    previous = BlockingClient(1)
    replacement = BlockingClient(2)
    sessions = FakeSessions()
    sessions.client = previous
    sessions.active_client = previous
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = HaloService(
        paths,
        session_manager=sessions,
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
        rtsp_host="127.0.0.1",
        rtsp_gateway_factory=FakeGateway,
    )
    service._media_client = previous

    refresh = asyncio.create_task(service._refresh_active_inventory())
    for _ in range(100):
        if refresh_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert refresh_started.is_set()

    sessions.active_client = replacement
    service._invalidate_media_access()
    allow_refresh.set()
    await refresh

    assert service._rtsp_gateway.camera_updates == []


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

    await _start_service(service)
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

    await _start_service(service)
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

    await _start_service(service)
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

    await _start_service(service)
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

    await _start_service(service)
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

    await _start_service(service)
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

    await _start_service(service)
    await asyncio.sleep(0.02)

    assert sessions.restore_calls == 1
    assert service._restore_task is None
    assert service.status()["account"]["last_error"] is None
    await service.stop()


@pytest.mark.asyncio
async def test_service_releases_remaining_resources_when_one_stop_fails(tmp_path):
    class FailingPluginApi:
        fail = True

        async def stop(self):
            if self.fail:
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
    await _start_service(service)
    service._plugin_api = FailingPluginApi()

    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await service.stop()

    assert service._rtsp_gateway.stopped is True
    assert sessions.shutdown_called is True
    assert service._started_at is None
    assert service._main_loop is None

    assert service._source_guard_lease is not None
    service._plugin_api.fail = False
    await service.stop()
    assert service._source_guard_lease is None


@pytest.mark.asyncio
async def test_media_session_stop_failures_are_propagated_after_all_cameras_stop(tmp_path):
    class FailingMediaSession(FakeMediaSession):
        def stop_all(self, reason):
            super().stop_all(reason)
            if self.camera_id == "1":
                raise RuntimeError("camera cleanup failed")

    service = _service(tmp_path)
    first = FailingMediaSession(None, SimpleNamespace(id=1))
    second = FailingMediaSession(None, SimpleNamespace(id=2))
    service._media_sessions.update({"1": first, "2": second})

    with pytest.raises(ExceptionGroup, match="media session shutdown failed") as raised:
        await service._stop_media_sessions("service_stop")

    assert any("camera cleanup failed" in str(error) for error in raised.value.exceptions)
    assert first.stop_reasons == ["service_stop"]
    assert second.stop_reasons == ["service_stop"]
    assert service._media_sessions == {}


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
async def test_plugin_camera_controls_use_sdk_methods_and_bounded_siren_duration(tmp_path):
    sessions = FakeSessions()
    device = FakeAccessoryDevice()
    client = FakeAccessoryClient(device)
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    await service._set_plugin_camera_control("42", "light", True)
    await service._set_plugin_camera_control("42", "light", False)
    await service._set_plugin_camera_control("42", "siren", True)
    await service._set_plugin_camera_control("42", "siren", False)

    assert device.light_values == [True, False]
    assert device.siren_values == [SIREN_ON_DURATION_SECONDS, 0]
    assert SIREN_ON_DURATION_SECONDS == 30
    assert client.submissions == 4
    assert service._camera_control_lock("42") is not service._camera_media_lock("42")


@pytest.mark.asyncio
async def test_mini_v3_exact_kind_override_dispatches_sdk_siren_setter(tmp_path):
    sessions = FakeSessions()
    device = FakeAccessoryDevice(capabilities=("video",))
    device.kind = "stickup_cam_mini_v3"
    client = FakeAccessoryClient(device)
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    await service._set_plugin_camera_control("42", "siren", True)

    assert device.has_capability("siren") is False
    assert device.siren_values == [30]


@pytest.mark.asyncio
async def test_plugin_camera_control_rechecks_sdk_capability_and_contains_failure(tmp_path):
    sessions = FakeSessions()

    class FailingDevice(FakeAccessoryDevice):
        async def async_set_light(self, _value):
            raise RuntimeError("private Ring failure detail")

    unsupported = FakeAccessoryDevice(capabilities=("video",))
    client = FakeAccessoryClient(unsupported)
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    with pytest.raises(PluginCameraOperationError) as raised:
        await service._set_plugin_camera_control("42", "light", True)
    assert raised.value.code == "light_unsupported"
    assert client.submissions == 0

    failing = FailingDevice()
    failing_client = FakeAccessoryClient(failing)
    sessions.active_client = failing_client
    service._media_client = failing_client
    _install_fixture_privacy(service, failing_client)
    with pytest.raises(PluginCameraOperationError) as raised:
        await service._set_plugin_camera_control("42", "light", True)
    assert raised.value.code == "control_failed"
    assert "private Ring failure detail" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["light", "siren"])
async def test_plugin_camera_controls_use_reservation_or_ordering_per_control(tmp_path, control):
    sessions = FakeSessions()
    started = asyncio.Event()
    release = asyncio.Event()
    order = []

    class OrderedDevice(FakeAccessoryDevice):
        async def _set_ordered(self, value):
            order.append(("start", value))
            if value:
                started.set()
                await release.wait()
            order.append(("end", value))

        async def async_set_light(self, value):
            await self._set_ordered(value)

        async def async_set_siren(self, value):
            await self._set_ordered(value)

    class LoopClient(FakeAccessoryClient):
        def __init__(self, device, loop):
            super().__init__(device)
            self.loop = loop

        def submit(self, coroutine):
            self.submissions += 1
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    device = OrderedDevice()
    client = LoopClient(device, asyncio.get_running_loop())
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    first_value = True if control == "light" else SIREN_ON_DURATION_SECONDS
    second_value = False if control == "light" else 0
    first = asyncio.create_task(service._set_plugin_camera_control("42", control, True))
    second = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        requested = asyncio.Event()

        async def queued_control():
            requested.set()
            await service._set_plugin_camera_control("42", control, False)

        # Both controls retain per-camera order. Lighting additionally waits
        # for the exclusive account reservation within its original deadline.
        second = asyncio.create_task(queued_control())
        await asyncio.wait_for(requested.wait(), timeout=1)
        assert not second.done()

        assert order == [("start", first_value)]
        assert client.submissions == 1
        release.set()
        await asyncio.wait_for(first, timeout=1)
        await asyncio.wait_for(second, timeout=1)

        assert client.submissions == 2
        assert order == [
            ("start", first_value),
            ("end", first_value),
            ("start", second_value),
            ("end", second_value),
        ]
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


@pytest.mark.asyncio
async def test_hung_camera_control_times_out_without_blocking_snapshot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("halo_server.service.RING_CONTROL_TIMEOUT_SECONDS", 0.02)
    sessions = FakeSessions()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HungDevice(FakeAccessoryDevice):
        async def async_set_light(self, _value):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    class LoopClient(FakeAccessoryClient):
        def __init__(self, device, loop):
            super().__init__(device)
            self.loop = loop

        def submit(self, coroutine):
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    device = HungDevice()
    client = LoopClient(device, asyncio.get_running_loop())
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    control = asyncio.create_task(service._set_plugin_camera_control("42", "light", True))
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await asyncio.wait_for(service._get_plugin_snapshot("42", False), timeout=1) == (
        b"\xff\xd8cached\xff\xd9"
    )
    with pytest.raises(PluginCameraOperationError) as raised:
        await control

    assert raised.value.code == "control_timeout"
    assert cancelled.is_set()
    assert service._session_change_lock.locked() is False


@pytest.mark.asyncio
async def test_logout_is_not_blocked_by_inflight_camera_control(tmp_path):
    sessions = FakeSessions()
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedDevice(FakeAccessoryDevice):
        async def async_set_light(self, value):
            started.set()
            await release.wait()
            self.light_values.append(value)

    class LoopClient(FakeAccessoryClient):
        def __init__(self, device, loop):
            super().__init__(device)
            self.loop = loop

        def submit(self, coroutine):
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    device = DelayedDevice()
    client = LoopClient(device, asyncio.get_running_loop())
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    control = asyncio.create_task(service._set_plugin_camera_control("42", "light", True))
    await asyncio.wait_for(started.wait(), timeout=1)
    logout = asyncio.create_task(service.handle_admin("logout", {}))
    await asyncio.wait_for(logout, timeout=1)

    assert sessions.active_client is None
    assert control.done() is False

    release.set()
    with pytest.raises(PluginCameraOperationError) as raised:
        await control

    assert raised.value.code == "ring_unavailable"
    assert device.light_values == [True]


@pytest.mark.asyncio
async def test_plugin_snapshot_support_is_independent_of_intercom(tmp_path):
    sessions = FakeSessions()
    device = FakeAccessoryDevice(capabilities=("video",))
    client = FakeAccessoryClient(device)
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    cached = await service._get_plugin_snapshot("42", False)
    fresh = await service._get_plugin_snapshot("42", True)

    assert cached == fresh == b"\xff\xd8cached\xff\xd9"
    assert client.snapshot_calls == [(device, False), (device, True)]
    with pytest.raises(PluginIntercomOpenError) as raised:
        await service._open_plugin_intercom("42")
    assert raised.value.code == "intercom_unsupported"


@pytest.mark.asyncio
async def test_plugin_snapshot_rejects_unknown_capability_and_inactive_session(tmp_path):
    sessions = FakeSessions()
    device = FakeAccessoryDevice(capabilities=())
    client = FakeAccessoryClient(device)
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    with pytest.raises(PluginCameraOperationError) as raised:
        await service._get_plugin_snapshot("42", False)
    assert raised.value.code == "snapshot_unsupported"
    assert client.snapshot_calls == []

    service._account_integrations_ready = False
    with pytest.raises(PluginCameraOperationError) as raised:
        await service._get_plugin_snapshot("42", False)
    assert raised.value.code == "ring_unavailable"


@pytest.mark.asyncio
async def test_plugin_snapshot_contains_submission_and_empty_result_failures(tmp_path):
    sessions = FakeSessions()
    device = FakeAccessoryDevice(capabilities=("video",))
    client = FakeAccessoryClient(device)
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    with pytest.raises(PluginCameraOperationError) as raised:
        await service._get_plugin_snapshot("missing", False)
    assert raised.value.code == "camera_unavailable"

    client.submit_snapshot_for_device = None
    with pytest.raises(PluginCameraOperationError) as raised:
        await service._get_plugin_snapshot("42", False)
    assert raised.value.code == "snapshot_unavailable"

    class FailingSnapshotClient(FakeAccessoryClient):
        def submit_snapshot_for_device(self, _device, *, fresh=False):
            future = concurrent.futures.Future()
            future.set_exception(RuntimeError("private snapshot failure"))
            return future

    failing = FailingSnapshotClient(device)
    sessions.active_client = failing
    service._media_client = failing
    _install_fixture_privacy(service, failing)
    with pytest.raises(PluginCameraOperationError) as raised:
        await service._get_plugin_snapshot("42", False)
    assert raised.value.code == "snapshot_failed"
    assert "private snapshot failure" not in str(raised.value)

    empty = FakeAccessoryClient(device, snapshot=None)
    sessions.active_client = empty
    service._media_client = empty
    _install_fixture_privacy(service, empty)
    with pytest.raises(PluginCameraOperationError) as raised:
        await service._get_plugin_snapshot("42", False)
    assert raised.value.code == "snapshot_unavailable"


@pytest.mark.asyncio
async def test_plugin_snapshot_cancellation_cancels_ring_loop_future(tmp_path):
    sessions = FakeSessions()
    device = FakeAccessoryDevice(capabilities=("video",))

    class PendingSnapshotClient(FakeAccessoryClient):
        def submit_snapshot_for_device(self, _device, *, fresh=False):
            self.snapshot_calls.append((_device, fresh))
            self.future = concurrent.futures.Future()
            return self.future

    client = PendingSnapshotClient(device)
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    task = asyncio.create_task(service._get_plugin_snapshot("42", True))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.future.cancelled() is True
    assert service._session_change_lock.locked() is False


@pytest.mark.asyncio
async def test_plugin_snapshots_submit_concurrently_across_cameras(tmp_path):
    sessions = FakeSessions()
    first = FakeAccessoryDevice(capabilities=("video",))
    second = FakeAccessoryDevice(capabilities=("video",))
    second.id = 43

    class MultiSnapshotClient(FakeAccessoryClient):
        def __init__(self):
            super().__init__(first)
            self.all_devices = [first, second]
            self.futures = {}

        def submit_snapshot_for_device(self, device, *, fresh=False):
            self.snapshot_calls.append((device, fresh))
            future = concurrent.futures.Future()
            self.futures[str(device.id)] = future
            return future

    client = MultiSnapshotClient()
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    first_task = asyncio.create_task(service._get_plugin_snapshot("42", False))
    second_task = asyncio.create_task(service._get_plugin_snapshot("43", False))
    for _ in range(10):
        if len(client.futures) == 2:
            break
        await asyncio.sleep(0)

    assert set(client.futures) == {"42", "43"}
    assert service._session_change_lock.locked() is False

    client.futures["42"].set_result(b"\xff\xd8first\xff\xd9")
    client.futures["43"].set_result(b"\xff\xd8second\xff\xd9")

    assert await asyncio.gather(first_task, second_task) == [
        b"\xff\xd8first\xff\xd9",
        b"\xff\xd8second\xff\xd9",
    ]


@pytest.mark.asyncio
async def test_inflight_snapshot_cannot_publish_after_logout(tmp_path):
    sessions = FakeSessions()
    device = FakeAccessoryDevice(capabilities=("video",))

    class PendingSnapshotClient(FakeAccessoryClient):
        def submit_snapshot_for_device(self, _device, *, fresh=False):
            self.future = concurrent.futures.Future()
            return self.future

    client = PendingSnapshotClient(device)
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True

    snapshot = asyncio.create_task(service._get_plugin_snapshot("42", False))
    await asyncio.sleep(0)
    await service.handle_admin("logout", {})
    client.future.set_result(b"\xff\xd8old-account\xff\xd9")

    with pytest.raises(PluginCameraOperationError) as raised:
        await snapshot
    assert raised.value.code == "ring_unavailable"


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


def _compatible_intercom_camera():
    return SimpleNamespace(
        id=42,
        name="Front Door",
        has_capability=lambda capability: capability == "video",
        generate_async_webrtc_stream=lambda: None,
        on_webrtc_candidate=lambda: None,
        close_webrtc_stream=lambda: None,
        get_ice_servers=lambda: [],
    )


@pytest.mark.asyncio
async def test_cancelled_intercom_start_is_released_before_second_owner_admission(tmp_path):
    entered = threading.Event()
    resume = threading.Event()

    class BlockingSession(FakeMediaSession):
        def acquire(self, purpose, *, lease_id):
            entered.set()
            assert resume.wait(2)
            return super().acquire(purpose, lease_id=lease_id)

    sessions = FakeSessions()
    sessions.client.all_devices = [_compatible_intercom_camera()]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions, BlockingSession)
    service._media_client = sessions.client
    task = asyncio.create_task(service._open_plugin_intercom("42"))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert "42" in service._intercom_leases
    assert not task.done()
    media = service._media_sessions["42"]
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert media.leases == set()
    assert service._intercom_leases == {}
    replacement = await service._open_plugin_intercom("42")
    assert len(media.leases) == 1
    await replacement.close()


@pytest.mark.asyncio
async def test_retiring_capture_blocks_replacement_after_stop_timeout(tmp_path):
    class RetiringSession(FakeMediaSession):
        receiver_retired = False

        def stop_all(self, reason):
            self.stop_reasons.append(reason)
            self.leases.clear()
            self.state = MediaSessionState.STOPPING

    sessions = FakeSessions()
    sessions.client.all_devices = [SimpleNamespace(id=42, name="Camera")]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions, RetiringSession)
    service._media_client = sessions.client
    media, _lease = await service._acquire_rtsp_consumer("42")
    await service._stop_media_sessions("account_replaced")
    service._media_client = sessions.client
    _install_fixture_privacy(service, sessions.client)

    with pytest.raises(RuntimeError, match="unavailable"):
        await service._acquire_rtsp_consumer("42")
    assert service._captures.retiring == {"42": media}
    media.receiver_retired = True
    replacement, lease = await service._acquire_rtsp_consumer("42")
    assert replacement is not media
    await service._close_rtsp_consumer_async("42", lease)


@pytest.mark.asyncio
async def test_inventory_hardware_replacement_retires_capture_even_when_api_id_matches(tmp_path):
    class SourceAwareSession(FakeMediaSession):
        def __init__(self, client, device, *, legacy_audio_enabled=True):
            super().__init__(client, device, legacy_audio_enabled=legacy_audio_enabled)
            self.source = client, device

        def owns_source(self, client, device):
            return self.source[0] is client and self.source[1] is device

    sessions = FakeSessions()
    old = SimpleNamespace(id=42, name="Old hardware")
    sessions.client.all_devices = [old]
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions, SourceAwareSession)
    service._media_client = sessions.client
    media, _lease = await service._acquire_rtsp_consumer("42")
    sessions.client.all_devices = [SimpleNamespace(id=42, name="New hardware")]

    await service._reconcile_refreshed_media(["42"])

    assert media.stop_reasons == ["inventory_refreshed"]
    assert service._media_sessions == {}
    _install_fixture_privacy(service, sessions.client)
    replacement, lease = await service._acquire_rtsp_consumer("42")
    assert replacement is not media
    await service._close_rtsp_consumer_async("42", lease)


def test_account_manager_publication_hides_old_observations_before_service_handoff(tmp_path):
    sessions = FakeSessions()
    sessions.active_client = sessions.client
    service = _service(tmp_path, sessions)
    service._media_client = sessions.client
    service._accept_events = True
    event = NormalizedRingEvent(
        event_id="old",
        camera_id="42",
        camera_name="Old account",
        camera_kind="lpd_v4",
        type=RingEventType.DOORBELL,
        source_kind="ding",
        source_state="active",
        occurred_at=100,
        received_at=10,
        source_expires_in=180,
        is_update=False,
    )
    service._process_event(event)
    epoch = service._plugin_events.epoch
    assert service.status()["events"]["recent"]

    sessions.active_client = FakeClient()

    status = service.status()
    assert status["events"]["recent"] == []
    assert status["incidents"] == {}
    assert status["recent_actions"] == []
    assert service._plugin_events.epoch != epoch
    service._process_listener_event(service._listener_generation, event)
    assert len(service._recent_events) == 1


@pytest.mark.asyncio
async def test_revoked_control_waiting_for_camera_lock_is_never_submitted(tmp_path):
    device = FakeAccessoryDevice()
    client = FakeAccessoryClient(device)
    sessions = FakeSessions()
    sessions.active_client = client
    service = _service(tmp_path, sessions)
    service._media_client = client
    service._account_integrations_ready = True
    allowed = [True]
    lock = service._camera_control_lock("42")
    await lock.acquire()
    task = asyncio.create_task(
        service._set_plugin_camera_control("42", "light", True, admission=lambda: allowed[0])
    )
    await asyncio.sleep(0)
    allowed[0] = False
    lock.release()
    with pytest.raises(PluginCameraOperationError) as raised:
        await task
    assert raised.value.code == "control_superseded"
    assert client.submissions == 0


@pytest.mark.asyncio
async def test_disconnected_login_handler_keeps_full_account_handoff_owned(tmp_path):
    entered = threading.Event()
    resume = threading.Event()

    class BlockingSessions(FakeSessions):
        def login(self, *args):
            entered.set()
            assert resume.wait(2)
            return super().login(*args)

    sessions = BlockingSessions()
    service = _service(tmp_path, sessions)
    task = asyncio.create_task(
        service.handle_admin("login", {"email": "person@example.com", "password": "secret"})
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(service._account_tasks) == 1
    resume.set()
    await asyncio.wait_for(asyncio.gather(*service._account_tasks), 1)
    assert service._media_client is sessions.active_client
    assert service._account_integrations_ready
    await service.stop()


@pytest.mark.asyncio
async def test_slow_initial_restore_does_not_block_admin_or_shutdown_ownership(tmp_path):
    entered = threading.Event()
    resume = threading.Event()

    class SlowRestore(FakeSessions):
        pending_cancelled = False

        def restore(self):
            entered.set()
            assert resume.wait(2)
            return super().restore()

        def cancel_pending_operations(self):
            self.pending_cancelled = True
            resume.set()

    sessions = SlowRestore()
    service = _service(tmp_path, sessions)
    await asyncio.wait_for(service.start(), 0.5)
    assert await asyncio.to_thread(entered.wait, 1)
    assert (await service.handle_admin("status", {}))["account"]["restoring"]
    await asyncio.wait_for(service.stop(), 1)
    assert sessions.pending_cancelled
    assert sessions.shutdown_called
    assert service._initial_restore_task is None
    assert service.status()["shutdown_complete"]


@pytest.mark.asyncio
async def test_immediate_shutdown_never_admits_a_late_initial_restore(tmp_path):
    sessions = FakeSessions()
    service = _service(tmp_path, sessions)
    await service.start()
    await service.stop()
    assert sessions.restore_calls == 0
    assert service._initial_restore_task is None
    assert sessions.shutdown_called
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
