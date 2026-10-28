"""No-account tests through real pinned SDK Auth and fresh-snapshot methods."""

import asyncio
import json
import ssl
import time
from collections import deque
from types import MethodType, SimpleNamespace

import aiohttp
import pytest
from ring_doorbell import Auth, Ring
from ring_doorbell.doorbot import RingDoorBell

from halo_server.ring import snapshots
from halo_server.ring.snapshots import SnapshotError, SnapshotHttpSession, SnapshotReader


class Content:
    def __init__(self, body=b"", *, drip=False, wait=None):
        self.body = body
        self.position = 0
        self.reads = []
        self.drip = drip
        self.wait = wait
        self.entered = asyncio.Event()

    async def read(self, size):
        self.entered.set()
        if self.wait is not None:
            await self.wait.wait()
        if self.drip:
            await asyncio.sleep(0.005)
            size = min(size, 1)
        self.reads.append(size)
        result = self.body[self.position : self.position + size]
        self.position += len(result)
        return result


class Response:
    def __init__(self, body=b"", *, status=200, headers=None, content=None):
        self.status = status
        self.headers = headers or {}
        self.content = content or Content(body)
        self.closed = False
        self.eager_reads = 0
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    def close(self):
        self.closed = True

    async def read(self):
        self.eager_reads += 1
        raise AssertionError("unbounded response.read must never be used for snapshot endpoints")

    async def text(self):
        return self._body.decode()

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                SimpleNamespace(real_url="https://api.ring.com/fixture"), (), status=self.status
            )


class Session:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []
        self.closed = False

    async def request(self, method, url, **kwargs):
        self.calls.append((method, str(url), kwargs))
        assert self.responses, "unexpected HTTP request"
        return self.responses.popleft()

    async def close(self):
        self.closed = True


def fixture(responses):
    session = Session(responses)
    transport = SnapshotHttpSession(session=session)
    auth = Auth(
        "synthetic",
        token={
            "access_token": "fixture-access",
            "refresh_token": "fixture-refresh",
            "token_type": "Bearer",
            "expires_at": time.time() + 3600,
        },
        http_client_session=transport,
    )
    ring = Ring(auth)
    ring.session = {"fixture": True}  # Avoid unrelated session-creation API.
    device = SimpleNamespace(id=42, _attrs={"id": 42}, _ring=ring)
    device.async_get_snapshot = MethodType(RingDoorBell.async_get_snapshot, device)
    return SnapshotReader(ring), transport, session, device


@pytest.mark.asyncio
async def test_real_sdk_cached_snapshot_streams_with_tls_and_no_eager_read():
    response = Response(b"\xff\xd8fixture\xff\xd9")
    reader, transport, session, device = fixture([response])
    assert await reader.fetch(device) == b"\xff\xd8fixture\xff\xd9"
    assert response.eager_reads == 0 and response.closed
    method, url, kwargs = session.calls[0]
    assert method == "GET" and url == "https://api.ring.com/clients_api/snapshots/image/42"
    assert kwargs["allow_redirects"] is False and kwargs["auto_decompress"] is False
    assert kwargs["headers"]["Accept-Encoding"] == "identity"
    assert kwargs["headers"]["Authorization"] == "Bearer fixture-access"
    assert isinstance(kwargs["ssl"], ssl.SSLContext)
    assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert kwargs["ssl"].check_hostname
    assert kwargs["timeout"].total == 7
    await reader.close()
    await transport.close()
    assert session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
async def test_chunked_or_misleading_length_stops_at_limit_plus_one(monkeypatch, headers):
    monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_BYTES", 32)
    response = Response(b"x" * 100, headers=headers)
    reader, transport, _session, device = fixture([response])
    assert await reader.fetch(device) is None
    assert response.content.position == 33
    assert response.closed and response.eager_reads == 0
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "999999999"},
        {"Content-Length": "-1"},
        {"Content-Length": "invalid"},
        {"Content-Encoding": "gzip"},
        {"Content-Encoding": "br"},
    ],
)
async def test_oversized_length_and_compression_are_rejected_before_read(headers):
    response = Response(b"not-read", headers=headers)
    reader, transport, _session, device = fixture([response])
    assert await reader.fetch(device) is None
    assert response.content.position == 0 and response.closed
    await transport.close()


