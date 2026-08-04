import asyncio
import logging
from contextlib import asynccontextmanager

import aiohttp
import pytest

from halo_server.api.events import EventJournal
from halo_server.api.plugin import (
    INTERCOM_FRAME_BYTES,
    PROTOCOL_HEADER,
    PluginApiServer,
)
from halo_server.api.tokens import PluginTokenStore


def _status():
    return {
        "service": "running",
        "account": {"authenticated": True},
        "events": {"connection": "connected"},
        "incidents": {"42": {"motion_published": True}},
        "cameras": [
            {
                "id": "42",
                "name": "Front Door",
                "family": "doorbots",
                "kind": "lpd_v4",
                "model": "Doorbell Pro",
                "online": True,
                "battery_percent": 90,
                "motion_detection": True,
                "has_light": False,
                "has_siren": False,
            }
        ],
    }


@asynccontextmanager
async def _server(
    tmp_path,
    *,
    rate_limit=120,
    media_provider=None,
    intercom_provider=None,
    status_provider=_status,
):
    tokens = PluginTokenStore(tmp_path / "integrations")
    issued = tokens.create("Scrypted")
    events = EventJournal(max_events=4)
    server = PluginApiServer(
        host="127.0.0.1",
        port=0,
        token_store=tokens,
        event_journal=events,
        status_provider=status_provider,
        media_provider=media_provider,
        intercom_provider=intercom_provider,
        rate_limit=rate_limit,
    )
    await server.start()
    try:
        yield server, tokens, issued, events
    finally:
        await server.stop()


def _headers(token):
    return {
        PROTOCOL_HEADER: "1",
        "Authorization": f"Bearer {token}",
    }


class FakeIntercomStream:
    def __init__(self):
        self.frames = []
        self.renewals = 0
        self.closed = asyncio.Event()
        self.received = asyncio.Event()

    async def write(self, payload):
        self.frames.append(payload)
        self.received.set()

    async def renew(self):
        self.renewals += 1
        return True

    async def close(self):
        self.closed.set()


@pytest.mark.asyncio
async def test_plugin_api_requires_version_and_authentication(tmp_path):
    async with _server(tmp_path) as (server, _tokens, issued, _events):
        url = f"http://127.0.0.1:{server.bound_port}/v1/health"
        async with aiohttp.ClientSession() as client:
            response = await client.get(url)
            assert response.status == 426
            assert (await response.json())["error"]["code"] == "unsupported_version"

            response = await client.get(url, headers={PROTOCOL_HEADER: "1"})
            assert response.status == 401

            response = await client.get(url, headers=_headers(issued.token))
            assert response.status == 200
            body = await response.json()

        assert body["ring_authenticated"] is True
        assert body["event_connection"] == "connected"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers[PROTOCOL_HEADER] == "1"


@pytest.mark.asyncio
async def test_plugin_devices_use_allowlisted_status_contract(tmp_path):
    async with _server(tmp_path) as (server, _tokens, issued, _events):
        url = f"http://127.0.0.1:{server.bound_port}/v1/devices"
        async with aiohttp.ClientSession() as client:
            response = await client.get(url, headers=_headers(issued.token))
            body = await response.json()

    assert response.status == 200
    assert body["devices"] == _status()["cameras"]
    assert body["states"] == {"42": {"motion_detected": True}}
    assert body["streams"] == {}
    assert "account" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account",
    [
        {"authenticated": False, "restoring": True, "last_error": None},
        {
            "authenticated": False,
            "restoring": False,
            "last_error": "Ring session restore failed; retrying",
        },
    ],
)
async def test_plugin_devices_preserve_inventory_while_ring_is_unavailable(tmp_path, account):
    def unavailable_status():
        return {
            "service": "running",
            "account": account,
            "events": {"connection": "offline"},
            "incidents": {},
            "cameras": [],
        }

    async with _server(tmp_path, status_provider=unavailable_status) as (
        server,
        _tokens,
        issued,
        _events,
    ):
        url = f"http://127.0.0.1:{server.bound_port}/v1/devices"
        async with aiohttp.ClientSession() as client:
            response = await client.get(url, headers=_headers(issued.token))
            body = await response.json()

    assert response.status == 503
    assert body["error"]["code"] == "ring_unavailable"


