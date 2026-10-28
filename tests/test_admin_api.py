import asyncio
import contextlib
import json
import logging
import os
import stat
import struct

import pytest

from halo_server.api import admin
from halo_server.api.admin import ADMIN_PROTOCOL_VERSION, AdminProtocolError, AdminServer
from halo_server.tui.client import AdminClient, AdminClientError


@pytest.mark.asyncio
async def test_admin_client_round_trip_over_private_socket(tmp_path):
    socket_path = tmp_path / "runtime" / "admin.sock"
    requests = []

    async def handler(method, params):
        requests.append((method, params))
        return {"authenticated": False}

    server = AdminServer(socket_path, handler)
    await server.start()
    try:
        result = await AdminClient(socket_path).status()
    finally:
        await server.stop()

    assert result == {"authenticated": False}
    assert requests == [("status", {})]
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_admin_socket_and_runtime_directory_are_owner_only(tmp_path):
    socket_path = tmp_path / "runtime" / "admin.sock"
    server = AdminServer(socket_path, lambda _method, _params: None)

    await server.start()
    try:
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    finally:
        await server.stop()


def test_peer_uid_uses_linux_peer_credentials(monkeypatch):
    monkeypatch.setattr(admin.socket, "SO_PEERCRED", 17, raising=False)

    class FakeSocket:
        def getsockopt(self, level, option, size):
            assert level == admin.socket.SOL_SOCKET
            assert option == admin.socket.SO_PEERCRED
            assert size == struct.calcsize("3i")
            return struct.pack("3i", 123, os.getuid(), 456)

    assert admin._peer_uid(FakeSocket()) == os.getuid()


def test_peer_uid_uses_macos_getpeereid(monkeypatch):
    class FakeSocket:
        def fileno(self):
            return 42

    monkeypatch.delattr(admin.socket, "SO_PEERCRED", raising=False)
    monkeypatch.setattr(admin.sys, "platform", "darwin")
    monkeypatch.setattr(admin, "_darwin_peer_uid", lambda descriptor: descriptor + 1)

    assert admin._peer_uid(FakeSocket()) == 43


def test_peer_uid_rejects_unverified_platform(monkeypatch):
    monkeypatch.delattr(admin.socket, "SO_PEERCRED", raising=False)
    monkeypatch.setattr(admin.sys, "platform", "win32")

    assert admin._peer_uid(object()) is None


def test_admin_peer_check_rejects_unverifiable_identity(monkeypatch):
    class FakeWriter:
        def get_extra_info(self, _name):
            return object()

    monkeypatch.setattr(admin, "_peer_uid", lambda _socket: None)

    with pytest.raises(AdminProtocolError, match="Cannot verify") as raised:
        admin._require_owner_peer(FakeWriter())

    assert raised.value.code == "peer_rejected"


def test_admin_peer_check_rejects_different_owner(monkeypatch):
    class FakeWriter:
        def get_extra_info(self, _name):
            return object()

    monkeypatch.setattr(admin, "_peer_uid", lambda _socket: os.getuid() + 1)

    with pytest.raises(AdminProtocolError, match="not the daemon owner") as raised:
        admin._require_owner_peer(FakeWriter())

    assert raised.value.code == "peer_rejected"


def test_darwin_peer_uid_reads_libc_credentials(monkeypatch):
    class FakeGetPeerEid:
        argtypes = None
        restype = None

        def __call__(self, descriptor, uid, gid):
            assert descriptor == 17
            uid._obj.value = 501
            gid._obj.value = 20
            return 0

    class FakeLibC:
        getpeereid = FakeGetPeerEid()

    monkeypatch.setattr(admin.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibC())

    assert admin._darwin_peer_uid(17) == 501


@pytest.mark.asyncio
async def test_admin_server_rejects_unknown_protocol_version(tmp_path):
    socket_path = tmp_path / "runtime" / "admin.sock"

    async def handler(_method, _params):
        raise AssertionError("invalid request reached handler")

    server = AdminServer(socket_path, handler)
    await server.start()
    try:
        reader, writer = await __import__("asyncio").open_unix_connection(socket_path)
        writer.write(b'{"version":99,"id":1,"method":"status"}\n')
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported_version"


