import threading
from types import SimpleNamespace

import pytest
import ring_doorbell
from ring_doorbell import AuthenticationError, Requires2FAError

from halo_server.ring.client import (
    RingClient,
    RingSessionManager,
    SessionSupersededError,
    is_ring_session_rejected,
)
from halo_server.ring.credentials import (
    CredentialStore,
    CredentialStoreError,
    CredentialSupersededError,
)
from halo_server.ring.snapshots import MAX_SNAPSHOT_BYTES


class _FakeDevices:
    def __init__(self, devices):
        self.all_devices = devices


class _ManagedClient:
    def __init__(
        self,
        token: str,
        *,
        login_error: Exception | None = None,
        restore_error: Exception | None = None,
        login_started: threading.Event | None = None,
        login_release: threading.Event | None = None,
        restore_hook=None,
        stop_error: Exception | None = None,
    ):
        self.token = {"refresh_token": token}
        self.login_error = login_error
        self.restore_error = restore_error
        self.login_started = login_started
        self.login_release = login_release
        self.restore_hook = restore_hook
        self.stop_error = stop_error
        self.is_authenticated = False
        self.stopped = False
        self.persist = None

    def authenticate(self, _email, _password, _otp_code):
        if self.login_started is not None:
            self.login_started.set()
        if self.login_release is not None:
            assert self.login_release.wait(timeout=2)
        if self.login_error is not None:
            raise self.login_error
        self.is_authenticated = True

    def authenticate_from_token(self, _token):
        if self.restore_hook is not None:
            self.restore_hook()
        if self.restore_error is not None:
            raise self.restore_error
        self.is_authenticated = True

    def activate_token_persistence(self, persist):
        persist(self.token)
        self.persist = persist

    def stop(self):
        self.stopped = True
        self.is_authenticated = False
        if self.stop_error is not None:
            raise self.stop_error


class _FailingClearStore:
    def clear(self):
        raise CredentialStoreError("storage unavailable")


def _factory(*clients):
    remaining = list(clients)

    def create():
        return remaining.pop(0)

    return create


@pytest.mark.asyncio
async def test_explicit_auth_defers_token_until_session_is_published(monkeypatch):
    auth_instances = []

    class FakeAuth:
        def __init__(self, _user_agent, _token, token_updater):
            self.token_updater = token_updater
            self.closed = False
            auth_instances.append(self)

        async def async_fetch_token(self, _username, _password, _otp_code):
            token = {"refresh_token": "new"}
            self.token_updater(token)
            return token

        async def async_close(self):
            self.closed = True

    class FakeRing:
        def __init__(self, auth):
            self.auth = auth

        async def async_update_data(self):
            pass

        def devices(self):
            return _FakeDevices([])

    monkeypatch.setattr(ring_doorbell, "Auth", FakeAuth)
    monkeypatch.setattr(ring_doorbell, "Ring", FakeRing)
    persisted = []
    client = RingClient()

    await client._async_authenticate("person@example.com", "password", "123456")

    assert persisted == []
    assert client.is_authenticated is True
    client.activate_token_persistence(persisted.append)
    assert persisted == [{"refresh_token": "new"}]
    auth_instances[0].token_updater({"refresh_token": "refreshed"})
    assert persisted[-1] == {"refresh_token": "refreshed"}
    await client._async_close()
    assert auth_instances[0].closed is True


@pytest.mark.asyncio
async def test_explicit_auth_closes_auth_when_two_factor_is_required(monkeypatch):
    auth_instances = []

    class FakeAuth:
        def __init__(self, _user_agent, _token, _token_updater):
            self.closed = False
            auth_instances.append(self)

        async def async_fetch_token(self, _username, _password, _otp_code):
            raise Requires2FAError

        async def async_close(self):
            self.closed = True

    monkeypatch.setattr(ring_doorbell, "Auth", FakeAuth)
    client = RingClient()

    with pytest.raises(Requires2FAError):
        await client._async_authenticate("person@example.com", "password", None)

    assert auth_instances[0].closed is True
    assert client.is_authenticated is False


