import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager

import aiohttp
import pytest

from halo_server.api import plugin as plugin_api
from halo_server.api.events import EventJournal
from halo_server.api.plugin import (
    INTERCOM_FRAME_BYTES,
    PLUGIN_PROTOCOL_VERSION,
    PROTOCOL_HEADER,
    PluginApiServer,
    PluginCameraOperationError,
)
from halo_server.api.tokens import PluginTokenStore
from halo_server.ring.snapshots import MAX_SNAPSHOT_BYTES


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
                "light_on": None,
                "has_siren": False,
                "siren_on": None,
                "has_intercom": True,
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
    control_provider=None,
    snapshot_provider=None,
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
        control_provider=control_provider,
        snapshot_provider=snapshot_provider,
        rate_limit=rate_limit,
    )
    await server.start()
    try:
        yield server, tokens, issued, events
    finally:
        await server.stop()


def _headers(token):
    return {
        PROTOCOL_HEADER: str(PLUGIN_PROTOCOL_VERSION),
        "Authorization": f"Bearer {token}",
    }


class FakeIntercomStream:
    def __init__(self, *, ready=True, close_error=None):
        self.frames = []
        self.renewals = 0
        self.ready = ready
        self.close_error = close_error
        self.ready_timeouts = []
        self.closed = asyncio.Event()
        self.received = asyncio.Event()

    async def write(self, payload):
        self.frames.append(payload)
        self.received.set()

    async def wait_ready(self, timeout):
        self.ready_timeouts.append(timeout)
        return self.ready

    async def renew(self):
        self.renewals += 1
        return True

    async def close(self):
        self.closed.set()
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.asyncio
async def test_plugin_api_requires_version_and_authentication(tmp_path):
    async with _server(tmp_path) as (server, _tokens, issued, _events):
        url = f"http://127.0.0.1:{server.bound_port}/v1/health"
        async with aiohttp.ClientSession() as client:
            response = await client.get(url)
            assert response.status == 426
            assert (await response.json())["error"]["code"] == "unsupported_version"

            old_headers = _headers(issued.token)
            old_headers[PROTOCOL_HEADER] = "1"
            response = await client.get(url, headers=old_headers)
            assert response.status == 426
            assert (await response.json())["error"]["code"] == "unsupported_version"
            assert response.headers[PROTOCOL_HEADER] == str(PLUGIN_PROTOCOL_VERSION)

            response = await client.get(
                url,
                headers={PROTOCOL_HEADER: str(PLUGIN_PROTOCOL_VERSION)},
            )
            assert response.status == 401

            response = await client.get(url, headers=_headers(issued.token))
            assert response.status == 200
            body = await response.json()

        assert body["ring_authenticated"] is True
        assert body["event_connection"] == "connected"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers[PROTOCOL_HEADER] == str(PLUGIN_PROTOCOL_VERSION)


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
async def test_plugin_devices_preserve_inventory_until_account_integrations_are_ready(tmp_path):
    def integrations_starting_status():
        status = _status()
        status["account"] = {
            "authenticated": True,
            "integrations_ready": False,
            "restoring": True,
            "last_error": "Ring account integrations are unavailable; retrying",
        }
        return status

    async with _server(tmp_path, status_provider=integrations_starting_status) as (
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
        "maximum_session_seconds": {
            "event": 600.0,
            "homekit_live": 600.0,
            "scrypted_live": 600.0,
            "snapshot": 30.0,
            "intercom": 300.0,
        },
        "reason": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("with_snapshot", "with_controls", "optional_capabilities"),
    [
        (False, False, []),
        (True, False, ["snapshot"]),
        (False, True, ["light", "siren"]),
        (True, True, ["snapshot", "light", "siren"]),
    ],
)
async def test_plugin_capabilities_advertise_only_configured_providers(
    tmp_path,
    with_snapshot,
    with_controls,
    optional_capabilities,
):
    async def snapshot(_camera_id, _fresh):
        return b"unused"

    async def control(_camera_id, _name, _on):
        return None

    async with (
        _server(
            tmp_path,
            snapshot_provider=snapshot if with_snapshot else None,
            control_provider=control if with_controls else None,
        ) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        response = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/capabilities",
            headers=_headers(issued.token),
        )
        capabilities = await response.json()

    assert response.status == 200
    assert capabilities["protocol_version"] == PLUGIN_PROTOCOL_VERSION
    assert capabilities["capabilities"] == [
        "devices",
        "events",
        "motion",
        "doorbell",
        "battery",
        "online",
        *optional_capabilities,
    ]


