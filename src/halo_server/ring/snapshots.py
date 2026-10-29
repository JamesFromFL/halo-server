"""Bound Ring snapshot bodies before the pinned SDK buffers them.

Pass SnapshotHttpSession through Auth's public http_client_session argument.
Authentication and token refresh remain owned by the SDK. Only its exact image
and timestamp endpoints are intercepted; other SDK requests are delegated.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import ssl
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from ring_doorbell.exceptions import RingError

from halo_server.ring.power import MAINS_ONLY_KINDS

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_TIMESTAMP_BYTES = 64 * 1024
SNAPSHOT_TOTAL_SECONDS = 8.0
MAX_SNAPSHOT_REQUESTS = 2
_TLS_CONTEXT = ssl.create_default_context()
_LOG = logging.getLogger(__name__)
_DIAGNOSTIC_SECONDS = 5.0
_REASONS = frozenset(
    {
        "authority_retired",
        "capacity",
        "closed",
        "transport_missing",
        "invalid_device",
        "cached_missing",
        "unauthorized",
        "forbidden",
        "rate_limited",
        "redirect_rejected",
        "http_error",
        "timeout",
        "network_error",
        "tls_error",
        "encoding_rejected",
        "size_limit",
        "byte_budget",
        "invalid_jpeg",
        "endpoint_rejected",
        "parameters_rejected",
        "operation_rejected",
        "sdk_contract",
        "cancelled",
        "sdk_error",
        "invalid_timestamp",
        "source_empty",
        "operation_failed",
    }
)
_ERROR_CLASSES = frozenset(
    {
        "RingError",
        "RingTimeout",
        "AuthenticationError",
        "SnapshotError",
        "TimeoutError",
        "ClientError",
        "ClientResponseError",
        "ClientConnectorError",
        "ClientConnectorCertificateError",
        "ClientConnectorSSLError",
        "ClientOSError",
        "ClientPayloadError",
        "ServerDisconnectedError",
        "ServerTimeoutError",
        "ConnectionTimeoutError",
        "SocketTimeoutError",
        "CancelledError",
        "RuntimeError",
        "ValueError",
        "KeyError",
        "IndexError",
        "ClientConnectionError",
        "ServerConnectionError",
        "ClientSSLError",
        "SSLError",
        "SSLCertVerificationError",
    }
)


@dataclass
class _SnapshotScope:
    image_remaining: int
    metadata_remaining: int
    admission: Callable[[], bool] | None = None
    phase: str = "cached"
    stage: str = "admission"
    http_status: int | None = None
    reason: str | None = None
    error_class: str = "none"
    success: bool = False


_snapshot_scope: ContextVar[_SnapshotScope | None] = ContextVar("halo_snapshot_scope", default=None)


class SnapshotError(RuntimeError):
    """Safe, content-free snapshot transport failure."""

    def __init__(self, message, *, reason="operation_failed"):
        if reason not in _REASONS:
            raise ValueError("Invalid snapshot failure reason")
        self.reason = reason
        super().__init__(message)


def _http_reason(scope):
    status = scope.http_status
    if status == 404 and scope.stage == "image" and scope.phase == "cached":
        return "cached_missing"
    return {
        401: "unauthorized",
        403: "forbidden",
        429: "rate_limited",
    }.get(
        status, "redirect_rejected" if status is not None and 300 <= status < 400 else "http_error"
    )


def _exception_reason(scope, error):
    # The SDK wraps transport errors; inspect only types and our own fixed code,
    # never exception strings, URLs, bodies or traceback contents.
    current = error
    seen = set()
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__
        scope.error_class = name if name in _ERROR_CLASSES else "other"
        if isinstance(current, SnapshotError):
            scope.reason = current.reason
        elif isinstance(current, asyncio.CancelledError):
            scope.reason = "cancelled"
            break
        elif isinstance(current, TimeoutError):
            scope.reason = "timeout"
            break
        elif isinstance(current, aiohttp.ClientResponseError):
            if type(current.status) is int and 100 <= current.status <= 599:
                scope.http_status = current.status
            scope.reason = _http_reason(scope)
        elif isinstance(
            current,
            (
                ssl.SSLError,
                aiohttp.ClientConnectorCertificateError,
                aiohttp.ClientConnectorSSLError,
            ),
        ):
            scope.reason = "tls_error"
        elif isinstance(current, aiohttp.ClientError):
            scope.reason = "network_error"
        elif (
            isinstance(current, (IndexError, KeyError, ValueError)) and scope.stage == "timestamps"
        ):
            scope.reason = "invalid_timestamp"
        current = current.__cause__ or current.__context__
    if scope.reason is None:
        scope.reason = (
            _http_reason(scope)
            if scope.http_status is not None and scope.http_status >= 300
            else ("sdk_error" if isinstance(error, RingError) else "operation_failed")
        )


def _endpoint(url: Any, method: str) -> int | None:
    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except ValueError:
        raise SnapshotError("Invalid snapshot endpoint", reason="endpoint_rejected") from None
    if not parsed.path.startswith("/clients_api/snapshots"):
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.ring.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SnapshotError("Untrusted snapshot endpoint", reason="endpoint_rejected")
    if method == "GET" and re.fullmatch(r"/clients_api/snapshots/image/[0-9]{1,20}", parsed.path):
        return MAX_SNAPSHOT_BYTES
    if method == "POST" and parsed.path == "/clients_api/snapshots/timestamps":
        return MAX_TIMESTAMP_BYTES
    raise SnapshotError("Unsupported snapshot operation", reason="operation_rejected")


class _SnapshotResponse:
    """SDK response facade whose read never calls the eager response.read()."""

    def __init__(self, response, limit: int, budget=None, image=False) -> None:
        self._response = response
        self._limit = limit
        self._body: bytes | None = None
        self._budget = budget
        self._stage = budget.stage if budget is not None else None
        self._image = image
        self._validated = False

    def __getattr__(self, name):
        return getattr(self._response, name)

    def _observe_response(self):
        if self._budget is not None:
            self._budget.stage = self._stage
            status = getattr(self._response, "status", None)
            self._budget.http_status = (
                status if type(status) is int and 100 <= status <= 599 else None
            )

    def raise_for_status(self):
        # Auth may refresh a token before checking the ORIGINAL 401 response.
        # Restore that response's provenance instead of reporting OAuth's200.
        self._observe_response()
        return self._response.raise_for_status()

    async def __aenter__(self):
        await self._response.__aenter__()
        self._validate_headers()
        return self

    def _validate_headers(self):
        if self._validated:
            return
        try:
            encoding = self._response.headers.get("Content-Encoding", "identity").strip().lower()
            if encoding != "identity":
                raise SnapshotError(
                    "Snapshot response encoding is unsupported", reason="encoding_rejected"
                )
            declared = self._response.headers.get("Content-Length")
            if declared is not None and (
                not re.fullmatch(r"[0-9]+", declared) or int(declared) > self._limit
            ):
                raise SnapshotError("Snapshot response exceeds size limit", reason="size_limit")
        except BaseException:
            self._response.close()
            raise
        self._validated = True

    async def __aexit__(self, *args):
        return await self._response.__aexit__(*args)

    async def read(self) -> bytes:
        self._observe_response()
        self._validate_headers()
        if self._body is None:
            result = bytearray()
            while True:
                # One extra byte establishes overflow without materializing
                # the remainder. aiohttp/TCP may independently read ahead.
                chunk = await self._response.content.read(
                    min(16 * 1024, self._limit - len(result) + 1)
                )
                if not chunk:
                    break
                result.extend(chunk)
                if self._budget is not None:
                    if self._image:
                        self._budget.image_remaining -= len(chunk)
                    else:
                        self._budget.metadata_remaining -= len(chunk)
                if len(result) > self._limit:
                    self._response.close()
                    raise SnapshotError("Snapshot response exceeds size limit", reason="size_limit")
            self._body = bytes(result)
        return self._body

    async def text(self, encoding="utf-8", **_kwargs) -> str:
        return (await self.read()).decode(encoding)

    async def json(self, **_kwargs):
        return json.loads(await self.read())


class _SessionRequest:
    """Public aiohttp request await/context behavior used by Auth and FCM."""

    def __init__(self, coroutine) -> None:
        self._coroutine = coroutine
        self._response = None

    def __await__(self):
        return self._coroutine.__await__()

    async def __aenter__(self):
        self._response = await self._coroutine
        return await self._response.__aenter__()

    async def __aexit__(self, *args):
        return await self._response.__aexit__(*args)


class SnapshotHttpSession:
    """Owned SDK session adapter using only public request/response APIs.

    The owning RingClient closes this provided session; Auth.async_close only
    closes sessions Auth created itself. Construction/request/close use the
    same persistent SDK loop. The exact SDK version is an explicit contract.
    """

    def __init__(self, *, session=None) -> None:
        if version("ring-doorbell") != "0.9.14":
            raise SnapshotError("Unsupported snapshot SDK contract", reason="sdk_contract")
        self._session = session or aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(), trust_env=False
        )

    def request(self, method, url, **kwargs):
        return _SessionRequest(self._request(method, url, **kwargs))

    def post(self, url, **kwargs):
        # Ring passes this same provided session to firebase-messaging, whose
        # registration uses async-with post(), rather than awaiting request().
        return self.request("POST", url, **kwargs)

    async def _request(self, method, url, **kwargs):
        limit = _endpoint(url, method.upper())
        budget = _snapshot_scope.get()
        if budget is not None and budget.admission is not None and not budget.admission():
            raise SnapshotError("Snapshot authority was retired", reason="authority_retired")
        image = limit == MAX_SNAPSHOT_BYTES
        stage = "image" if image else "timestamps" if limit is not None else "token"
        if limit is None:
            if budget is None:
                return await self._session.request(method, url, **kwargs)
            from ring_doorbell.const import OAuth

            if method.upper() != "POST" or str(url) != OAuth.ENDPOINT:
                raise SnapshotError(
                    "Unexpected snapshot request endpoint", reason="endpoint_rejected"
                )
            limit = MAX_TIMESTAMP_BYTES
        if budget is not None:
            budget.stage = stage
            budget.http_status = None
            budget.reason = None
            budget.error_class = "none"
            limit = min(limit, budget.image_remaining if image else budget.metadata_remaining)
            if limit <= 0:
                raise SnapshotError(
                    "Snapshot operation byte budget exhausted", reason="byte_budget"
                )
        if kwargs.get("params"):
            raise SnapshotError("Unexpected snapshot parameters", reason="parameters_rejected")
        headers = dict(kwargs.pop("headers", {}))
        headers["Accept-Encoding"] = "identity"
        kwargs.update(
            headers=headers,
            allow_redirects=False,
            auto_decompress=False,
            ssl=_TLS_CONTEXT,
            timeout=aiohttp.ClientTimeout(total=7, connect=3, sock_connect=3, sock_read=2),
        )
        response = await self._session.request(method, url, **kwargs)
        if budget is not None:
            status = getattr(response, "status", None)
            budget.http_status = status if type(status) is int and 100 <= status <= 599 else None
        return _SnapshotResponse(response, limit, budget, image)

    @property
    def closed(self) -> bool:
        return self._session.closed

    async def close(self) -> None:
        await self._session.close()


class SnapshotReader:
    """Finite per-client snapshot admission and whole-operation cancellation."""

    def __init__(self, ring) -> None:
        self._ring = ring
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._diagnostic_last = float("-inf")

    @staticmethod
    def _can_refresh_missing_cache(device):
        # A false/missing battery flag alone never proves mains power. Require
        # both an already-reviewed mains-only kind and explicit SDK evidence.
        try:
            checker = getattr(device, "has_capability", None)
            return (
                getattr(device, "kind", None) in MAINS_ONLY_KINDS
                and callable(checker)
                and checker("video") is True
                and checker("battery") is False
                and callable(getattr(device, "async_get_snapshot", None))
            )
        except Exception:
            return False

    async def _refresh_missing_cache(self, device, scope, admission):
        if admission is not None and not admission():
            scope.reason = "authority_retired"
            return None
        scope.phase = "fresh"
        scope.stage = "reader"
        scope.http_status = None
        scope.reason = None
        scope.error_class = "none"
        raw = await device.async_get_snapshot(retries=2, delay=1)
        if admission is not None and not admission():
            scope.reason = "authority_retired"
            return None
        image = validated_snapshot_bytes(raw)
        scope.success = image is not None
        if image is None:
            scope.reason = "source_empty" if raw is None else "invalid_jpeg"
        return image

    def _diagnostic(self, scope, fresh, began):
        # One scalar throttle is not operation state. Every reason/status/type
        # above belongs to the individual fetch's ContextVar scope.
        with contextlib.suppress(BaseException):
            now = time.monotonic()
            if scope.success or now - self._diagnostic_last < _DIAGNOSTIC_SECONDS:
                return
            self._diagnostic_last = now
            _LOG.warning(
                "Ring snapshot result phase=%s stage=%s reason=%s http_status=%s "
                "error_class=%s fresh_requested=%s elapsed_ms=%d",
                scope.phase if scope.phase in {"fresh", "cached"} else "cached",
                scope.stage
                if scope.stage in {"admission", "image", "timestamps", "token", "reader"}
                else "reader",
                scope.reason if scope.reason in _REASONS else "operation_failed",
                scope.http_status,
                scope.error_class
                if scope.error_class in _ERROR_CLASSES or scope.error_class == "none"
                else "other",
                fresh is True,
                min(3_600_000, max(0, int((now - began) * 1000))),
            )

    async def fetch(
        self,
        device,
        *,
        fresh: bool = False,
        admission: Callable[[], bool] | None = None,
    ) -> bytes | None:
        began = time.monotonic()
        scope = _SnapshotScope(MAX_SNAPSHOT_BYTES, 4 * MAX_TIMESTAMP_BYTES, admission)
        scope.phase = "fresh" if fresh else "cached"
        if admission is not None and not admission():
            scope.reason = "authority_retired"
            self._diagnostic(scope, fresh, began)
            return None
        if self._closed or len(self._tasks) >= MAX_SNAPSHOT_REQUESTS:
            scope.reason = "closed" if self._closed else "capacity"
            self._diagnostic(scope, fresh, began)
            return None
        # A missing transport must fail closed, never use the SDK's raw reader.
        if not isinstance(
            getattr(self._ring.auth, "http_client_session", None), SnapshotHttpSession
        ):
            scope.reason = "transport_missing"
            self._diagnostic(scope, fresh, began)
            return None
        task = asyncio.current_task()
        self._tasks.add(task)
        scope_token = _snapshot_scope.set(scope)
        try:
            async with asyncio.timeout(SNAPSHOT_TOTAL_SECONDS):
                api_id = getattr(device, "id", None)
                if isinstance(api_id, bool) or not re.fullmatch(r"[0-9]{1,20}", str(api_id)):
                    scope.reason = "invalid_device"
                    return None
                if fresh:
                    try:
                        image = await device.async_get_snapshot(retries=2, delay=1)
                        if validated_snapshot_bytes(image) is not None:
                            scope.success = True
                            return image
                    except (RingError, SnapshotError, IndexError, KeyError, ValueError):
                        # Malformed/empty SDK timestamp responses can still
                        # use the cache, within the same time/byte budget.
                        pass
                from ring_doorbell.const import SNAPSHOT_ENDPOINT

                if admission is not None and not admission():
                    scope.reason = "authority_retired"
                    return None
                scope.phase = "cached"
                cached_missing = False
                try:
                    response = await self._ring.async_query(SNAPSHOT_ENDPOINT.format(api_id))
                except (RingError, aiohttp.ClientResponseError) as exc:
                    _exception_reason(scope, exc)
                    if (
                        not fresh
                        and scope.phase == "cached"
                        and scope.stage == "image"
                        and scope.http_status == 404
                        and scope.reason == "cached_missing"
                        and self._can_refresh_missing_cache(device)
                    ):
                        cached_missing = True
                    else:
                        raise
                if cached_missing:
                    # Reuse this exact operation's deadline, authority and
                    # image/metadata budgets. Leave the handled404 exception
                    # context before awaiting so a later timeout does not
                    # inherit that old response as its failure cause.
                    return await self._refresh_missing_cache(device, scope, admission)
                if response.status_code == 200:
                    image = validated_snapshot_bytes(response.content)
                    scope.success = image is not None
                    if image is None:
                        scope.reason = "invalid_jpeg"
                    return image
                scope.reason = _http_reason(scope)
        except asyncio.CancelledError as exc:
            _exception_reason(scope, exc)
            raise
        except (
            SnapshotError,
            RingError,
            TimeoutError,
            aiohttp.ClientError,
            ValueError,
            RuntimeError,
        ) as exc:
            _exception_reason(scope, exc)
            return None
        finally:
            _snapshot_scope.reset(scope_token)
            self._tasks.discard(task)
            self._diagnostic(scope, fresh, began)
        return None

    async def close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def validated_snapshot_bytes(value: Any) -> bytes | None:
    """Return one bounded JPEG byte string or ``None`` for invalid input."""
    if not isinstance(value, bytes):
        return None
    image = value
    if (
        not image
        or len(image) > MAX_SNAPSHOT_BYTES
        or not image.startswith(b"\xff\xd8")
        or not image.endswith(b"\xff\xd9")
    ):
        return None
    return image