@pytest.mark.asyncio
async def test_plugin_devices_preserve_inventory_after_explicit_logout(tmp_path):
    def signed_out_status():
        return {
            "service": "running",
            "account": {"authenticated": False, "restoring": False, "last_error": None},
            "events": {"connection": "offline"},
            "incidents": {},
            "cameras": [],
        }

    async with _server(tmp_path, status_provider=signed_out_status) as (
        server,
        _tokens,
        issued,
        _events,
    ):
        url = f"http://127.0.0.1:{server.bound_port}/v1/devices"
        async with aiohttp.ClientSession() as client:
            response = await client.get(url, headers=_headers(issued.token))
            body = await response.json()

    assert response.status == 503
    assert body["error"]["code"] == "ring_authentication_required"


@pytest.mark.asyncio
async def test_plugin_devices_publish_authoritative_empty_authenticated_inventory(tmp_path):
    def empty_account_status():
        return {
            "service": "running",
            "account": {"authenticated": True, "restoring": False, "last_error": None},
            "events": {"connection": "connected"},
            "incidents": {},
            "cameras": [],
        }

    async with _server(tmp_path, status_provider=empty_account_status) as (
        server,
        _tokens,
        issued,
        _events,
    ):
        url = f"http://127.0.0.1:{server.bound_port}/v1/devices"
        async with aiohttp.ClientSession() as client:
            response = await client.get(url, headers=_headers(issued.token))
            body = await response.json()

    assert response.status == 200
    assert body["devices"] == []
    assert body["states"] == {}
    assert body["streams"] == {}