@pytest.mark.asyncio
async def test_camera_light_and_siren_controls_accept_only_capable_devices(tmp_path):
    status = _status()
    status["cameras"][0]["has_light"] = True
    status["cameras"][0]["has_siren"] = True
    calls = []

    async def control(camera_id, name, on):
        calls.append((camera_id, name, on))

    async with (
        _server(
            tmp_path,
            status_provider=lambda: status,
            control_provider=control,
        ) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        light = await client.put(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/light",
            headers=_headers(issued.token),
            json={"on": True},
        )
        siren = await client.put(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/siren",
            headers=_headers(issued.token),
            json={"on": False},
        )

        assert light.status == 204
        assert await light.read() == b""
        assert light.headers["Cache-Control"] == "no-store"
        assert light.headers[PROTOCOL_HEADER] == str(PLUGIN_PROTOCOL_VERSION)
        assert siren.status == 204

    assert calls == [("42", "light", True), ("42", "siren", False)]


@pytest.mark.asyncio
async def test_camera_control_rejects_non_exact_json_bodies_without_provider_call(tmp_path):
    status = _status()
    status["cameras"][0]["has_light"] = True
    calls = []

    async def control(*args):
        calls.append(args)

    async with (
        _server(
            tmp_path,
            status_provider=lambda: status,
            control_provider=control,
        ) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        url = f"http://127.0.0.1:{server.bound_port}/v1/devices/42/light"
        responses = [
            await client.put(url, headers=_headers(issued.token), json={}),
            await client.put(url, headers=_headers(issued.token), json={"on": 1}),
            await client.put(
                url,
                headers=_headers(issued.token),
                json={"on": True, "extra": False},
            ),
            await client.put(url, headers=_headers(issued.token), json=[True]),
            await client.put(
                url,
                headers={**_headers(issued.token), "Content-Type": "application/json"},
                data=b"{",
            ),
            await client.put(
                url,
                headers={**_headers(issued.token), "Content-Type": "application/json"},
                data=b'{"on":true,"on":false}',
            ),
            await client.put(
                url,
                headers={**_headers(issued.token), "Content-Type": "text/plain"},
                data=b'{"on":true}',
            ),
        ]
        bodies = [await response.json() for response in responses]

    assert [response.status for response in responses] == [400] * len(responses)
    assert {body["error"]["code"] for body in bodies} == {"bad_request"}
    assert calls == []


@pytest.mark.asyncio
async def test_camera_control_rejects_unknown_and_unsupported_devices(tmp_path):
    calls = []

    async def control(*args):
        calls.append(args)

    async with (
        _server(tmp_path, control_provider=control) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        unsupported = await client.put(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/light",
            headers=_headers(issued.token),
            json={"on": True},
        )
        missing = await client.put(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/missing/siren",
            headers=_headers(issued.token),
            json={"on": True},
        )
        unsupported_body = await unsupported.json()
        missing_body = await missing.json()

    assert unsupported.status == 404
    assert unsupported_body["error"]["code"] == "light_unsupported"
    assert missing.status == 404
    assert missing_body["error"]["code"] == "camera_unavailable"
    assert calls == []


@pytest.mark.asyncio
async def test_camera_control_authentication_precedes_body_and_provider(tmp_path):
    calls = []

    async def control(*args):
        calls.append(args)

    async with (
        _server(tmp_path, control_provider=control) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        url = f"http://127.0.0.1:{server.bound_port}/v1/devices/42/light"
        unauthorized = await client.put(
            url,
            headers={PROTOCOL_HEADER: str(PLUGIN_PROTOCOL_VERSION)},
            data=b"not-json",
        )
        old_version = await client.put(
            url,
            headers={**_headers(issued.token), PROTOCOL_HEADER: "1"},
            json={"on": True},
        )

    assert unauthorized.status == 401
    assert old_version.status == 426
    assert calls == []


@pytest.mark.asyncio
async def test_camera_control_returns_stable_provider_error(tmp_path):
    status = _status()
    status["cameras"][0]["has_siren"] = True

    async def fail_control(*_args):
        raise PluginCameraOperationError(
            "control_failed",
            "Ring camera control could not be completed",
            status=502,
        )

    async with (
        _server(
            tmp_path,
            status_provider=lambda: status,
            control_provider=fail_control,
        ) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        response = await client.put(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/siren",
            headers=_headers(issued.token),
            json={"on": True},
        )
        body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "control_failed"


@pytest.mark.asyncio
async def test_camera_control_timeout_cancels_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api, "CAMERA_CONTROL_TIMEOUT_SECONDS", 0.01)
    status = _status()
    status["cameras"][0]["has_light"] = True
    cancelled = asyncio.Event()

    async def control(*_args):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    async with (
        _server(
            tmp_path,
            status_provider=lambda: status,
            control_provider=control,
        ) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        response = await client.put(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/light",
            headers=_headers(issued.token),
            json={"on": True},
        )
        body = await response.json()

    assert response.status == 504
    assert body["error"]["code"] == "control_timeout"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_camera_operations_are_unavailable_without_injected_providers(tmp_path):
    async with (
        _server(tmp_path) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        light = await client.put(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/light",
            headers=_headers(issued.token),
            json={"on": True},
        )
        snapshot = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot",
            headers=_headers(issued.token),
        )
        light_body = await light.json()
        snapshot_body = await snapshot.json()

    assert light.status == snapshot.status == 404
    assert light_body["error"]["code"] == "control_unavailable"
    assert snapshot_body["error"]["code"] == "snapshot_unavailable"


@pytest.mark.asyncio
async def test_snapshot_defaults_to_cached_and_fresh_is_explicit(tmp_path):
    image = b"\xff\xd8halo-jpeg\xff\xd9"
    calls = []
    status = _status()
    status["cameras"][0]["has_intercom"] = False

    async def snapshot(camera_id, fresh):
        calls.append((camera_id, fresh))
        return image

    async with (
        _server(
            tmp_path,
            snapshot_provider=snapshot,
            status_provider=lambda: status,
        ) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        cached = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot",
            headers=_headers(issued.token),
        )
        cached_body = await cached.read()
        fresh = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot?fresh=1",
            headers=_headers(issued.token),
        )
        fresh_body = await fresh.read()

    assert cached.status == fresh.status == 200
    assert cached_body == fresh_body == image
    assert cached.headers["Content-Type"] == "image/jpeg"
    assert cached.headers["Cache-Control"] == "no-store"
    assert cached.headers[PROTOCOL_HEADER] == str(PLUGIN_PROTOCOL_VERSION)
    assert calls == [("42", False), ("42", True)]


@pytest.mark.asyncio
async def test_snapshot_security_and_methods_cannot_wake_camera(tmp_path):
    calls = []

    async def snapshot(*args):
        calls.append(args)
        return b"\xff\xd8jpeg\xff\xd9"

    async with (
        _server(tmp_path, snapshot_provider=snapshot) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        response = await client.head(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot?fresh=1",
            headers=_headers(issued.token),
        )
        unauthorized = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot?fresh=1",
            headers={PROTOCOL_HEADER: str(PLUGIN_PROTOCOL_VERSION)},
        )
        old_version = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot?fresh=1",
            headers={**_headers(issued.token), PROTOCOL_HEADER: "1"},
        )
        wrong_method = await client.post(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot?fresh=1",
            headers=_headers(issued.token),
        )

    assert response.status == 405
    assert unauthorized.status == 401
    assert old_version.status == 426
    assert wrong_method.status == 405
    assert calls == []


@pytest.mark.asyncio
async def test_snapshot_rejects_invalid_query_unknown_camera_and_signed_out_state(tmp_path):
    calls = []

    async def snapshot(*args):
        calls.append(args)
        return b"\xff\xd8jpeg\xff\xd9"

    signed_out = _status()
    signed_out["account"]["authenticated"] = False
    async with (
        _server(tmp_path, snapshot_provider=snapshot) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        base = f"http://127.0.0.1:{server.bound_port}/v1/devices"
        invalid = await client.get(
            f"{base}/42/snapshot?fresh=true",
            headers=_headers(issued.token),
        )
        repeated = await client.get(
            f"{base}/42/snapshot?fresh=0&fresh=1",
            headers=_headers(issued.token),
        )
        extra = await client.get(
            f"{base}/42/snapshot?extra=1",
            headers=_headers(issued.token),
        )
        missing = await client.get(
            f"{base}/missing/snapshot",
            headers=_headers(issued.token),
        )
    async with (
        _server(
            tmp_path / "signed-out",
            snapshot_provider=snapshot,
            status_provider=lambda: signed_out,
        ) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        unavailable = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot",
            headers=_headers(issued.token),
        )

    assert [invalid.status, repeated.status, extra.status] == [400, 400, 400]
    assert missing.status == 404
    assert unavailable.status == 503
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image",
    [
        None,
        bytearray(b"\xff\xd8jpeg\xff\xd9"),
        b"",
        b"not-a-jpeg",
        b"\xff\xd8missing-end",
        b"\xff\xd8" + b"x" * MAX_SNAPSHOT_BYTES + b"\xff\xd9",
    ],
)
async def test_snapshot_rejects_empty_malformed_and_oversize_images(tmp_path, image):
    async def snapshot(_camera_id, _fresh):
        return image

    async with (
        _server(tmp_path, snapshot_provider=snapshot) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        response = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot",
            headers=_headers(issued.token),
        )
        body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "snapshot_invalid"


@pytest.mark.asyncio
async def test_snapshot_timeout_cancels_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api, "SNAPSHOT_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def snapshot(_camera_id, _fresh):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    async with (
        _server(tmp_path, snapshot_provider=snapshot) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        response = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot",
            headers=_headers(issued.token),
        )
        body = await response.json()

    assert response.status == 504
    assert body["error"]["code"] == "snapshot_timeout"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_snapshot_returns_stable_provider_error(tmp_path):
    async def snapshot(_camera_id, _fresh):
        raise PluginCameraOperationError(
            "snapshot_failed",
            "Ring snapshot could not be completed",
            status=502,
        )

    async with (
        _server(tmp_path, snapshot_provider=snapshot) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        response = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot",
            headers=_headers(issued.token),
        )
        body = await response.json()

    assert response.status == 502
    assert body["error"]["code"] == "snapshot_failed"


@pytest.mark.asyncio
async def test_snapshot_contains_unexpected_provider_error(tmp_path, caplog):
    caplog.set_level(logging.ERROR, logger="halo_server.api.plugin")

    async def snapshot(_camera_id, _fresh):
        raise RuntimeError("private upstream snapshot detail")

    async with (
        _server(tmp_path, snapshot_provider=snapshot) as (server, _tokens, issued, _events),
        aiohttp.ClientSession() as client,
    ):
        response = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/devices/42/snapshot",
            headers=_headers(issued.token),
        )
        body = await response.json()

    assert response.status == 500
    assert body["error"]["code"] == "internal_error"
    assert "private upstream snapshot detail" not in str(body)
    assert "private upstream snapshot detail" in caplog.text


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
        ready = await socket.receive_json()
        payload = b"\x01\x00" * (INTERCOM_FRAME_BYTES // 2)
        await socket.send_bytes(payload)
        await asyncio.wait_for(intercom.received.wait(), timeout=1)
        active = await socket.receive_json()
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
    assert ready == {"type": "ready", "protocol_version": PLUGIN_PROTOCOL_VERSION}
    assert active == {"type": "active"}
    assert intercom.ready_timeouts == [15.0]
    assert "Halo intercom WebSocket accepted camera_id=42" in caplog.text
    assert "Halo intercom first PCM frame received camera_id=42 bytes=1920" in caplog.text
    assert (
        "Halo intercom WebSocket closed camera_id=42 reason=client_closed frames=1" in caplog.text
    )


@pytest.mark.asyncio
async def test_intercom_socket_refuses_compression_and_accepts_incompressible_pcm(tmp_path):
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
            compress=15,
        )
        assert socket.compress == 0
        await socket.receive_json()
        payload = hashlib.shake_256(b"halo-intercom-pcm").digest(INTERCOM_FRAME_BYTES)
        await socket.send_bytes(payload)
        await asyncio.wait_for(intercom.received.wait(), timeout=1)
        assert (await socket.receive_json())["type"] == "active"
        await socket.close()
        await asyncio.wait_for(intercom.closed.wait(), timeout=1)

    assert intercom.frames == [payload]


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
        assert (await socket.receive_json())["type"] == "ready"
        await socket.send_str("not audio")
        message = await socket.receive()
        await asyncio.wait_for(intercom.closed.wait(), timeout=1)

    assert message.type is aiohttp.WSMsgType.CLOSE
    assert message.data == 1003
    assert intercom.frames == []


@pytest.mark.asyncio
async def test_intercom_socket_closes_when_camera_speaker_does_not_activate(tmp_path):
    intercom = FakeIntercomStream(ready=False)

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
        await socket.receive_json()
        await socket.send_bytes(b"\x00" * INTERCOM_FRAME_BYTES)
        message = await socket.receive()
        await asyncio.wait_for(intercom.closed.wait(), timeout=1)

    assert message.type is aiohttp.WSMsgType.CLOSE
    assert message.data == 1011
    assert intercom.frames == [b"\x00" * INTERCOM_FRAME_BYTES]


@pytest.mark.asyncio
async def test_intercom_socket_logs_stream_cleanup_failure(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    intercom = FakeIntercomStream(close_error=RuntimeError("release failed"))

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
        await socket.receive_json()
        await socket.close()
        await asyncio.wait_for(intercom.closed.wait(), timeout=1)

    assert "Halo intercom stream cleanup failed camera_id=42" in caplog.text
    assert "release failed" in caplog.text


@pytest.mark.asyncio
async def test_intercom_ping_traffic_does_not_extend_audio_idle_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api, "INTERCOM_IDLE_TIMEOUT", 0.05)
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
        await socket.receive_json()

        async def send_pings():
            while True:
                await socket.ping()
                await asyncio.sleep(0.01)

        pings = asyncio.create_task(send_pings())
        try:
            message = await socket.receive(timeout=1)
        finally:
            pings.cancel()
            await asyncio.gather(pings, return_exceptions=True)
        await asyncio.wait_for(intercom.closed.wait(), timeout=1)

    assert message.type is aiohttp.WSMsgType.CLOSE
    assert message.data == 1008
    assert intercom.frames == []


@pytest.mark.asyncio
async def test_intercom_is_not_published_or_opened_for_unsupported_camera(tmp_path):
    status = _status()
    status["cameras"][0]["has_intercom"] = False
    provider_calls = 0

    async def open_intercom(_camera_id):
        nonlocal provider_calls
        provider_calls += 1
        return FakeIntercomStream()

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
            status_provider=lambda: status,
            media_provider=lambda _camera_id: descriptor,
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
        response = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/intercom/42",
            headers=_headers(issued.token),
        )
        error = await response.json()

    assert "intercom" not in devices["streams"]["42"]
    assert response.status == 404
    assert error["error"]["code"] == "intercom_unsupported"
    assert provider_calls == 0