@pytest.mark.asyncio
async def test_admin_server_maps_explicit_protocol_errors(tmp_path):
    socket_path = tmp_path / "runtime" / "admin.sock"

    async def handler(_method, _params):
        raise AdminProtocolError("unknown_method", "No such method")

    server = AdminServer(socket_path, handler)
    await server.start()
    try:
        with pytest.raises(AdminClientError) as raised:
            await AdminClient(socket_path).request("missing")
    finally:
        await server.stop()

    assert raised.value.code == "unknown_method"
    assert str(raised.value) == "No such method"


@pytest.mark.asyncio
async def test_admin_server_does_not_expose_unhandled_exception_text(tmp_path):
    socket_path = tmp_path / "runtime" / "admin.sock"

    async def handler(_method, _params):
        raise RuntimeError("refresh_token=secret")

    server = AdminServer(socket_path, handler)
    await server.start()
    try:
        with pytest.raises(AdminClientError) as raised:
            await AdminClient(socket_path).request("status")
    finally:
        await server.stop()

    assert raised.value.code == "internal_error"
    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_admin_server_logs_unhandled_request_context(tmp_path, caplog):
    socket_path = tmp_path / "runtime" / "admin.sock"

    async def handler(_method, _params):
        raise RuntimeError("injected admin failure")

    caplog.set_level(logging.ERROR, logger="halo_server.api.admin")
    server = AdminServer(socket_path, handler)
    await server.start()
    try:
        with pytest.raises(AdminClientError):
            await AdminClient(socket_path).request("status")
    finally:
        await server.stop()

    assert "Unhandled admin request failure method=status" in caplog.text


@pytest.mark.asyncio
async def test_admin_server_refuses_to_replace_regular_file(tmp_path):
    socket_path = tmp_path / "runtime" / "admin.sock"
    socket_path.parent.mkdir(mode=0o700)
    socket_path.write_text("do not replace", encoding="utf-8")
    server = AdminServer(socket_path, lambda _method, _params: None)

    with pytest.raises(RuntimeError, match="unsafe"):
        await server.start()

    assert socket_path.read_text(encoding="utf-8") == "do not replace"


@pytest.mark.asyncio
async def test_admin_client_rejects_response_with_wrong_request_id(tmp_path):
    socket_path = tmp_path / "wrong-id.sock"

    async def serve(reader, writer):
        await reader.readline()
        writer.write(
            json.dumps(
                {
                    "version": ADMIN_PROTOCOL_VERSION,
                    "id": 999,
                    "ok": True,
                    "result": {},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        writer.close()

    asyncio = __import__("asyncio")
    server = await asyncio.start_unix_server(serve, path=socket_path)
    try:
        with pytest.raises(AdminClientError) as raised:
            await AdminClient(socket_path).status()
    finally:
        server.close()
        await server.wait_closed()

    assert raised.value.code == "bad_response"


@pytest.mark.asyncio
async def test_large_valid_response_uses_separate_response_frame_limit(tmp_path):
    socket_path = tmp_path / "admin.sock"

    async def handler(_method, _params):
        return {"large_status": "x" * 70_000}

    server = AdminServer(socket_path, handler)
    await server.start()
    try:
        result = await AdminClient(socket_path).status()
        assert len(result["large_status"]) == 70_000
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1])
async def test_response_exact_frame_boundary_and_typed_overflow(tmp_path, extra):
    socket_path = tmp_path / "admin.sock"
    envelope = {"version": 1, "id": 1, "ok": True, "result": ""}
    overhead = len(admin._encode_response(envelope))
    result = "x" * (admin.MAX_RESPONSE_BYTES - overhead + extra)

    async def handler(_method, _params):
        return result

    server = AdminServer(socket_path, handler)
    await server.start()
    try:
        if extra:
            with pytest.raises(AdminClientError) as raised:
                await AdminClient(socket_path).request("status")
            assert raised.value.code == "response_too_large"
        else:
            assert await AdminClient(socket_path).request("status") == result
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1])
async def test_request_frame_exact_boundary(tmp_path, extra):
    socket_path = tmp_path / "admin.sock"
    calls = []

    async def handler(_method, params):
        calls.append(len(params["padding"]))
        return {}

    server = AdminServer(socket_path, handler)
    await server.start()
    writer = None
    try:
        request = {"version": 1, "id": 1, "method": "status", "params": {"padding": ""}}
        base = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        request["params"]["padding"] = "x" * (admin.MAX_REQUEST_BYTES - len(base) + extra)
        encoded = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        assert len(encoded) == admin.MAX_REQUEST_BYTES + extra
        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(encoded)
        await writer.drain()
        response = json.loads(await reader.readline())
        assert response["ok"] is (not extra)
        if extra:
            assert response["error"]["code"] == "request_too_large"
            assert calls == []
        else:
            assert len(calls) == 1
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_idle_partial_request_times_out_and_releases_handler(tmp_path):
    async def handler(_method, _params):
        raise AssertionError("incomplete request reached handler")

    server = AdminServer(tmp_path / "admin.sock", handler, read_timeout=0.02)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(server.socket_path)
    try:
        writer.write(b'{"version":1')
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 1))
        assert response["error"]["code"] == "request_timeout"
        assert await asyncio.wait_for(reader.read(), 1) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()
    assert not server._connections