@pytest.mark.asyncio
async def test_cached_auth_closes_auth_when_account_validation_fails(monkeypatch):
    auth_instances = []

    class FakeAuth:
        def __init__(self, _user_agent, _token, _token_updater):
            self.closed = False
            auth_instances.append(self)

        async def async_close(self):
            self.closed = True

    class FakeRing:
        def __init__(self, auth):
            self.auth = auth

        async def async_update_data(self):
            raise AuthenticationError("expired")

    monkeypatch.setattr(ring_doorbell, "Auth", FakeAuth)
    monkeypatch.setattr(ring_doorbell, "Ring", FakeRing)
    client = RingClient()

    with pytest.raises(AuthenticationError, match="expired"):
        await client._async_authenticate_from_token({"refresh_token": "old"})

    assert auth_instances[0].closed is True
    assert client.is_authenticated is False


@pytest.mark.asyncio
async def test_device_refresh_uses_ttl_and_family_filter(monkeypatch):
    devices = [
        SimpleNamespace(id=1, family="doorbots"),
        SimpleNamespace(id=2, family="stickup_cams"),
        SimpleNamespace(id=3, family=None),
    ]

    class FakeRing:
        def __init__(self):
            self.updates = 0

        async def async_update_data(self):
            self.updates += 1

        def devices(self):
            return _FakeDevices(devices)

    client = RingClient()
    client._ring = FakeRing()
    client._authenticated = True
    client._last_device_update_at = 0
    monkeypatch.setattr("halo_server.ring.client.time.monotonic", lambda: 100.0)

    selected = await client._async_refresh_devices({"doorbots", "other"}, 8)
    cached = await client._async_refresh_devices(None, 8)

    assert [device.id for device in selected] == [1, 3]
    assert cached == devices
    assert client.ring.updates == 1


@pytest.mark.asyncio
async def test_cached_snapshot_does_not_request_a_fresh_camera_image(monkeypatch):
    class FakeDevice:
        id = 42
        name = "Front Door"

        async def async_get_snapshot(self, **_kwargs):
            raise AssertionError("cached snapshot must not wake the camera")

    class FakeRing:
        async def async_query(self, endpoint):
            assert "42" in endpoint
            return SimpleNamespace(status_code=200, content=b"\xff\xd8cached\xff\xd9")

    client = RingClient()
    client._ring = FakeRing()
    monkeypatch.setattr(
        "ring_doorbell.const.SNAPSHOT_ENDPOINT",
        "/clients_api/snapshots/image/{0}",
    )

    assert await client._async_snapshot_bytes(FakeDevice(), fresh=False) == (
        b"\xff\xd8cached\xff\xd9"
    )