@pytest.mark.asyncio
async def test_intercom_unknown_camera_is_reported_as_unavailable(tmp_path):
    provider_calls = 0

    async def open_intercom(_camera_id):
        nonlocal provider_calls
        provider_calls += 1
        return FakeIntercomStream()

    async with (
        _server(tmp_path, intercom_provider=open_intercom) as (
            server,
            _tokens,
            issued,
            _events,
        ),
        aiohttp.ClientSession() as client,
    ):
        response = await client.get(
            f"http://127.0.0.1:{server.bound_port}/v1/intercom/missing",
            headers=_headers(issued.token),
        )
        error = await response.json()

    assert response.status == 404
    assert error["error"]["code"] == "camera_unavailable"
    assert provider_calls == 0


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
    assert response.headers[PROTOCOL_HEADER] == str(PLUGIN_PROTOCOL_VERSION)
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
async def test_plugin_api_logs_unhandled_request_context_without_exposing_error(tmp_path, caplog):
    def fail_status():
        raise RuntimeError("injected provider detail")

    caplog.set_level(logging.ERROR, logger="halo_server.api.plugin")
    async with _server(tmp_path, status_provider=fail_status) as (
        server,
        _tokens,
        issued,
        _events,
    ):
        url = f"http://127.0.0.1:{server.bound_port}/v1/health"
        async with aiohttp.ClientSession() as client:
            response = await client.get(url, headers=_headers(issued.token))
            body = await response.json()

    assert response.status == 500
    assert body["error"] == {
        "code": "internal_error",
        "message": "Halo Server could not complete the plugin request",
    }
    assert "injected provider detail" not in str(body)
    assert "Unhandled plugin API request method=GET path=/v1/health" in caplog.text


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
async def test_plugin_event_websocket_rejects_binary_client_messages(tmp_path):
    async with _server(tmp_path) as (server, _tokens, issued, events):
        url = f"http://127.0.0.1:{server.bound_port}/v1/events"
        async with aiohttp.ClientSession() as client:
            socket = await client.ws_connect(url, headers=_headers(issued.token))
            await socket.receive_json()
            await socket.send_bytes(b"unsupported")
            message = await socket.receive()

    assert message.type is aiohttp.WSMsgType.CLOSE
    assert message.data == 1008


