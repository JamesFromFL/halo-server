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
    ):
        self.token = {"refresh_token": token}
        self.login_error = login_error
        self.restore_error = restore_error
        self.login_started = login_started
        self.login_release = login_release
        self.restore_hook = restore_hook
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
async def test_snapshot_uses_cached_endpoint_after_live_snapshot_failure(monkeypatch):
    class FakeDevice:
        id = 42
        name = "Front Door"

        async def async_get_snapshot(self, **_kwargs):
            raise RuntimeError("live snapshot unavailable")

    class FakeRing:
        async def async_query(self, endpoint):
            assert "42" in endpoint
            return SimpleNamespace(status_code=200, content=b"cached")

    client = RingClient()
    client._ring = FakeRing()
    monkeypatch.setattr(
        "ring_doorbell.const.SNAPSHOT_ENDPOINT",
        "/clients_api/snapshots/image/{0}",
    )

    assert await client._async_snapshot_bytes(FakeDevice()) == b"cached"


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