@pytest.mark.asyncio
async def test_fresh_snapshot_returns_live_jpeg_without_cached_request():
    calls = []

    class FakeDevice:
        id = 42
        name = "Front Door"

        async def async_get_snapshot(self, **kwargs):
            calls.append(kwargs)
            return b"\xff\xd8fresh\xff\xd9"

    class FakeRing:
        async def async_query(self, _endpoint):
            raise AssertionError("valid fresh snapshot must not use cached endpoint")

    client = RingClient()
    client._ring = FakeRing()

    assert await client._async_snapshot_bytes(FakeDevice(), fresh=True) == (
        b"\xff\xd8fresh\xff\xd9"
    )
    assert calls == [{"retries": 2, "delay": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fresh_result",
    [
        None,
        b"",
        b"not-a-jpeg",
        b"\xff\xd8missing-end",
        b"\xff\xd8" + b"x" * MAX_SNAPSHOT_BYTES + b"\xff\xd9",
        IndexError("empty snapshot timestamp response"),
    ],
    ids=[
        "none",
        "empty",
        "non-jpeg",
        "truncated-jpeg",
        "oversize",
        "empty-timestamp",
    ],
)
async def test_fresh_snapshot_falls_back_to_cached_jpeg(monkeypatch, fresh_result):
    class FakeDevice:
        id = 42
        name = "Front Door"

        async def async_get_snapshot(self, **_kwargs):
            if isinstance(fresh_result, Exception):
                raise fresh_result
            return fresh_result

    class FakeRing:
        async def async_query(self, endpoint):
            assert "42" in endpoint
            return SimpleNamespace(status_code=200, content=b"\xff\xd8cached\xff\xd9")

    client = RingClient()
    client._ring = FakeRing()
    monkeypatch.setattr(
        "ring_doorbell.const.SNAPSHOT_ENDPOINT",
        "/clients_api/snapshots/image/{0}",
    )

    assert await client._async_snapshot_bytes(FakeDevice(), fresh=True) == (
        b"\xff\xd8cached\xff\xd9"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cached_result",
    [
        SimpleNamespace(status_code=404, content=b"\xff\xd8cached\xff\xd9"),
        SimpleNamespace(status_code=200, content=b""),
        SimpleNamespace(status_code=200, content=b"not-a-jpeg"),
        SimpleNamespace(
            status_code=200,
            content=b"\xff\xd8" + b"x" * MAX_SNAPSHOT_BYTES + b"\xff\xd9",
        ),
        RuntimeError("cached endpoint unavailable"),
    ],
    ids=["http-error", "empty", "non-jpeg", "oversize", "query-error"],
)
async def test_snapshot_rejects_invalid_cached_response(monkeypatch, cached_result):
    class FakeDevice:
        id = 42
        name = "Front Door"

    class FakeRing:
        async def async_query(self, _endpoint):
            if isinstance(cached_result, Exception):
                raise cached_result
            return cached_result

    client = RingClient()
    client._ring = FakeRing()
    monkeypatch.setattr(
        "ring_doorbell.const.SNAPSHOT_ENDPOINT",
        "/clients_api/snapshots/image/{0}",
    )

    assert await client._async_snapshot_bytes(FakeDevice(), fresh=False) is None


def test_login_publishes_validated_session_and_stops_previous(tmp_path):
    first = _ManagedClient("first")
    second = _ManagedClient("second")
    store = CredentialStore(tmp_path / "credentials.json")
    manager = RingSessionManager(store, client_factory=_factory(first, second))

    assert manager.login("first@example.com", "password") is first
    assert manager.login("second@example.com", "password") is second

    assert first.stopped is True
    assert second.stopped is False
    assert manager.active_client is second
    assert manager.account_email == "second@example.com"
    assert store.load().ring_token == {"refresh_token": "second"}


def test_event_credentials_survive_restart_and_are_bound_to_active_client(tmp_path):
    path = tmp_path / "credentials.json"
    first = _ManagedClient("first")
    store = CredentialStore(path)
    manager = RingSessionManager(store, client_factory=lambda: first)
    manager.login("person@example.com", "password")
    initial, persist = manager.prepare_event_listener_credentials(first)

    assert initial is None
    persist({"gcm": {"android_id": "push-device"}})
    manager.shutdown()

    restored = _ManagedClient("restored")
    restarted = RingSessionManager(CredentialStore(path), client_factory=lambda: restored)
    assert restarted.restore() is restored
    credentials, _persist = restarted.prepare_event_listener_credentials(restored)
    assert credentials == {"gcm": {"android_id": "push-device"}}


def test_logout_clears_event_credentials_and_rejects_late_listener_callback(tmp_path):
    client = _ManagedClient("active")
    store = CredentialStore(tmp_path / "credentials.json")
    manager = RingSessionManager(store, client_factory=lambda: client)
    manager.login("person@example.com", "password")
    _credentials, persist = manager.prepare_event_listener_credentials(client)
    persist({"gcm": {"android_id": "push-device"}})

    manager.logout()

    cleared = store.load()
    assert cleared.ring_token is None
    assert cleared.account_email is None
    assert cleared.event_credentials is None
    with pytest.raises(SessionSupersededError, match="superseded"):
        persist({"gcm": {"android_id": "late-device"}})
    assert store.load().event_credentials is None


def test_event_credential_callback_preserves_external_revision_change(tmp_path):
    path = tmp_path / "credentials.json"
    client = _ManagedClient("active")
    store = CredentialStore(path)
    manager = RingSessionManager(store, client_factory=lambda: client)
    manager.login("person@example.com", "password")
    _credentials, persist = manager.prepare_event_listener_credentials(client)
    external = CredentialStore(path)
    external.save_event_credentials({"gcm": {"android_id": "external"}})

    with pytest.raises(CredentialSupersededError, match="stale"):
        persist({"gcm": {"android_id": "stale"}})

    assert CredentialStore(path).load_event_credentials() == {"gcm": {"android_id": "external"}}


def test_failed_replacement_login_does_not_invalidate_active_event_writer(tmp_path):
    active = _ManagedClient("active")
    rejected = _ManagedClient("rejected", login_error=AuthenticationError("rejected"))
    store = CredentialStore(tmp_path / "credentials.json")
    manager = RingSessionManager(store, client_factory=_factory(active, rejected))
    manager.login("person@example.com", "password")
    _credentials, persist = manager.prepare_event_listener_credentials(active)

    with pytest.raises(AuthenticationError):
        manager.login("other@example.com", "wrong")
    persist({"gcm": {"android_id": "still-active"}})

    assert manager.active_client is active
    assert store.load_event_credentials() == {"gcm": {"android_id": "still-active"}}


def test_failed_login_preserves_existing_session_and_credentials(tmp_path):
    first = _ManagedClient("first")
    failed = _ManagedClient("failed", login_error=AuthenticationError("rejected"))
    store = CredentialStore(tmp_path / "credentials.json")
    manager = RingSessionManager(store, client_factory=_factory(first, failed))
    manager.login("first@example.com", "password")

    with pytest.raises(AuthenticationError, match="rejected"):
        manager.login("second@example.com", "wrong")

    assert manager.active_client is first
    assert first.stopped is False
    assert failed.stopped is True
    assert store.load().ring_token == {"refresh_token": "first"}


def test_logout_supersedes_login_in_progress(tmp_path):
    started = threading.Event()
    release = threading.Event()
    candidate = _ManagedClient(
        "late",
        login_started=started,
        login_release=release,
    )
    store = CredentialStore(tmp_path / "credentials.json")
    manager = RingSessionManager(store, client_factory=lambda: candidate)
    errors = []

    login_thread = threading.Thread(
        target=lambda: _capture_error(
            errors,
            lambda: manager.login("person@example.com", "password"),
        )
    )
    login_thread.start()
    assert started.wait(timeout=2)
    manager.logout()
    release.set()
    login_thread.join(timeout=2)

    assert len(errors) == 1
    assert isinstance(errors[0], SessionSupersededError)
    assert manager.active_client is None
    assert candidate.stopped is True
    assert store.load().ring_token is None


def test_restore_clears_only_definitively_rejected_credentials(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    store.save_session({"refresh_token": "old"}, "person@example.com")
    rejected = _ManagedClient(
        "unused",
        restore_error=AuthenticationError("expired"),
    )
    manager = RingSessionManager(store, client_factory=lambda: rejected)

    assert manager.restore() is None

    assert rejected.stopped is True
    assert store.load().ring_token is None


def test_restore_preserves_credentials_on_ring_session_406(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    store.save_session({"refresh_token": "old"}, "person@example.com")
    rejected = _ManagedClient(
        "unused",
        restore_error=RuntimeError(
            "HTTP error with status code 406 during query of url "
            "https://api.ring.com/clients_api/session: 406, message='Not Acceptable'"
        ),
    )
    manager = RingSessionManager(store, client_factory=lambda: rejected)

    assert manager.restore() is None

    assert store.load().ring_token == {"refresh_token": "old"}


def test_stored_credentials_presence_tracks_session_token(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    manager = RingSessionManager(store)

    assert manager.has_stored_credentials() is False
    store.save_session({"refresh_token": "old"}, "person@example.com")
    assert manager.has_stored_credentials() is True
    store.clear()
    assert manager.has_stored_credentials() is False


def test_restore_surfaces_concurrent_credential_revision_change(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    store.save_session({"refresh_token": "old"}, "person@example.com")
    external = CredentialStore(path)
    candidate = _ManagedClient(
        "restored",
        restore_hook=lambda: external.save_token({"refresh_token": "external"}),
    )
    manager = RingSessionManager(store, client_factory=lambda: candidate)

    with pytest.raises(CredentialSupersededError):
        manager.restore()

    assert manager.active_client is None
    assert candidate.stopped is True
    assert CredentialStore(path).load_token() == {"refresh_token": "external"}


def test_logout_stops_session_even_when_credential_clear_fails():
    client = _ManagedClient("active")
    manager = RingSessionManager(_FailingClearStore(), client_factory=lambda: client)
    manager._client = client
    manager._account_email = "person@example.com"

    with pytest.raises(CredentialStoreError, match="storage unavailable"):
        manager.logout()

    assert client.stopped is True
    assert manager.active_client is None
    assert manager.account_email is None


def test_shutdown_surfaces_ring_client_stop_failure(tmp_path):
    client = _ManagedClient("active", stop_error=RuntimeError("loop did not stop"))
    manager = RingSessionManager(
        CredentialStore(tmp_path / "credentials.json"),
        client_factory=lambda: client,
    )
    manager._client = client
    manager._account_email = "person@example.com"

    with pytest.raises(RuntimeError, match="loop did not stop"):
        manager.shutdown()

    assert client.stopped is True
    assert manager.active_client is None
    assert manager.account_email is None


def test_ring_client_stop_surfaces_async_auth_close_failure(monkeypatch):
    class FakeLoop:
        def __init__(self):
            self.stop_requested = False

        def is_closed(self):
            return False

        def is_running(self):
            return True

        def stop(self):
            self.stop_requested = True

        def call_soon_threadsafe(self, callback):
            callback()

    class FakeThread:
        def __init__(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            assert timeout > 0
            self.alive = False

    class FailingFuture:
        def result(self, *, timeout):
            assert timeout > 0
            raise RuntimeError("auth close failed")

    def submit(coroutine, _loop):
        coroutine.close()
        return FailingFuture()

    monkeypatch.setattr(
        "halo_server.ring.client.asyncio.run_coroutine_threadsafe",
        submit,
    )
    client = RingClient()
    loop = FakeLoop()
    client._ring = SimpleNamespace(auth=object())
    client._authenticated = True
    client._loop = loop
    client._loop_thread = FakeThread()

    with pytest.raises(ExceptionGroup, match="Ring client shutdown failed") as raised:
        client.stop()

    assert any("auth close failed" in str(error) for error in raised.value.exceptions)
    assert loop.stop_requested is True
    assert client._loop is None
    assert client._loop_thread is None


def test_ring_client_stop_quarantines_session_when_event_loop_is_unavailable():
    client = RingClient()
    client._ring = SimpleNamespace(auth=object())
    client._authenticated = True

    with pytest.raises(ExceptionGroup, match="Ring client shutdown failed") as raised:
        client.stop()

    assert any("event loop was unavailable" in str(error) for error in raised.value.exceptions)
    assert client.is_authenticated is False
    assert client.ring is None


def test_is_ring_session_rejected_is_specific_to_session_406():
    rejected = RuntimeError(
        "HTTP error with status code 406 during query of url "
        "https://api.ring.com/clients_api/session: 406, message='Not Acceptable'"
    )

    assert is_ring_session_rejected(rejected) is True
    assert is_ring_session_rejected(RuntimeError("status code 500")) is False


def _capture_error(errors, operation):
    try:
        operation()
    except BaseException as exc:
        errors.append(exc)