@pytest.mark.asyncio
async def test_plugin_event_websocket_limits_each_client_and_releases_slot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(plugin_api, "MAX_EVENT_CONNECTIONS_PER_CLIENT", 1)
    async with _server(tmp_path) as (server, _tokens, issued, _events):
        url = f"http://127.0.0.1:{server.bound_port}/v1/events"
        async with aiohttp.ClientSession() as client:
            first = await client.ws_connect(url, headers=_headers(issued.token))
            await first.receive_json()
            limited = await client.get(url, headers=_headers(issued.token))
            limited_body = await limited.json()
            await first.close()
            # A completed client close handshake does not join the server's
            # canceled journal wait. Observe resource retirement, not one
            # interpreter-dependent event-loop scheduling turn.
            async with asyncio.timeout(1):
                while server._event_connection_total:
                    await asyncio.sleep(0.001)
            replacement = await client.ws_connect(url, headers=_headers(issued.token))
            await replacement.receive_json()
            await replacement.close()

    assert limited.status == 429
    assert limited_body["error"]["code"] == "connection_limit"


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


class _PausedRequest(dict):
    def __init__(self, token, *, path="/v1/devices/42/light", release=None):
        super().__init__()
        from multidict import MultiDict

        self.headers = _headers(token)
        self.path = path
        self.method = "PUT" if path.endswith("light") else "GET"
        self.match_info = {"camera_id": "42"}
        self.query = MultiDict()
        self.content_type = "application/json"
        self.entered = asyncio.Event()
        self.release = release

    async def json(self, *, loads):
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return loads('{"on":true}')