@pytest.mark.asyncio
async def test_plugin_devices_publish_authenticated_on_demand_media_descriptor(tmp_path):
    descriptor = {
        "id": "main",
        "name": "Halo Stream",
        "url": "rtsp://halo:temporary@127.0.0.1:18554/halo/opaque",
        "container": "rtsp",
        "prebuffer": 10_000,
        "video": {"codec": "h264"},
        "audio": {"codec": "opus"},
    }
    async with (
        _server(
            tmp_path,
            media_provider=lambda camera_id: descriptor if camera_id == "42" else None,
        ) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        devices = await (
            await client.get(
                f"http://127.0.0.1:{server.bound_port}/v1/devices",
                headers=_headers(issued.token),
            )
        ).json()
        capabilities = await (
            await client.get(
                f"http://127.0.0.1:{server.bound_port}/v1/capabilities",
                headers=_headers(issued.token),
            )
        ).json()

    assert devices["streams"] == {"42": descriptor}
    assert capabilities["media"] == {
        "available": True,
        "transport": "rtsp",
        "on_demand": True,
        "two_way_audio": False,
        "reason": None,
    }


@pytest.mark.asyncio
async def test_intercom_descriptor_and_socket_accept_only_fixed_pcm_frames(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    descriptor = {
        "id": "main",
        "name": "Halo Stream",
        "url": "rtsp://halo:temporary@127.0.0.1:18554/halo/opaque",
        "container": "rtsp",
        "prebuffer": 10_000,
        "video": {"codec": "h264"},
        "audio": {"codec": "opus"},
    }
    intercom = FakeIntercomStream()

    async def open_intercom(camera_id):
        assert camera_id == "42"
        return intercom

    async with (
        _server(
            tmp_path,
            media_provider=lambda camera_id: descriptor if camera_id == "42" else None,
            intercom_provider=open_intercom,
        ) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        devices = await (
            await client.get(
                f"http://127.0.0.1:{server.bound_port}/v1/devices",
                headers=_headers(issued.token),
            )
        ).json()
        capabilities = await (
            await client.get(
                f"http://127.0.0.1:{server.bound_port}/v1/capabilities",
                headers=_headers(issued.token),
            )
        ).json()
        socket = await client.ws_connect(
            f"http://127.0.0.1:{server.bound_port}/v1/intercom/42",
            headers=_headers(issued.token),
        )
        payload = b"\x01\x00" * (INTERCOM_FRAME_BYTES // 2)
        await socket.send_bytes(payload)
        await asyncio.wait_for(intercom.received.wait(), timeout=1)
        await socket.close()
        await asyncio.wait_for(intercom.closed.wait(), timeout=1)

    assert devices["streams"]["42"]["intercom"] == {
        "codec": "pcm_s16le",
        "sample_rate": 48_000,
        "channels": 1,
        "frame_duration_ms": 20,
    }
    assert "intercom" in capabilities["capabilities"]
    assert capabilities["media"]["two_way_audio"] is True
    assert intercom.frames == [payload]
    assert "Halo intercom WebSocket accepted camera_id=42" in caplog.text
    assert "Halo intercom first PCM frame received camera_id=42 bytes=1920" in caplog.text
    assert (
        "Halo intercom WebSocket closed camera_id=42 reason=client_closed frames=1" in caplog.text
    )


@pytest.mark.asyncio
async def test_intercom_socket_rejects_non_binary_audio_and_releases_stream(tmp_path):
    intercom = FakeIntercomStream()

    async def open_intercom(_camera_id):
        return intercom

    async with (
        _server(tmp_path, intercom_provider=open_intercom) as (
            server,
            _tokens,
            issued,
            _events,
        ),
        aiohttp.ClientSession() as client,
    ):
        socket = await client.ws_connect(
            f"http://127.0.0.1:{server.bound_port}/v1/intercom/42",
            headers=_headers(issued.token),
        )
        await socket.send_str("not audio")
        message = await socket.receive()
        await asyncio.wait_for(intercom.closed.wait(), timeout=1)

    assert message.type is aiohttp.WSMsgType.CLOSE
    assert message.data == 1003
    assert intercom.frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method", "status", "code"),
    [
        ("/v1/missing", "get", 404, "not_found"),
        ("/v1/health", "post", 405, "method_not_allowed"),
    ],
)
async def test_plugin_api_http_errors_keep_versioned_json_contract(
    tmp_path,
    path,
    method,
    status,
    code,
):
    async with _server(tmp_path) as (server, _tokens, issued, _events):
        url = f"http://127.0.0.1:{server.bound_port}{path}"
        async with aiohttp.ClientSession() as client:
            response = await getattr(client, method)(url, headers=_headers(issued.token))
            body = await response.json()

    assert response.status == status
    assert response.headers[PROTOCOL_HEADER] == "1"
    assert response.headers["Cache-Control"] == "no-store"
    assert body["error"]["code"] == code


@pytest.mark.asyncio
async def test_revoked_plugin_token_is_rejected_on_next_request(tmp_path):
    async with _server(tmp_path) as (server, tokens, issued, _events):
        tokens.revoke(issued.identity.id)
        url = f"http://127.0.0.1:{server.bound_port}/v1/health"
        async with aiohttp.ClientSession() as client:
            response = await client.get(url, headers=_headers(issued.token))

    assert response.status == 401


@pytest.mark.asyncio
async def test_plugin_api_rate_limits_each_identity(tmp_path):
    async with _server(tmp_path, rate_limit=2) as (server, _tokens, issued, _events):
        url = f"http://127.0.0.1:{server.bound_port}/v1/health"
        async with aiohttp.ClientSession() as client:
            assert (await client.get(url, headers=_headers(issued.token))).status == 200
            assert (await client.get(url, headers=_headers(issued.token))).status == 200
            response = await client.get(url, headers=_headers(issued.token))

    assert response.status == 429


@pytest.mark.asyncio
async def test_plugin_websocket_replays_and_streams_sequenced_events(tmp_path):
    async with _server(tmp_path) as (server, _tokens, issued, events):
        first = events.publish(
            "motion_on",
            camera_id="42",
            payload={"generation": 1},
            emitted_at=100,
        )
        url = f"http://127.0.0.1:{server.bound_port}/v1/events?epoch={events.epoch}&after=0"
        async with aiohttp.ClientSession() as client:
            socket = await client.ws_connect(url, headers=_headers(issued.token))
            hello = await socket.receive_json()
            replay = await socket.receive_json()
            second = events.publish(
                "motion_off",
                camera_id="42",
                payload={"generation": 1},
                emitted_at=101,
            )
            streamed = await socket.receive_json()
            await socket.close()

    assert hello["type"] == "hello"
    assert replay == {"type": "event", "event": first.as_dict()}
    assert streamed == {"type": "event", "event": second.as_dict()}


@pytest.mark.asyncio
async def test_plugin_websocket_requires_resync_after_journal_gap(tmp_path):
    async with _server(tmp_path) as (server, _tokens, issued, events):
        for number in range(5):
            events.publish("event", camera_id=None, payload={"number": number})
        url = f"http://127.0.0.1:{server.bound_port}/v1/events?epoch={events.epoch}&after=0"
        async with aiohttp.ClientSession() as client:
            response = await client.get(url, headers=_headers(issued.token))
            body = await response.json()

    assert response.status == 409
    assert body["error"]["code"] == "resync_required"


def test_non_loopback_plugin_api_requires_tls(tmp_path):
    with pytest.raises(ValueError, match="TLS"):
        PluginApiServer(
            host="0.0.0.0",
            port=18443,
            token_store=PluginTokenStore(tmp_path / "integrations"),
            event_journal=EventJournal(),
            status_provider=_status,
        )