@pytest.mark.asyncio
async def test_redirect_is_not_followed_or_returned_as_an_image():
    response = Response(status=302, headers={"Location": "http://example.invalid/image"})
    reader, transport, session, device = fixture([response])
    assert await reader.fetch(device) is None
    assert len(session.calls) == 1 and session.calls[0][2]["allow_redirects"] is False
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://api.ring.com/clients_api/snapshots/image/42",
        "https://evil.invalid/clients_api/snapshots/image/42",
        "https://@api.ring.com/clients_api/snapshots/image/42",
        "https://api.ring.com:444/clients_api/snapshots/image/42",
        "https://api.ring.com:bad/clients_api/snapshots/image/42",
        "https://api.ring.com/clients_api/snapshots/image/42?token=fixture",
        "https://api.ring.com/clients_api/snapshots/image/42#fragment",
        "https://api.ring.com/clients_api/snapshots/image/../42",
    ],
)
async def test_authenticated_snapshot_authority_is_narrow(url):
    session = Session([])
    transport = SnapshotHttpSession(session=session)
    with pytest.raises(SnapshotError):
        await transport.request("GET", url)
    assert not session.calls
    await transport.close()


@pytest.mark.asyncio
async def test_real_sdk_fresh_trigger_poll_and_image_all_use_bounded_transport(monkeypatch):
    original_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await original_sleep(0)

    monkeypatch.setattr("ring_doorbell.doorbot.asyncio.sleep", fast_sleep)
    timestamp = json.dumps({"timestamps": [{"timestamp": (time.time() + 60) * 1000}]}).encode()
    replies = [Response(b"{}"), Response(timestamp), Response(b"\xff\xd8fresh\xff\xd9")]
    reader, transport, session, device = fixture(replies)
    assert await reader.fetch(device, fresh=True) == b"\xff\xd8fresh\xff\xd9"
    assert [call[0] for call in session.calls] == ["POST", "POST", "GET"]
    assert all(reply.eager_reads == 0 and reply.closed for reply in replies)
    await transport.close()


@pytest.mark.asyncio
async def test_fresh_timestamp_body_is_bounded_too(monkeypatch):
    monkeypatch.setattr(snapshots, "MAX_TIMESTAMP_BYTES", 16)
    response = Response(b"x" * 100)
    reader, transport, _session, device = fixture([response])
    assert await reader.fetch(device, fresh=True) is None
    assert response.content.position == 17 and response.closed
    await transport.close()