def _offline_server(tmp_path, **providers):
    tokens = PluginTokenStore(tmp_path / "integrations")
    issued = tokens.create("fixture")
    events = EventJournal(max_events=4)

    def status():
        value = _status()
        value["cameras"][0]["has_light"] = True
        return value

    server = PluginApiServer(
        host="127.0.0.1",
        port=0,
        token_store=tokens,
        event_journal=events,
        status_provider=status,
        **providers,
    )
    return server, tokens, issued, events


class _OfflineSocket:
    def __init__(self, on_send=None, receive=None):
        self.closed = False
        self.prepared = False
        self.headers = {}
        self.messages = []
        self.close_code = None
        self.on_send = on_send
        self.receiver = receive

    async def prepare(self, _request):
        self.prepared = True

    async def send_json(self, message):
        self.messages.append(message)
        if self.on_send:
            self.on_send(message)

    async def receive(self):
        if self.receiver:
            return await self.receiver()
        await asyncio.Event().wait()

    async def close(self, *, code=1000, message=b""):
        self.closed = True
        self.close_code = code


@pytest.mark.asyncio
async def test_revocation_during_body_does_not_submit_control(tmp_path):
    calls = []

    async def provider(*args):
        calls.append(args)

    server, tokens, issued, _events = _offline_server(tmp_path, control_provider=provider)
    release = asyncio.Event()
    request = _PausedRequest(issued.token, release=release)
    task = asyncio.create_task(server._security_middleware(request, server._set_light))
    await request.entered.wait()
    tokens.revoke(issued.identity.id)
    release.set()
    response = await task
    assert response.status == 401
    assert calls == []
    assert not server._active_requests


