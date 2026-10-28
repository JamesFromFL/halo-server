"""Bound Ring snapshot bodies before the pinned SDK buffers them.

Pass SnapshotHttpSession through Auth's public http_client_session argument.
Authentication and token refresh remain owned by the SDK. Only its exact image
and timestamp endpoints are intercepted; other SDK requests are delegated.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from ring_doorbell.exceptions import RingError

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_TIMESTAMP_BYTES = 64 * 1024
SNAPSHOT_TOTAL_SECONDS = 8.0
MAX_SNAPSHOT_REQUESTS = 2
_TLS_CONTEXT = ssl.create_default_context()


@dataclass
class _SnapshotScope:
    image_remaining: int
    metadata_remaining: int
    admission: Callable[[], bool] | None = None


_snapshot_scope: ContextVar[_SnapshotScope | None] = ContextVar("halo_snapshot_scope", default=None)


class SnapshotError(RuntimeError):
    """Safe, content-free snapshot transport failure."""


def _endpoint(url: Any, method: str) -> int | None:
    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except ValueError:
        raise SnapshotError("Invalid snapshot endpoint") from None
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
        raise SnapshotError("Untrusted snapshot endpoint")
    if method == "GET" and re.fullmatch(r"/clients_api/snapshots/image/[0-9]{1,20}", parsed.path):
        return MAX_SNAPSHOT_BYTES
    if method == "POST" and parsed.path == "/clients_api/snapshots/timestamps":
        return MAX_TIMESTAMP_BYTES
    raise SnapshotError("Unsupported snapshot operation")


class _SnapshotResponse:
    """SDK response facade whose read never calls the eager response.read()."""

    def __init__(self, response, limit: int, budget=None, image=False) -> None:
        self._response = response
        self._limit = limit
        self._body: bytes | None = None
        self._budget = budget
        self._image = image
        self._validated = False

    def __getattr__(self, name):
        return getattr(self._response, name)

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
                raise SnapshotError("Snapshot response encoding is unsupported")
            declared = self._response.headers.get("Content-Length")
            if declared is not None and (
                not re.fullmatch(r"[0-9]+", declared) or int(declared) > self._limit
            ):
                raise SnapshotError("Snapshot response exceeds size limit")
        except BaseException:
            self._response.close()
            raise
        self._validated = True

    async def __aexit__(self, *args):
        return await self._response.__aexit__(*args)

    async def read(self) -> bytes:
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
                    raise SnapshotError("Snapshot response exceeds size limit")
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
            raise SnapshotError("Unsupported snapshot SDK contract")
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
            raise SnapshotError("Snapshot authority was retired")
        image = limit == MAX_SNAPSHOT_BYTES
        if limit is None:
            if budget is None:
                return await self._session.request(method, url, **kwargs)
            from ring_doorbell.const import OAuth

            if method.upper() != "POST" or str(url) != OAuth.ENDPOINT:
                raise SnapshotError("Unexpected snapshot request endpoint")
            limit = MAX_TIMESTAMP_BYTES
        if budget is not None:
            limit = min(limit, budget.image_remaining if image else budget.metadata_remaining)
            if limit <= 0:
                raise SnapshotError("Snapshot operation byte budget exhausted")
        if kwargs.get("params"):
            raise SnapshotError("Unexpected snapshot parameters")
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

    async def fetch(
        self,
        device,
        *,
        fresh: bool = False,
        admission: Callable[[], bool] | None = None,
    ) -> bytes | None:
        if admission is not None and not admission():
            return None
        if self._closed or len(self._tasks) >= MAX_SNAPSHOT_REQUESTS:
            return None
        # A missing transport must fail closed, never use the SDK's raw reader.
        if not isinstance(
            getattr(self._ring.auth, "http_client_session", None), SnapshotHttpSession
        ):
            return None
        task = asyncio.current_task()
        self._tasks.add(task)
        scope_token = _snapshot_scope.set(
            _SnapshotScope(MAX_SNAPSHOT_BYTES, 4 * MAX_TIMESTAMP_BYTES, admission)
        )
        try:
            async with asyncio.timeout(SNAPSHOT_TOTAL_SECONDS):
                api_id = getattr(device, "id", None)
                if isinstance(api_id, bool) or not re.fullmatch(r"[0-9]{1,20}", str(api_id)):
                    return None
                if fresh:
                    try:
                        image = await device.async_get_snapshot(retries=2, delay=1)
                        if validated_snapshot_bytes(image) is not None:
                            return image
                    except (RingError, SnapshotError, IndexError, KeyError, ValueError):
                        # Malformed/empty SDK timestamp responses can still
                        # use the cache, within the same time/byte budget.
                        pass
                from ring_doorbell.const import SNAPSHOT_ENDPOINT

                if admission is not None and not admission():
                    return None
                response = await self._ring.async_query(SNAPSHOT_ENDPOINT.format(api_id))
                if response.status_code == 200:
                    return validated_snapshot_bytes(response.content)
        except (
            SnapshotError,
            RingError,
            TimeoutError,
            aiohttp.ClientError,
            ValueError,
            RuntimeError,
        ):
            return None
        finally:
            _snapshot_scope.reset(scope_token)
            self._tasks.discard(task)
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
