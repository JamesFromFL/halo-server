"""Transactional Ring account and device session ownership for Halo Server."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from halo_server.ring.credentials import CredentialStore, CredentialStoreError

_log = logging.getLogger(__name__)

APP_USER_AGENT = "android:com.ringapp"
DEFAULT_CALL_TIMEOUT = 90
ASYNC_CLOSE_TIMEOUT = 5
LOOP_JOIN_TIMEOUT = 5
LOOP_START_TIMEOUT = 5
DEVICE_REFRESH_TTL = 8


class SessionSupersededError(RuntimeError):
    """Raised when newer login, restore, or logout work invalidates an operation."""


class RingClient:
    """Synchronous owner of one async ring-doorbell session and event loop."""

    def __init__(self) -> None:
        self._ring = None
        self._authenticated = False
        self._pending_auth_token: dict[str, Any] | None = None
        self._token_updater: Callable[[dict[str, Any]], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_start_error: BaseException | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._last_device_update_at = 0.0

    @property
    def ring(self):
        """Return the authenticated Ring object for server-owned integrations."""
        return self._ring

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated and self._ring is not None

    @property
    def all_devices(self) -> list:
        if self._ring is None:
            return []
        return list(self._ring.devices().all_devices)

    def authenticate(self, username: str, password: str, otp_code: str | None = None) -> None:
        self._run(self._async_authenticate(username, password, otp_code))

    def authenticate_from_token(self, token: dict[str, Any]) -> None:
        self._run(self._async_authenticate_from_token(token))

    def activate_token_persistence(
        self,
        token_updater: Callable[[dict[str, Any]], None],
    ) -> None:
        """Publish deferred credentials only after the session wins ownership."""
        if not self.is_authenticated or self._ring is None:
            raise RuntimeError("Cannot publish an unauthenticated Ring client")
        if self._pending_auth_token is not None:
            token_updater(self._pending_auth_token)
            self._pending_auth_token = None
        self._token_updater = token_updater
        self._ring.auth.token_updater = token_updater

    def refresh_session_token(self) -> None:
        if self._ring is None:
            raise RuntimeError("Not signed in to Ring")
        self._run(self._async_refresh_session_token())

    def refresh_devices(
        self,
        families: set[str] | frozenset[str] | None = None,
        *,
        max_age_seconds: int = DEVICE_REFRESH_TTL,
    ) -> list:
        if self._ring is None:
            return []
        return self._run(self._async_refresh_devices(families, max_age_seconds))

    def get_device_by_id(self, device_id: int) -> Any | None:
        for device in self.all_devices:
            if getattr(device, "id", None) == device_id:
                return device
        return None

    def snapshot_for_device(self, device) -> bytes | None:
        if self._ring is None:
            return None
        return self._run(self._async_snapshot_bytes(device))

    def submit(self, coro) -> concurrent.futures.Future:
        """Submit work to the Ring event loop without blocking the caller."""
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())

    def stop(self) -> None:
        """Close Ring authentication and terminate the owned event loop."""
        with self._loop_lock:
            loop = self._loop
            thread = self._loop_thread
            if loop and not loop.is_closed() and loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(self._async_close(), loop).result(
                        timeout=ASYNC_CLOSE_TIMEOUT
                    )
                except Exception as exc:
                    _log.debug("Error during Ring async close: %s", exc)
                loop.call_soon_threadsafe(loop.stop)

            if thread is not None and thread.is_alive():
                thread.join(timeout=LOOP_JOIN_TIMEOUT)
                if thread.is_alive():
                    _log.warning("Ring asyncio thread did not stop within timeout")
                    return

            self._loop = None
            self._loop_thread = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            thread_alive = self._loop_thread is not None and self._loop_thread.is_alive()
            loop_ready = (
                self._loop is not None
                and not self._loop.is_closed()
                and self._loop.is_running()
                and thread_alive
            )
            if not loop_ready:
                ready = threading.Event()
                self._loop = None
                self._loop_start_error = None
                self._loop_thread = threading.Thread(
                    target=self._run_loop,
                    args=(ready,),
                    daemon=True,
                    name="halo-ring-asyncio",
                )
                self._loop_thread.start()
                if not ready.wait(timeout=LOOP_START_TIMEOUT):
                    raise RuntimeError("Ring asyncio loop did not start")
                if self._loop_start_error is not None:
                    raise RuntimeError(
                        "Ring asyncio loop failed to start"
                    ) from self._loop_start_error
                if self._loop is None:
                    raise RuntimeError("Ring asyncio loop started without an event loop")
            return self._loop

    def _run_loop(self, ready: threading.Event) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        except BaseException as exc:
            self._loop_start_error = exc
            ready.set()
            return

        ready.set()
        try:
            loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _run(self, coro) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return future.result(timeout=DEFAULT_CALL_TIMEOUT)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("Ring API request timed out") from None

    async def _async_authenticate(
        self,
        username: str,
        password: str,
        otp_code: str | None,
    ) -> None:
        from ring_doorbell import Auth, Ring

        self._authenticated = False
        self._ring = None
        self._pending_auth_token = None
        auth = Auth(APP_USER_AGENT, None, self._capture_auth_token)
        try:
            token = await auth.async_fetch_token(username, password, otp_code)
            if self._pending_auth_token is None:
                self._capture_auth_token(token)
            ring = Ring(auth)
            await ring.async_update_data()
            self._finish_authentication(auth, ring)
            self._last_device_update_at = time.monotonic()
        except BaseException:
            self._pending_auth_token = None
            await self._close_auth(auth)
            raise

    async def _async_authenticate_from_token(self, token: dict[str, Any]) -> None:
        from ring_doorbell import Auth, Ring

        self._authenticated = False
        self._ring = None
        self._pending_auth_token = None
        auth = Auth(APP_USER_AGENT, token, self._capture_auth_token)
        try:
            ring = Ring(auth)
            await ring.async_update_data()
            self._finish_authentication(auth, ring)
            self._last_device_update_at = time.monotonic()
        except BaseException:
            self._pending_auth_token = None
            await self._close_auth(auth)
            raise

    def _capture_auth_token(self, token: dict[str, Any]) -> None:
        self._pending_auth_token = dict(token)

    def _finish_authentication(self, auth, ring) -> None:
        auth.token_updater = self._capture_auth_token
        self._ring = ring
        self._authenticated = True

    async def _async_refresh_session_token(self) -> None:
        if self._ring is None:
            raise RuntimeError("Not signed in to Ring")
        await self._ring.auth.async_refresh_tokens()
        await self._ring.async_update_data()
        self._last_device_update_at = time.monotonic()

    async def _async_refresh_devices(
        self,
        families: set[str] | frozenset[str] | None,
        max_age_seconds: int,
    ) -> list:
        if self._ring is None:
            return []
        if time.monotonic() - self._last_device_update_at > max_age_seconds:
            await self._ring.async_update_data()
            self._last_device_update_at = time.monotonic()
        devices = list(self._ring.devices().all_devices)
        if families is None:
            return devices
        return [
            device for device in devices if (getattr(device, "family", None) or "other") in families
        ]

    async def _async_snapshot_bytes(self, device) -> bytes | None:
        try:
            snapshot = await device.async_get_snapshot(retries=2, delay=1)
            if snapshot:
                return bytes(snapshot)
        except Exception as exc:
            _log.debug("Snapshot fetch failed for %s: %s", device.name, exc)

        try:
            from ring_doorbell.const import SNAPSHOT_ENDPOINT

            response = await self._ring.async_query(SNAPSHOT_ENDPOINT.format(device.id))
            if response.status_code == 200 and response.content:
                return bytes(response.content)
        except Exception as exc:
            _log.debug("Cached snapshot fetch failed for %s: %s", device.name, exc)
        return None

    async def _async_close(self) -> None:
        ring = self._ring
        self._authenticated = False
        self._ring = None
        self._pending_auth_token = None
        self._token_updater = None
        if ring is not None:
            await self._close_auth(ring.auth)

    @staticmethod
    async def _close_auth(auth) -> None:
        try:
            await auth.async_close()
        except Exception as exc:
            _log.debug("Ring auth close failed: %s", exc)


class RingSessionManager:
    """Serialize and publish one active Halo Server Ring client."""

    def __init__(
        self,
        credentials: CredentialStore,
        *,
        client_factory: Callable[[], RingClient] = RingClient,
    ) -> None:
        self._credentials = credentials
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._generation = 0
        self._client: RingClient | None = None
        self._account_email: str | None = None

    @property
    def active_client(self) -> RingClient | None:
        with self._lock:
            return self._client

    @property
    def account_email(self) -> str | None:
        with self._lock:
            return self._account_email

    def has_stored_credentials(self) -> bool:
        """Return whether a cached Ring session is available for restoration."""
        return self._credentials.load_token() is not None

    def login(
        self,
        email: str,
        password: str,
        otp_code: str | None = None,
    ) -> RingClient:
        generation = self._begin_operation()
        candidate = self._client_factory()
        try:
            candidate.authenticate(email, password, otp_code)
            previous = self._publish(candidate, email, generation)
        except BaseException:
            _stop_quietly(candidate)
            raise
        _stop_quietly(previous)
        return candidate

    def restore(self) -> RingClient | None:
        stored = self._credentials.load()
        if stored.ring_token is None:
            return None

        generation = self._begin_operation()
        candidate = self._client_factory()
        try:
            candidate.authenticate_from_token(stored.ring_token)
            self._credentials.assert_revision(stored.revision)
            previous = self._publish(
                candidate,
                stored.account_email or "",
                generation,
            )
        except CredentialStoreError:
            _stop_quietly(candidate)
            raise
        except Exception as exc:
            _stop_quietly(candidate)
            if _should_clear_cached_token(exc):
                _log.info("Stored Ring session was rejected; cached credentials were cleared")
                self._clear_rejected_if_current(generation)
            else:
                _log.warning(
                    "Stored Ring session restore failed with %s; cached credentials preserved",
                    type(exc).__name__,
                )
            return None
        _stop_quietly(previous)
        return candidate

    def logout(self) -> None:
        clear_error: BaseException | None = None
        with self._lock:
            self._generation += 1
            client = self._client
            self._client = None
            self._account_email = None
            try:
                self._credentials.clear()
            except BaseException as exc:
                clear_error = exc
        _stop_quietly(client)
        if clear_error is not None:
            raise clear_error

    def shutdown(self) -> None:
        with self._lock:
            self._generation += 1
            client = self._client
            self._client = None
            self._account_email = None
        _stop_quietly(client)

    def _begin_operation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def _publish(
        self,
        candidate: RingClient,
        email: str,
        generation: int,
    ) -> RingClient | None:
        with self._lock:
            if generation != self._generation:
                raise SessionSupersededError("A newer Ring login, restore, or logout completed")

            def persist(token: dict[str, Any]) -> None:
                with self._lock:
                    if generation != self._generation and self._client is not candidate:
                        raise SessionSupersededError("Ring session was superseded")
                    self._credentials.save_session(token, email)

            candidate.activate_token_persistence(persist)
            previous = self._client
            self._client = candidate
            self._account_email = email or None
            return previous

    def _clear_rejected_if_current(self, generation: int) -> None:
        with self._lock:
            active = self._client
            if generation != self._generation or (active is not None and active.is_authenticated):
                return
            self._credentials.clear()
            self._account_email = None


def _stop_quietly(client: RingClient | None) -> None:
    if client is None:
        return
    try:
        client.stop()
    except Exception as exc:
        _log.debug("Failed to stop discarded Ring client: %s", exc)


def is_ring_session_rejected(exc: Exception) -> bool:
    message = str(exc)
    return (
        "status code 406" in message
        and "/clients_api/session" in message
        and "Not Acceptable" in message
    )


def _should_clear_cached_token(exc: Exception) -> bool:
    from ring_doorbell import AuthenticationError

    return isinstance(exc, AuthenticationError)