@pytest.mark.asyncio
async def test_revocation_after_provider_queue_checks_submission_admission(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def provider(*_args, admission):
        entered.set()
        await release.wait()
        if not admission():
            raise PluginCameraOperationError(
                "unauthorized", "Plugin authentication failed", status=401
            )
        effects.append(True)

    server, tokens, issued, _events = _offline_server(
        tmp_path,
        control_provider=provider,
        provider_admission=True,
    )
    request = _PausedRequest(issued.token)
    task = asyncio.create_task(server._security_middleware(request, server._set_light))
    await entered.wait()
    tokens.revoke(issued.identity.id)
    release.set()
    assert (await task).status == 401
    assert not effects


@pytest.mark.asyncio
async def test_control_body_wait_has_whole_request_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api, "CAMERA_CONTROL_TIMEOUT_SECONDS", 0.01)

    async def provider(*_args):
        raise AssertionError("body never completed")

    server, _tokens, issued, _events = _offline_server(tmp_path, control_provider=provider)
    request = _PausedRequest(issued.token, release=asyncio.Event())
    response = await server._security_middleware(request, server._set_light)
    assert response.status == 504


@pytest.mark.asyncio
async def test_snapshot_result_after_revocation_is_not_delivered(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(*_args):
        entered.set()
        await release.wait()
        return b"\xff\xd8private\xff\xd9"

    server, tokens, issued, _events = _offline_server(tmp_path, snapshot_provider=provider)
    request = _PausedRequest(issued.token, path="/v1/devices/42/snapshot")
    task = asyncio.create_task(server._security_middleware(request, server._snapshot))
    await entered.wait()
    tokens.revoke(issued.identity.id)
    release.set()
    response = await task
    assert response.status == 401
    assert b"private" not in response.body


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [True, False])
async def test_revoked_event_socket_never_delivers_replay_or_live_events(
    tmp_path, monkeypatch, replay
):
    from multidict import MultiDict

    server, tokens, issued, events = _offline_server(tmp_path)
    request = _PausedRequest(issued.token, path="/v1/events")
    if replay:
        events.publish("doorbell", camera_id="42", payload={"event_id": "private"})
        request.query = MultiDict(epoch=events.epoch, after="0")

    def sent(message):
        if message["type"] == "hello":
            tokens.revoke(issued.identity.id)
            if not replay:
                events.publish("doorbell", camera_id="42", payload={"event_id": "private"})

    socket = _OfflineSocket(on_send=sent)
    monkeypatch.setattr(plugin_api.web, "WebSocketResponse", lambda **_kwargs: socket)
    await server._security_middleware(request, server._event_socket)
    assert [message["type"] for message in socket.messages] == ["hello"]
    assert socket.close_code == 4003


