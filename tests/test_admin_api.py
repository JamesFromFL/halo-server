import json
import stat

import pytest

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