@pytest.mark.asyncio
async def test_connection_cap_rejects_extra_peer_without_another_handler(tmp_path):
    async def handler(_method, _params):
        raise AssertionError("idle request reached handler")

    server = AdminServer(tmp_path / "admin.sock", handler, max_connections=1)
    await server.start()
    first_reader, first = await asyncio.open_unix_connection(server.socket_path)
    second = None
    try:
        await asyncio.sleep(0)
        second_reader, second = await asyncio.open_unix_connection(server.socket_path)
        with contextlib.suppress(ConnectionResetError):
            assert await asyncio.wait_for(second_reader.read(), 1) == b""
        assert len(server._connections) == 1
        await server.stop()
        assert await asyncio.wait_for(first_reader.read(), 1) == b""
        assert not server._connections
    finally:
        first.close()
        await first.wait_closed()
        if second:
            second.close()
            with contextlib.suppress(ConnectionResetError):
                await second.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_stop_joins_cancelled_handler_cleanup_and_concurrent_stop(tmp_path):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(_method, _params):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise

    server = AdminServer(tmp_path / "admin.sock", handler)
    await server.start()
    request = asyncio.create_task(AdminClient(server.socket_path).status())
    await asyncio.wait_for(entered.wait(), 1)
    first = asyncio.create_task(server.stop())
    await asyncio.wait_for(cancelled.wait(), 1)
    second = asyncio.create_task(server.stop())
    assert not first.done()
    assert len(server._connections) == 1
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    await asyncio.gather(request, return_exceptions=True)
    assert not server._connections
    assert not server.socket_path.exists()


@pytest.mark.asyncio
async def test_handler_timeout_is_typed_even_if_handler_suppresses_cancellation(tmp_path):
    async def handler(_method, _params):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"too_late": True}

    server = AdminServer(tmp_path / "admin.sock", handler, handler_timeout=0.01)
    await server.start()
    try:
        with pytest.raises(AdminClientError) as raised:
            await AdminClient(server.socket_path).status()
        assert raised.value.code == "request_timeout"
    finally:
        await server.stop()


class _Writer:
    def __init__(self, *, drain_stalls=False, close_stalls=False):
        self.transport = self
        self.buffer = bytearray()
        self.closed = False
        self.aborted = False
        self.drain_stalls = drain_stalls
        self.close_stalls = close_stalls

    def write(self, data):
        self.buffer.extend(data)

    async def drain(self):
        if self.drain_stalls:
            await asyncio.Event().wait()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        if self.close_stalls:
            await asyncio.Event().wait()

    def abort(self):
        self.aborted = True