@pytest.mark.asyncio
async def test_old_epoch_replay_is_rejected_after_hello(tmp_path, monkeypatch):
    from multidict import MultiDict

    server, _tokens, issued, events = _offline_server(tmp_path)
    events.publish("doorbell", camera_id="42", payload={"event_id": "private"})
    request = _PausedRequest(issued.token, path="/v1/events")
    request.query = MultiDict(epoch=events.epoch, after="0")
    socket = _OfflineSocket(
        on_send=lambda message: events.reset() if message["type"] == "hello" else None
    )
    monkeypatch.setattr(plugin_api.web, "WebSocketResponse", lambda **_kwargs: socket)
    await server._security_middleware(request, server._event_socket)
    assert [message["type"] for message in socket.messages] == ["hello", "resync_required"]
    assert socket.close_code == 1012


@pytest.mark.asyncio
async def test_intercom_revocation_blocks_pcm_and_active_feedback(tmp_path, monkeypatch):
    from types import SimpleNamespace

    stream = FakeIntercomStream()

    async def provider(_camera_id):
        return stream

    server, tokens, issued, _events = _offline_server(tmp_path, intercom_provider=provider)
    request = _PausedRequest(issued.token, path="/v1/intercom/42")

    async def receive():
        return SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=bytes(INTERCOM_FRAME_BYTES))

    socket = _OfflineSocket(
        on_send=lambda message: (
            tokens.revoke(issued.identity.id) if message["type"] == "ready" else None
        ),
        receive=receive,
    )
    monkeypatch.setattr(plugin_api.web, "WebSocketResponse", lambda **_kwargs: socket)
    await server._security_middleware(request, server._intercom_socket)
    assert stream.frames == []
    assert stream.closed.is_set()
    assert [message["type"] for message in socket.messages] == ["ready"]
    assert socket.close_code == 4003