@pytest.mark.asyncio
async def test_whole_operation_deadline_stops_slow_drip(monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_TOTAL_SECONDS", 0.02)
    response = Response(content=Content(b"x" * 100, drip=True))
    reader, transport, _session, device = fixture([response])
    assert await reader.fetch(device) is None
    assert response.content.position < 100 and response.closed
    assert not reader._tasks
    await transport.close()


@pytest.mark.asyncio
async def test_no_wait_admission_and_close_cancel_active_reads():
    replies = [Response(content=Content(wait=asyncio.Event())) for _ in range(2)]
    reader, transport, session, device = fixture(replies)
    tasks = [asyncio.create_task(reader.fetch(device)) for _ in range(2)]
    await asyncio.gather(*(reply.content.entered.wait() for reply in replies))
    assert await reader.fetch(device) is None
    assert len(session.calls) == 2
    await reader.close()
    assert all(task.cancelled() for task in tasks)
    assert not reader._tasks and all(reply.closed for reply in replies)
    assert await reader.fetch(device) is None
    await transport.close()


@pytest.mark.asyncio
async def test_caller_cancel_releases_slot_and_response():
    reply = Response(content=Content(wait=asyncio.Event()))
    reader, transport, session, device = fixture([reply, Response(b"\xff\xd8next\xff\xd9")])
    task = asyncio.create_task(reader.fetch(device))
    await reply.content.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reply.closed and not reader._tasks
    assert await reader.fetch(device) == b"\xff\xd8next\xff\xd9"
    assert len(session.calls) == 2
    await transport.close()


@pytest.mark.asyncio
async def test_missing_injected_transport_fails_closed():
    reader = SnapshotReader(SimpleNamespace(auth=SimpleNamespace(http_client_session=None)))
    assert await reader.fetch(SimpleNamespace(id=42)) is None
    await reader.close()


@pytest.mark.asyncio
async def test_expired_token_uses_real_sdk_refresh_and_latest_token_persistence():
    reader, transport, session, device = fixture([])
    auth = reader._ring.auth
    auth._oauth_client._expires_at = time.time() - 1
    latest = {
        "access_token": "new-fixture",
        "refresh_token": "new-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    session.responses.extend(
        [Response(json.dumps(latest).encode()), Response(b"\xff\xd8ok\xff\xd9")]
    )
    updates = []
    auth.token_updater = updates.append
    assert await reader.fetch(device) == b"\xff\xd8ok\xff\xd9"
    assert session.calls[0][0] == "POST" and "oauth" in session.calls[0][1]
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer new-fixture"
    assert updates[-1]["refresh_token"] == "new-refresh"
    await transport.close()


@pytest.mark.asyncio
async def test_401_sdk_refresh_keeps_bounded_response_closed_and_persists_token():
    latest = {
        "access_token": "new-fixture",
        "refresh_token": "new-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    unauthorized = Response(status=401)
    reader, transport, session, device = fixture(
        [unauthorized, Response(json.dumps(latest).encode())]
    )
    updates = []
    reader._ring.auth.token_updater = updates.append
    assert await reader.fetch(device) is None  # Pinned SDK does not replay the 401 GET.
    assert unauthorized.closed and unauthorized.eager_reads == 0
    assert updates[-1]["refresh_token"] == "new-refresh"
    assert len(session.calls) == 2
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["valid", "chunked-overflow", "gzip", "redirect", "bad-tls", "slow-drip"]
)
async def test_native_https_snapshot_boundary(tmp_path, monkeypatch, mode):
    """Real TLS/socket fixture with resolver pinned to this loopback server."""
    import datetime
    import socket

    from aiohttp import web
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.ring.com")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("api.ring.com")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "fixture-cert.pem", tmp_path / "fixture-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert_path, key_path)
    client_tls = (
        ssl.create_default_context()
        if mode == "bad-tls"
        else ssl.create_default_context(cafile=str(cert_path))
    )
    monkeypatch.setattr(snapshots, "_TLS_CONTEXT", client_tls)
    monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_BYTES", 32768)
    calls = []

    async def handler(request):
        calls.append((request.path, request.headers.get("Authorization")))
        assert request.headers.get("Accept-Encoding") == "identity"
        if mode == "redirect":
            return web.Response(
                status=302, headers={"Location": "http://example.invalid/forbidden"}
            )
        if mode == "gzip":
            return web.Response(body=b"compressed-fixture", headers={"Content-Encoding": "gzip"})
        if mode in {"chunked-overflow", "slow-drip"}:
            response = web.StreamResponse()
            await response.prepare(request)
            try:
                for _ in range(64 if mode == "chunked-overflow" else 20):
                    await response.write(b"x" * (1024 if mode == "chunked-overflow" else 1))
                    if mode == "slow-drip":
                        await asyncio.sleep(0.01)
                await response.write_eof()
            except (ConnectionResetError, aiohttp.ClientConnectionError):
                pass
            return response
        return web.Response(body=b"\xff\xd8native\xff\xd9", content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/clients_api/snapshots/image/42", handler)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    class Resolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            assert host == "api.ring.com"
            return [
                {
                    "hostname": host,
                    "host": "127.0.0.1",
                    "port": fixture_port,
                    "family": socket.AF_INET,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            ]

        async def close(self):
            pass

    fixture_port = port
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=Resolver(), use_dns_cache=False),
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
    )
    transport = SnapshotHttpSession(session=session)
    auth = Auth(
        "fixture",
        token={
            "access_token": "fixture-access",
            "refresh_token": "fixture-refresh",
            "token_type": "Bearer",
            "expires_at": time.time() + 3600,
        },
        http_client_session=transport,
    )
    ring = Ring(auth)
    ring.session = {"fixture": True}
    reader = SnapshotReader(ring)
    if mode == "slow-drip":
        monkeypatch.setattr(snapshots, "SNAPSHOT_TOTAL_SECONDS", 0.04)
    try:
        result = await reader.fetch(SimpleNamespace(id=42))
        assert result == (b"\xff\xd8native\xff\xd9" if mode == "valid" else None)
        assert calls == (
            []
            if mode == "bad-tls"
            else [("/clients_api/snapshots/image/42", "Bearer fixture-access")]
        )
        assert not reader._tasks
    finally:
        await reader.close()
        await transport.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_malformed_fresh_sdk_timestamps_can_use_bounded_cached_fallback(monkeypatch):
    original_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await original_sleep(0)

    monkeypatch.setattr("ring_doorbell.doorbot.asyncio.sleep", fast_sleep)
    responses = [
        Response(b"{}"),
        Response(b'{"timestamps":[]}'),
        Response(b"\xff\xd8cached\xff\xd9"),
    ]
    reader, transport, session, device = fixture(responses)
    assert await reader.fetch(device, fresh=True) == b"\xff\xd8cached\xff\xd9"
    assert len(session.calls) == 3
    assert all(response.closed and response.eager_reads == 0 for response in responses)
    await transport.close()


