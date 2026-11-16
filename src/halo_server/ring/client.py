"""Transactional Ring account and device session ownership for Halo Server."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from halo_server.account_scope import AccountScope, new_profile_id
from halo_server.camera_privacy import ring_privacy_receipt
from halo_server.ring.credentials import CredentialStore, CredentialStoreError
from halo_server.ring.lighting import LightingReservation
from halo_server.ring.snapshots import SnapshotHttpSession, SnapshotReader

_log = logging.getLogger(__name__)

APP_USER_AGENT = "android:com.ringapp"
DEFAULT_CALL_TIMEOUT = 90
ASYNC_CLOSE_TIMEOUT = 5
LOOP_JOIN_TIMEOUT = 5
LOOP_START_TIMEOUT = 5
DEVICE_REFRESH_TTL = 8


class SessionSupersededError(RuntimeError):
    """Raised when newer login, restore, or logout work invalidates an operation."""


def _same_device_identity(previous: dict | None, current: dict) -> bool:
    """Preserve wrappers only with unchanged family-local replacement evidence."""
    return previous is not None and all(
        previous.get(key) == current.get(key) for key in ("kind", "location_id", "device_id")
    )


def _validate_device_inventory(rows: object) -> None:
    """Reject incomplete/ambiguous SDK rows before they remove live devices."""
    if not isinstance(rows, dict):
        raise ValueError("Invalid Ring device inventory")
    seen: set[int] = set()
    for family in ("stickup_cams", "chimes", "doorbots", "authorized_doorbots", "other"):
        entries = rows.get(family, {})
        if not isinstance(entries, dict):
            raise ValueError("Invalid Ring device inventory")
        for device_id, row in entries.items():
            if (
                type(device_id) is not int
                or device_id <= 0
                or device_id in seen
                or not isinstance(row, dict)
                or type(row.get("id")) is not int
                or row["id"] != device_id
                or not isinstance(row.get("description"), str)
                or not isinstance(row.get("kind"), str)
                or not row["kind"]
            ):
                raise ValueError("Invalid or ambiguous Ring device inventory")
            seen.add(device_id)


class RingClient:
    """Synchronous owner of one async ring-doorbell session and event loop."""

    supports_snapshot_admission = True

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
        self._last_device_update_receipt: tuple[float, int] | None = None
        self._camera_privacy_receipt = None
        self._inventory_refresh_lock = asyncio.Lock()
        self._snapshot_transport: SnapshotHttpSession | None = None
        self._snapshot_reader: SnapshotReader | None = None
        self._session_resources: set[Any] = set()
        self._stopped = False
        self.lighting_reservation = LightingReservation()

    def register_session_resource(self, resource) -> None:
        """Register on the Ring loop; retire before credentials or that loop close."""
        if self._stopped or not self.is_authenticated:
            raise SessionSupersededError("Ring session is no longer active")
        self._session_resources.add(resource)

    def unregister_session_resource(self, resource) -> None:
        self._session_resources.discard(resource)

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

    def snapshot_for_device(self, device, *, fresh: bool = False) -> bytes | None:
        if self._ring is None:
            return None
        return self._run(self._async_snapshot_bytes(device, fresh=fresh))

    def submit_snapshot_for_device(
        self,
        device,
        *,
        fresh: bool = False,
        admission: Callable[[], bool] | None = None,
    ) -> concurrent.futures.Future:
        """Submit one snapshot request to the client-owned Ring event loop."""
        if self._ring is None:
            raise RuntimeError("Not signed in to Ring")
        return self.submit(self._async_snapshot_bytes(device, fresh=fresh, admission=admission))

    def submit(self, coro) -> concurrent.futures.Future:
        """Submit work to the Ring event loop without blocking the caller."""
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        except BaseException:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise

    def stop(self) -> None:
        """Close Ring authentication and terminate the owned event loop."""
        failures: list[Exception] = []
        with self._loop_lock:
            self._stopped = True
            self.lighting_reservation.retire()
            ring = self._detach_session()
            loop = self._loop
            thread = self._loop_thread
            if loop and not loop.is_closed() and loop.is_running():
                if ring is not None:
                    coroutine = self._close_ring_auth(ring)
                    try:
                        close_future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                    except Exception as exc:
                        coroutine.close()
                        failures.append(exc)
                        _log.exception("Could not submit Ring async close")
                    else:
                        try:
                            close_future.result(timeout=ASYNC_CLOSE_TIMEOUT)
                        except concurrent.futures.TimeoutError:
                            close_future.cancel()
                            failure = TimeoutError("Ring async close timed out")
                            failures.append(failure)
                            _log.error("Ring async close did not finish within timeout")
                        except Exception as exc:
                            failures.append(exc)
                            _log.exception("Ring async close failed")
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except Exception as exc:
                    failures.append(exc)
                    _log.exception("Could not request Ring event-loop stop")
            elif ring is not None:
                failure = RuntimeError("Ring event loop was unavailable during auth close")
                failures.append(failure)
                _log.error(str(failure))

            if thread is not None and thread.is_alive():
                thread.join(timeout=LOOP_JOIN_TIMEOUT)
                if thread.is_alive():
                    failure = RuntimeError("Ring asyncio thread did not stop within timeout")
                    failures.append(failure)
                    _log.error(str(failure))

            if thread is None or not thread.is_alive():
                self._loop = None
                self._loop_thread = None
        if failures:
            raise ExceptionGroup("Ring client shutdown failed", failures)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._stopped:
                raise RuntimeError("Ring client is stopped")
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
        future = self.submit(coro)
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
        transport = SnapshotHttpSession()
        auth = None
        try:
            auth = Auth(
                APP_USER_AGENT,
                None,
                self._capture_auth_token,
                http_client_session=transport,
            )
            from halo_server.ring.auth_refresh import install_serialized_auth_refresh

            install_serialized_auth_refresh(auth)
            token = await auth.async_fetch_token(username, password, otp_code)
            if self._pending_auth_token is None:
                self._capture_auth_token(token)
            ring = Ring(auth)
            lighting_revision = self.lighting_reservation.revision
            await self._async_update_inventory(ring)
            self._finish_authentication(auth, ring, transport)
            self._record_inventory_receipt(lighting_revision)
        except BaseException:
            self._pending_auth_token = None
            await self._close_auth(auth, transport)
            raise

    async def _async_authenticate_from_token(self, token: dict[str, Any]) -> None:
        from ring_doorbell import Auth, Ring

        self._authenticated = False
        self._ring = None
        self._pending_auth_token = None
        transport = SnapshotHttpSession()
        auth = None
        try:
            auth = Auth(
                APP_USER_AGENT,
                token,
                self._capture_auth_token,
                http_client_session=transport,
            )
            from halo_server.ring.auth_refresh import install_serialized_auth_refresh

            install_serialized_auth_refresh(auth)
            ring = Ring(auth)
            lighting_revision = self.lighting_reservation.revision
            await self._async_update_inventory(ring)
            self._finish_authentication(auth, ring, transport)
            self._record_inventory_receipt(lighting_revision)
        except BaseException:
            self._pending_auth_token = None
            await self._close_auth(auth, transport)
            raise

    def _capture_auth_token(self, token: dict[str, Any]) -> None:
        self._pending_auth_token = dict(token)

    def _finish_authentication(self, auth, ring, transport: SnapshotHttpSession) -> None:
        if self._stopped:
            raise SessionSupersededError("Ring client was stopped during authentication")
        auth.token_updater = self._capture_auth_token
        self._ring = ring
        self._authenticated = True
        self._snapshot_transport = transport
        self._snapshot_reader = SnapshotReader(ring)

    async def _async_refresh_session_token(self) -> None:
        if self._ring is None:
            raise RuntimeError("Not signed in to Ring")
        await self._ring.auth.async_refresh_tokens()
        await self._async_refresh_devices(None, -1)

    async def _async_refresh_devices(
        self,
        families: set[str] | frozenset[str] | None,
        max_age_seconds: int,
    ) -> list:
        if self._ring is None:
            return []
        async with self._inventory_refresh_lock:
            if time.monotonic() - self._last_device_update_at > max_age_seconds:
                lighting_revision = self.lighting_reservation.revision
                await self._async_update_inventory(self._ring)
                self._record_inventory_receipt(lighting_revision)
        devices = list(self._ring.devices().all_devices)
        if families is None:
            return devices
        return [
            device for device in devices if (getattr(device, "family", None) or "other") in families
        ]

    def _record_inventory_receipt(self, lighting_revision):
        now = time.monotonic()
        self._last_device_update_at = now
        # One immutable publication pairs the actual GET's receipt time with
        # its pre-request mutation revision. Cache hits never rewrite either.
        self._last_device_update_receipt = (now, lighting_revision)
        self._camera_privacy_receipt = ring_privacy_receipt(self.all_devices, now)

    @staticmethod
    async def _async_update_inventory(ring) -> None:
        """Stage the pinned SDK's whole refresh before replacing its cached view.

        SDK 0.9.14 never rebuilds RingDevices; clearing it after update_data is
        too late because update_groups already dereferences removed wrappers.
        A detached view also prevents a failed dings/groups step from exposing
        an authoritative-looking partial inventory to the service.
        """
        from importlib.metadata import version

        from ring_doorbell.ring import Ring

        if not isinstance(ring, Ring):
            # Test/custom clients implementing the public update interface.
            await ring.async_update_data()
            return
        if version("ring-doorbell") != "0.9.14":
            raise RuntimeError("Ring inventory adapter requires ring-doorbell 0.9.14")
        staged = copy.copy(ring)
        staged._devices = None
        await staged.async_update_data()
        _validate_device_inventory(staged.devices_data)
        refreshed = staged.devices()
        previous = ring.devices()
        old_rows = ring.devices_data
        for family in refreshed:
            old_devices = {device.id: device for device in previous[family]}
            for index, device in enumerate(refreshed[family]):
                old = old_devices.get(device.id)
                old_row = old_rows.get(family, {}).get(device.id)
                row = staged.devices_data[family][device.id]
                if old is not None and _same_device_identity(old_row, row):
                    refreshed[family][index] = old
                else:
                    device._ring = ring
        # SDK lookup and family lists must agree, including shared doorbells.
        refreshed._all_devices = {
            device.id: device for family in refreshed for device in refreshed[family]
        }
        # No awaits after this point: publish only the fully successful refresh.
        ring.devices_data = staged.devices_data
        ring.dings_data = staged.dings_data
        ring.groups_data = staged.groups_data
        ring.session = staged.session
        ring.session_refresh_time = staged.session_refresh_time
        ring._devices = refreshed

    async def _async_snapshot_bytes(
        self,
        device,
        *,
        fresh: bool = False,
        admission: Callable[[], bool] | None = None,
    ) -> bytes | None:
        if self._ring is None or (admission is not None and not admission()):
            return None
        if self._snapshot_reader is None:
            self._snapshot_reader = SnapshotReader(self._ring)
        return await self._snapshot_reader.fetch(device, fresh=fresh, admission=admission)

    async def _async_close(self) -> None:
        ring = self._detach_session()

        if ring is not None:
            await self._close_ring_auth(ring)

    def _detach_session(self) -> Any | None:
        ring = self._ring
        self._authenticated = False
        self._ring = None
        self._pending_auth_token = None
        self._token_updater = None
        self._last_device_update_receipt = None
        self._camera_privacy_receipt = None
        return ring

    async def _close_ring_auth(self, ring) -> None:
        reader, transport = self._snapshot_reader, self._snapshot_transport
        self._snapshot_reader = None
        self._snapshot_transport = None
        try:
            resources = tuple(self._session_resources)
            self._session_resources.clear()
            try:
                if resources:
                    results = await asyncio.gather(
                        *(resource.close() for resource in resources), return_exceptions=True
                    )
                    failures = [result for result in results if isinstance(result, Exception)]
                    if failures:
                        raise ExceptionGroup("Ring integration retirement failed", failures)
            finally:
                if reader is not None:
                    await reader.close()
        finally:
            try:
                await ring.auth.async_close()
            finally:
                if transport is not None:
                    await transport.close()

    @staticmethod
    async def _close_auth(auth, transport: SnapshotHttpSession | None = None) -> None:
        try:
            if auth is not None:
                await auth.async_close()
        except Exception as exc:
            _log.debug("Ring auth close failed: %s", exc)
        finally:
            if transport is not None:
                await transport.close()


class RingSessionManager:
    """Serialize and publish one active Halo Server Ring client."""

    supports_account_admission = True

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
        self._active_scope: AccountScope | None = None
        self._pending_clients: dict[int, RingClient] = {}

    @property
    def active_client(self) -> RingClient | None:
        with self._lock:
            return self._client

    @property
    def account_email(self) -> str | None:
        with self._lock:
            return self._account_email

    @property
    def active_scope(self) -> AccountScope | None:
        with self._lock:
            return self._active_scope

    @property
    def active_context(self) -> tuple[RingClient, AccountScope] | None:
        """Read current client and publication authority in one transaction."""
        with self._lock:
            if self._client is None or self._active_scope is None:
                return None
            return self._client, self._active_scope

    def is_current(self, client: RingClient, scope: AccountScope) -> bool:
        with self._lock:
            return self._client is client and self._active_scope == scope

    def has_stored_credentials(self) -> bool:
        """Return whether a cached Ring session is available for restoration."""
        return self._credentials.load_token() is not None

    def prepare_event_listener_credentials(
        self,
        client: RingClient,
    ) -> tuple[dict[str, Any] | None, Callable[[dict[str, Any]], None]]:
        """Bind persisted push credentials to the current authenticated session."""
        with self._lock:
            if self._client is not client or not client.is_authenticated:
                raise SessionSupersededError("Ring session was superseded")
            generation = self._generation
            credentials = self._credentials.load_event_credentials()

            def persist(updated: dict[str, Any]) -> None:
                with self._lock:
                    if generation != self._generation and self._client is not client:
                        raise SessionSupersededError("Ring session was superseded")
                    self._credentials.save_event_credentials(updated)

            return credentials, persist

    def login(
        self,
        email: str,
        password: str,
        otp_code: str | None = None,
        *,
        admission: Callable[[], bool] | None = None,
    ) -> RingClient:
        generation = self._begin_operation(admission)
        candidate = self._client_factory()
        try:
            self._register_candidate(candidate, generation)
            candidate.authenticate(email, password, otp_code)
            # No canonical Ring principal is exposed by the current adapter.
            # Even matching email cannot prove interactive account continuity.
            previous = self._publish(candidate, email, generation, new_profile_id())
        except BaseException:
            _stop_quietly(candidate)
            raise
        finally:
            self._remove_candidate(candidate, generation)
        _stop_quietly(previous)
        return candidate

    def restore(self, *, admission: Callable[[], bool] | None = None) -> RingClient | None:
        generation = self._begin_operation(admission)
        stored = self._credentials.load()
        with self._lock:
            if generation != self._generation:
                return None
        if stored.ring_token is None:
            return None
        candidate = self._client_factory()
        try:
            self._register_candidate(candidate, generation)
            candidate.authenticate_from_token(stored.ring_token)
            self._credentials.assert_revision(stored.revision)
            previous = self._publish(
                candidate,
                stored.account_email or "",
                generation,
                stored.profile_id,
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
        except BaseException:
            _stop_quietly(candidate)
            raise
        finally:
            self._remove_candidate(candidate, generation)
        _stop_quietly(previous)
        return candidate

    def logout(self) -> None:
        clear_error: BaseException | None = None
        with self._lock:
            self._generation += 1
            pending = self._take_pending_locked()
            client = self._client
            self._client = None
            self._account_email = None
            self._active_scope = None
            try:
                self._credentials.clear()
            except BaseException as exc:
                clear_error = exc
        _stop_quietly(client)
        failures = _stop_pending_clients(pending, exclude=client)
        if clear_error is not None:
            raise clear_error
        _raise_stop_failures(failures)

    def shutdown(self) -> None:
        with self._lock:
            self._generation += 1
            pending = self._take_pending_locked()
            client = self._client
            self._client = None
            self._account_email = None
            self._active_scope = None
        failures = _stop_pending_clients([*pending, *([client] if client is not None else [])])
        _raise_stop_failures(failures)

    def cancel_pending_operations(self) -> None:
        """Retire authentication attempts without changing active authority.

        Stop is requested outside the manager lock so an async auth cleanup can
        finish its generation-checked persistence callback without deadlocking.
        A factory that has not returned yet is rejected before authentication.
        """
        with self._lock:
            self._generation += 1
            pending = self._take_pending_locked()
            active = self._client
        _raise_stop_failures(_stop_pending_clients(pending, exclude=active))

    def _take_pending_locked(self) -> list[RingClient]:
        pending = list(self._pending_clients.values())
        self._pending_clients.clear()
        return pending

    def _register_candidate(self, candidate: RingClient, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                raise SessionSupersededError("Ring authentication admission was superseded")
            self._pending_clients[generation] = candidate

    def _remove_candidate(self, candidate: RingClient, generation: int) -> None:
        with self._lock:
            if self._pending_clients.get(generation) is candidate:
                self._pending_clients.pop(generation)

    def _begin_operation(self, admission: Callable[[], bool] | None = None) -> int:
        with self._lock:
            if admission is not None and not admission():
                raise SessionSupersededError("Ring account operation is no longer admitted")
            self._generation += 1
            return self._generation

    def _publish(
        self,
        candidate: RingClient,
        email: str,
        generation: int,
        profile_id: str,
    ) -> RingClient | None:
        with self._lock:
            if generation != self._generation:
                raise SessionSupersededError("A newer Ring login, restore, or logout completed")
            scope = AccountScope(profile_id, generation)

            def persist(token: dict[str, Any]) -> None:
                with self._lock:
                    if generation != self._generation and self._client is not candidate:
                        raise SessionSupersededError("Ring session was superseded")
                    self._credentials.save_session(token, email, profile_id=profile_id)

            candidate.activate_token_persistence(persist)
            previous = self._client
            self._client = candidate
            self._account_email = email or None
            self._active_scope = scope
            return previous

    def _clear_rejected_if_current(self, generation: int) -> None:
        with self._lock:
            active = self._client
            if generation != self._generation or (active is not None and active.is_authenticated):
                return
            self._credentials.clear()
            self._account_email = None
            self._active_scope = None


def _stop_quietly(client: RingClient | None) -> None:
    if client is None:
        return
    try:
        client.stop()
    except Exception as exc:
        _log.debug("Failed to stop discarded Ring client: %s", exc)


def _stop_pending_clients(clients: list[RingClient], *, exclude=None) -> list[Exception]:
    failures = []
    seen: set[int] = set()
    for client in clients:
        if client is exclude or id(client) in seen:
            continue
        seen.add(id(client))
        try:
            client.stop()
        except Exception as exc:
            failures.append(exc)
    return failures


def _raise_stop_failures(failures: list[Exception]) -> None:
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise ExceptionGroup("Ring pending session shutdown failed", failures)


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