@pytest.mark.asyncio
async def test_intercom_revocation_during_activation_has_no_active_feedback(tmp_path, monkeypatch):
    from types import SimpleNamespace

    stream = FakeIntercomStream()

    async def provider(_camera_id):
        return stream

    server, tokens, issued, _events = _offline_server(tmp_path, intercom_provider=provider)

    async def ready(_timeout):
        tokens.revoke(issued.identity.id)
        return True

    stream.wait_ready = ready

    async def receive():
        return SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=bytes(INTERCOM_FRAME_BYTES))

    socket = _OfflineSocket(receive=receive)
    monkeypatch.setattr(plugin_api.web, "WebSocketResponse", lambda **_kwargs: socket)
    request = _PausedRequest(issued.token, path="/v1/intercom/42")
    await server._security_middleware(request, server._intercom_socket)
    assert len(stream.frames) == 1  # Admitted before revocation; cannot be undone.
    assert [message["type"] for message in socket.messages] == ["ready"]
    assert stream.closed.is_set()


@pytest.mark.asyncio
async def test_revoke_client_cancels_paused_request_and_releases_admission(tmp_path):
    entered, retired = asyncio.Event(), asyncio.Event()

    async def provider(*_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()

    server, tokens, issued, _events = _offline_server(tmp_path, snapshot_provider=provider)
    request = _PausedRequest(issued.token, path="/v1/devices/42/snapshot")
    task = asyncio.create_task(server._security_middleware(request, server._snapshot))
    await entered.wait()
    tokens.revoke(issued.identity.id)
    await server.revoke_client(issued.identity.id)
    assert task.cancelled()
    assert retired.is_set()
    assert not server._active_requests