@pytest.mark.asyncio
async def test_stalled_server_drain_and_close_have_deadlines(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "_require_owner_peer", lambda _writer: None)

    async def handler(_method, _params):
        return {}

    server = AdminServer(tmp_path / "unused.sock", handler, write_timeout=0.01, close_timeout=0.01)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"version":1,"id":1,"method":"status"}\n')
    writer = _Writer(drain_stalls=True, close_stalls=True)
    await asyncio.wait_for(server._handle_connection(reader, writer), 0.5)
    assert writer.closed and writer.aborted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["over-limit", "no-newline", "boolean-version", "boolean-id", "deeply-nested"]
)
async def test_client_converts_invalid_response_frames_to_typed_errors(monkeypatch, tmp_path, kind):
    async def connect(_path, *, limit):
        assert limit == admin.MAX_RESPONSE_BYTES
        reader = asyncio.StreamReader(limit=limit)
        response = {"version": 1, "id": 1, "ok": True, "result": {}}
        if kind == "over-limit":
            encoded = b"x" * (admin.MAX_RESPONSE_BYTES + 2)
        elif kind == "deeply-nested":
            encoded = b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"
        else:
            if kind == "boolean-version":
                response["version"] = True
            if kind == "boolean-id":
                response["id"] = True
            encoded = json.dumps(response).encode() + (b"" if kind == "no-newline" else b"\n")
        reader.feed_data(encoded)
        reader.feed_eof()
        return reader, _Writer()

    monkeypatch.setattr("halo_server.tui.client.asyncio.open_unix_connection", connect)
    with pytest.raises(AdminClientError) as raised:
        await AdminClient(tmp_path / "unused.sock").status()
    assert raised.value.code == "bad_response"


@pytest.mark.asyncio
async def test_client_close_timeout_aborts_writer(monkeypatch, tmp_path):
    writer = _Writer(close_stalls=True)

    async def connect(_path, *, limit):
        reader = asyncio.StreamReader(limit=limit)
        reader.feed_data(b'{"version":1,"id":1,"ok":true,"result":{}}\n')
        return reader, writer

    monkeypatch.setattr("halo_server.tui.client.asyncio.open_unix_connection", connect)
    monkeypatch.setattr("halo_server.tui.client.CLOSE_TIMEOUT", 0.01)
    assert await asyncio.wait_for(AdminClient(tmp_path / "unused.sock").status(), 0.5) == {}
    assert writer.closed and writer.aborted


@pytest.mark.asyncio
async def test_client_whole_request_budget_does_not_restart_at_each_phase(monkeypatch, tmp_path):
    writer = _Writer()
    timers = []

    async def connect(_path, *, limit):
        await asyncio.sleep(0.03)
        reader = asyncio.StreamReader(limit=limit)
        timers.append(
            asyncio.get_running_loop().call_later(
                0.03, reader.feed_data, b'{"version":1,"id":1,"ok":true,"result":{}}\n'
            )
        )
        return reader, writer

    monkeypatch.setattr("halo_server.tui.client.asyncio.open_unix_connection", connect)
    with pytest.raises(AdminClientError) as raised:
        await asyncio.wait_for(AdminClient(tmp_path / "unused.sock", timeout=0.04).status(), 0.15)
    assert raised.value.code == "request_failed"
    assert writer.closed
    for timer in timers:
        timer.cancel()


@pytest.mark.asyncio
async def test_stop_retains_handler_ownership_until_delayed_cancellation_settles(tmp_path):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(_method, _params):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return {}

    server = AdminServer(tmp_path / "admin.sock", handler, stop_timeout=0.01)
    await server.start()
    request = asyncio.create_task(AdminClient(server.socket_path).status())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RuntimeError, match="still stopping"):
            await server.stop()
        assert cancelled.is_set()
        assert len(server._connections) == 1
        assert not server.socket_path.exists()
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await server.stop()
    assert not server._connections


@pytest.mark.asyncio
async def test_unserializable_handler_result_is_a_typed_error(tmp_path):
    async def handler(_method, _params):
        return {"unexpected": object()}

    server = AdminServer(tmp_path / "admin.sock", handler)
    await server.start()
    try:
        with pytest.raises(AdminClientError) as raised:
            await AdminClient(server.socket_path).status()
        assert raised.value.code == "internal_error"
    finally:
        await server.stop()