@pytest.mark.asyncio
async def test_fresh_and_cached_fallback_share_one_image_byte_budget(monkeypatch):
    monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_BYTES", 16)
    first, second = Response(b"not-a-jpeg!!!"), Response(b"\xff\xd8cached\xff\xd9")
    reader, transport, _session, device = fixture([first, second])

    async def fresh(**_kwargs):
        response = await reader._ring.async_query("/clients_api/snapshots/image/42")
        return response.content

    device.async_get_snapshot = fresh
    assert await reader.fetch(device, fresh=True) is None
    assert first.content.position + second.content.position == 17
    assert first.closed and second.closed
    await transport.close()


@pytest.mark.asyncio
async def test_invalid_device_id_is_rejected_before_fresh_trigger():
    reader, transport, session, device = fixture([])
    device.id = "../unbounded"
    assert await reader.fetch(device, fresh=True) is None
    assert not session.calls
    await transport.close()


@pytest.mark.asyncio
async def test_real_fcm_checkin_uses_provided_session_post_context():
    from firebase_messaging.fcmregister import FcmRegister, FcmRegisterConfig
    from firebase_messaging.proto.checkin_pb2 import AndroidCheckinResponse

    payload = AndroidCheckinResponse(
        stats_ok=True, android_id=123, security_token=456
    ).SerializeToString()
    response = Response(payload)

    async def read_ordinary_registration():
        return payload

    response.read = read_ordinary_registration
    session = Session([response])
    transport = SnapshotHttpSession(session=session)
    register = FcmRegister(
        FcmRegisterConfig(
            project_id="fixture", app_id="fixture", api_key="fixture", messaging_sender_id="fixture"
        ),
        http_client_session=transport,
    )
    result = await register.gcm_check_in()
    assert result["androidId"] == "123" and result["securityToken"] == "456"
    assert session.calls[0][0] == "POST"
    assert session.calls[0][1] == "https://android.clients.google.com/checkin"
    assert response.closed
    await register.close()
    assert not session.closed  # Provided transport remains RingClient-owned.
    await transport.close()
    assert session.closed


@pytest.mark.asyncio
async def test_public_request_and_post_contexts_keep_snapshot_caps(monkeypatch):
    monkeypatch.setattr(snapshots, "MAX_TIMESTAMP_BYTES", 16)
    responses = [Response(b"x" * 100), Response(b"x" * 100)]
    session = Session(responses)
    transport = SnapshotHttpSession(session=session)
    url = "https://api.ring.com/clients_api/snapshots/timestamps"
    for operation in (transport.request("POST", url), transport.post(url)):
        with pytest.raises(SnapshotError):
            async with operation as response:
                await response.read()
    assert all(response.content.position == 17 and response.closed for response in responses)
    await transport.close()
