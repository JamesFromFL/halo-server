"""Allowlisted shared-session writes; network regression uses localhost only."""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import aiohttp
import pytest
from oauthlib.oauth2 import TokenExpiredError
from ring_doorbell.const import OAuth
from test_ring_shared_auth import Response, TokenResponse, fake_auth, real_auth

from halo_server.ring.control_routes import device_url, group_url, settings_url
from halo_server.ring.device_rest import RingDeviceRestGateway
from halo_server.ring.hub.rest import AlarmRestError, RingAlarmRestGateway
from halo_server.ring.json_transport import (
    MAX_WRITE_REPLY_BYTES,
    RingJsonError,
    RingJsonTransport,
    RingWriteCancelled,
    RingWriteReceipt,
)

VOLUME = {"chime": {"settings": {"volume": 5}}}


def gateway(auth, *, core=None, admission=None):
    core = core or RingJsonTransport(auth)
    return RingDeviceRestGateway(auth, transport=core, admission=admission), core


async def volume(core, **kwargs):
    return await core.request_write(
        device_url(7), method="PUT", json=VOLUME, admission=lambda: True, **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args,kwargs,http,url,payload",
    [
        (
            "set_chime_volume",
            (7, 0),
            {},
            "PUT",
            device_url(7),
            {"chime": {"settings": {"volume": 0}}},
        ),
        (
            "set_chime_volume",
            (7, 11),
            {},
            "PUT",
            device_url(7),
            {"chime": {"settings": {"volume": 11}}},
        ),
        ("test_chime", (7, "ding"), {}, "POST", device_url(7) + "play_sound", {"kind": "ding"}),
        ("test_chime", (7, "motion"), {}, "POST", device_url(7) + "play_sound", {"kind": "motion"}),
        ("snooze_chime", (7, 1), {}, "POST", device_url(7) + "do_not_disturb", {"time": 1}),
        ("snooze_chime", (7, 1440), {}, "POST", device_url(7) + "do_not_disturb", {"time": 1440}),
        ("snooze_chime", (7, None), {}, "POST", device_url(7) + "do_not_disturb", None),
        (
            "set_chime_nightlight",
            (7, True),
            {},
            "PATCH",
            settings_url(7),
            {"night_light_settings": {"light_sensor_enabled": True}},
        ),
        (
            "set_chime_nightlight",
            (7, False),
            {},
            "PATCH",
            settings_url(7),
            {"night_light_settings": {"light_sensor_enabled": False}},
        ),
        (
            "set_light_group",
            ("home", "group", True),
            {"duration_seconds": 30},
            "POST",
            group_url("home", "group"),
            {"lights_on": {"enabled": True, "duration_seconds": 30}},
        ),
        (
            "set_light_group",
            ("home", "group", True),
            {"duration_seconds": 900},
            "POST",
            group_url("home", "group"),
            {"lights_on": {"enabled": True, "duration_seconds": 900}},
        ),
        (
            "set_light_group",
            ("home", "group", False),
            {},
            "POST",
            group_url("home", "group"),
            {"lights_on": {"enabled": False}},
        ),
    ],
)
async def test_typed_methods_send_exact_minimal_bodies_on_same_auth(
    method, args, kwargs, http, url, payload
):
    response = Response(b"", status=204)
    auth, calls, scopes = fake_auth(response)
    feature, core = gateway(auth)
    marks = []
    receipt = await getattr(feature, method)(
        *args, **kwargs, admission=lambda: True, on_admitted=lambda: marks.append("sent")
    )
    assert receipt == RingWriteReceipt("accepted", "http-accepted", 204, True)
    assert receipt.accepted and marks == ["sent"] and response.closed
    assert scopes == ["enter", "exit"]
    assert len(calls) == 1
    actual_method, actual_url, options = calls[0]
    assert (actual_method, actual_url) == (http, url)
    if payload is None:
        assert "json" not in options and "Content-Type" not in options["headers"]
    else:
        assert options["json"] == payload
        assert options["headers"]["Content-Type"] == "application/json"
    assert options["headers"]["hardware_id"] == "fixture-hardware"
    assert options["headers"]["Authorization"] == "Bearer fixture-only"
    assert options["allow_redirects"] is False and options["auto_decompress"] is False
    assert options["ssl"] is core._tls and options["timeout"].total <= 10
    with pytest.raises(FrozenInstanceError):
        receipt.outcome = "confirmed"
    await feature.close()
    assert not core._closed
    await core.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        ("read_inventory", ()),
        ("read_locations", ()),
        ("read_chime_health", (7,)),
        ("read_chime_settings", (7,)),
        ("read_light_groups", ("home",)),
        ("read_light_group_report", ("home", "group")),
    ],
)
async def test_new_reads_are_bounded_and_share_alarm_core(method, args):
    response = Response(b'{"fixture":true}')
    auth, calls, _ = fake_auth(response)
    alarm = RingAlarmRestGateway(auth)
    feature, core = gateway(auth, core=alarm.shared_transport)
    assert await getattr(feature, method)(*args) == {"fixture": True}
    assert calls[0][0] == "GET" and response.content.calls and response.closed
    await feature.close()
    assert not core._closed
    await alarm.close()
    assert core._closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,method,payload",
    [
        (device_url(7), "POST", VOLUME),
        (device_url(7), "DELETE", None),
        (device_url(7), "PUT", {"chime": {"settings": {"volume": True}}}),
        (device_url(7), "PUT", {"chime": {"settings": {"volume": 12}}}),
        (device_url(7), "PUT", {"chime": {"settings": {"volume": 5.0}}}),
        (device_url(7), "PUT", {"chime": {"settings": {"volume": 5, "ding_audio_id": 1}}}),
        (device_url(7), "PUT", {"chime": {"settings": {"volume": 5}, "description": "new"}}),
        (device_url(7) + "play_sound", "POST", {"kind": "siren"}),
        (device_url(7) + "play_sound", "POST", {"kind": "ding", "repeat": True}),
        (device_url(7) + "do_not_disturb", "POST", {}),
        (device_url(7) + "do_not_disturb", "POST", {"time": 0}),
        (device_url(7) + "do_not_disturb", "POST", {"time": True}),
        (device_url(7) + "do_not_disturb", "POST", {"time": 1441}),
        (settings_url(7), "PATCH", {"night_light_settings": {"light_sensor_enabled": 1}}),
        (
            settings_url(7),
            "PATCH",
            {"night_light_settings": {"light_sensor_enabled": True}, "siren": True},
        ),
        (group_url("home", "group"), "POST", {"lights_on": {"enabled": True}}),
        (
            group_url("home", "group"),
            "POST",
            {"lights_on": {"enabled": True, "duration_seconds": 0}},
        ),
        (
            group_url("home", "group"),
            "POST",
            {"lights_on": {"enabled": True, "duration_seconds": 60.0}},
        ),
        (
            group_url("home", "group"),
            "POST",
            {"lights_on": {"enabled": False, "duration_seconds": 60}},
        ),
        (group_url("home"), "POST", {"lights_on": {"enabled": False}}),
        ("https://api.ring.com/clients_api/doorbots/7/siren_on", "POST", {}),
        ("https://api.ring.com/clients_api/ring_devices", "POST", {}),
        (device_url(7) + "?extra=true", "PUT", VOLUME),
        (device_url(7) + "#fragment", "PUT", VOLUME),
        (device_url(7).replace("api.ring.com", "api.ring.com:443"), "PUT", VOLUME),
        (device_url(7).replace("api.ring.com", "api.ring.com.evil.test"), "PUT", VOLUME),
        (device_url(7).replace("/7/", "/07/"), "PUT", VOLUME),
        (device_url(7).replace("/7/", "/%37/"), "PUT", VOLUME),
    ],
)
async def test_core_denies_unproven_operations_without_touching_auth(url, method, payload):
    core = RingJsonTransport(SimpleNamespace())
    with pytest.raises(RingJsonError, match="operation-not-allowed"):
        await core.request_write(url, method=method, json=payload, admission=lambda: True)


@pytest.mark.parametrize(
    "identifier",
    [None, True, 1, "", ".", "..", "a/b", "a\\b", "%2f", "a\n", "a\u200b", "\ud800", "a" * 129],
)
def test_group_builder_rejects_path_escape_and_malformed_identifiers(identifier):
    with pytest.raises((ValueError, UnicodeError)):
        group_url(identifier, "group")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["../groups/g/devices", "%2e%2e", "%252F", "%2F", "g?extra=1", "g#fragment", "g%0A"]
)
async def test_core_rejects_group_traversal_or_noncanonical_path(path):
    core = RingJsonTransport(SimpleNamespace())
    url = f"https://api.ring.com/groups/v1/locations/home/groups/{path}/devices"
    with pytest.raises(RingJsonError, match="operation-not-allowed"):
        await core.request_write(
            url, method="POST", json={"lights_on": {"enabled": False}}, admission=lambda: True
        )


@pytest.mark.asyncio
async def test_alarm_wrapper_stays_read_only_and_does_not_widen_its_own_routes():
    auth, calls, _ = fake_auth()
    alarm = RingAlarmRestGateway(auth)
    with pytest.raises(AlarmRestError, match="operation-not-allowed"):
        await alarm.request_json(device_url(7), method="PUT", json=VOLUME)
    with pytest.raises(AlarmRestError, match="endpoint-not-allowed"):
        await alarm.request_json(settings_url(7))
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome,code",
    [
        (200, "accepted", "http-accepted"),
        (201, "accepted", "http-accepted"),
        (202, "accepted", "http-accepted"),
        (204, "accepted", "http-accepted"),
        (400, "rejected", "request-failed"),
        (401, "rejected", "authentication-required"),
        (403, "rejected", "permission-denied"),
        (404, "rejected", "request-failed"),
        (408, "uncertain", "request-failed"),
        (429, "rejected", "rate-limited"),
        (301, "uncertain", "redirect-not-allowed"),
        (307, "uncertain", "redirect-not-allowed"),
        (500, "uncertain", "request-failed"),
        (503, "uncertain", "request-failed"),
    ],
)
async def test_http_status_has_explicit_outcome_and_never_refreshes_or_replays(
    status, outcome, code
):
    response = Response(b"private upstream message", status=status, headers={"Retry-After": "900"})
    auth, calls, _ = fake_auth(response)
    core = RingJsonTransport(auth)
    receipt = await volume(core)
    assert (receipt.outcome, receipt.code, receipt.http_status, receipt.possibly_sent) == (
        outcome,
        code,
        status,
        True,
    )
    assert len(calls) == 1 and auth.async_refresh_tokens.await_count == 0 and response.closed
    assert "private" not in repr(receipt)
    if not 200 <= status < 300:
        assert receipt.retry_after == 900


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,data,code",
    [
        ({"Content-Encoding": "gzip"}, b"data", "unsupported-encoding"),
        ({"Content-Length": str(MAX_WRITE_REPLY_BYTES + 1)}, b"data", "response-too-large"),
        ({}, b"x" * (MAX_WRITE_REPLY_BYTES + 1), "response-too-large"),
        ({"Content-Length": "5"}, b"xx", "invalid-response"),
    ],
)
async def test_incomplete_2xx_body_is_uncertain_and_never_replayed(headers, data, code):
    response = Response(data, headers=headers)
    auth, calls, _ = fake_auth(response)
    receipt = await volume(RingJsonTransport(auth))
    assert receipt == RingWriteReceipt("uncertain", code, 200, True)
    assert len(calls) == 1 and response.closed


@pytest.mark.asyncio
async def test_expired_token_refreshes_once_before_send_and_admission_is_rechecked():
    auth, calls, _ = fake_auth(Response())
    original = auth._oauth_client.add_token
    prepared = []
    allowed = [True]

    def prepare(*args, **kwargs):
        prepared.append(True)
        if len(prepared) == 1:
            raise TokenExpiredError()
        return original(*args, **kwargs)

    async def refresh():
        allowed[0] = False
        return {}

    auth._oauth_client.add_token = prepare
    auth.async_refresh_tokens.side_effect = refresh
    core = RingJsonTransport(auth)
    result = await core.request_write(
        device_url(7), method="PUT", json=VOLUME, admission=lambda: allowed[0]
    )
    assert result == RingWriteReceipt("rejected", "admission-denied")
    assert auth.async_refresh_tokens.await_count == 1 and not calls


@pytest.mark.asyncio
async def test_preflight_refresh_timeout_has_one_total_deadline_and_no_handoff():
    auth, calls, _ = fake_auth()

    def expired(*args, **kwargs):
        raise TokenExpiredError()

    async def stalled():
        await asyncio.Event().wait()

    auth._oauth_client.add_token = expired
    auth.async_refresh_tokens.side_effect = stalled
    result = await volume(RingJsonTransport(auth), timeout=0.01)
    assert result == RingWriteReceipt("rejected", "timeout")
    assert not calls and auth.async_refresh_tokens.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["network", "body"])
async def test_timeout_after_handoff_is_uncertain_and_releases_shared_slot(where):
    response = Response(gate=asyncio.Event())

    async def stalled(url):
        await asyncio.Event().wait()

    auth, calls, scopes = fake_auth(response, request=stalled if where == "network" else None)
    core = RingJsonTransport(auth)
    result = await volume(core, timeout=0.01)
    assert result.outcome == "uncertain" and result.code == "timeout" and result.possibly_sent
    assert len(calls) == 1 and core._active == 0 and not core._requests
    assert scopes == ["enter", "exit"]
    if where == "body":
        assert response.closed


@pytest.mark.asyncio
async def test_network_exception_is_sanitized_and_not_replayed():
    async def broken(url):
        raise aiohttp.ClientConnectionError("private token and URL")

    auth, calls, _ = fake_auth(request=broken)
    receipt = await volume(RingJsonTransport(auth))
    assert receipt == RingWriteReceipt("uncertain", "network-unavailable", possibly_sent=True)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, 1, "true"])
async def test_truthy_or_missing_admission_cannot_send(value):
    auth, calls, _ = fake_auth()
    core = RingJsonTransport(auth)
    receipt = await core.request_write(
        device_url(7), method="PUT", json=VOLUME, admission=lambda: value
    )
    assert receipt == RingWriteReceipt("rejected", "admission-denied")
    assert not calls


@pytest.mark.asyncio
async def test_async_admission_is_closed_and_rejected_without_io():
    auth, calls, _ = fake_auth()

    async def admitted():
        return True

    receipt = await RingJsonTransport(auth).request_write(
        device_url(7), method="PUT", json=VOLUME, admission=admitted
    )
    assert receipt == RingWriteReceipt("rejected", "invalid-admission")
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("after_handoff", [False, True])
async def test_cancellation_preserves_send_boundary(after_handoff):
    gate = asyncio.Event()
    auth, calls, _ = fake_auth(Response(gate=gate), Response())
    core = RingJsonTransport(auth)
    marks = []
    blocker = None
    if not after_handoff:
        blocker = asyncio.create_task(
            core.request_json("https://api.ring.com/clients_api/ring_devices")
        )
        while not calls:
            await asyncio.sleep(0)
    task = asyncio.create_task(volume(core, on_admitted=lambda: marks.append(True)))
    if after_handoff:
        while not calls:
            await asyncio.sleep(0)
    else:
        while not core._background:
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(RingWriteCancelled) as error:
        await task
    assert error.value.possibly_sent is after_handoff
    assert marks == ([True] if after_handoff else [])
    if blocker:
        gate.set()
        await blocker
    assert core._active == 0 and not core._requests


@pytest.mark.asyncio
async def test_queued_write_rechecks_admission_and_copies_payload_before_wait():
    gate = asyncio.Event()
    auth, calls, _ = fake_auth(Response(gate=gate), Response())
    core = RingJsonTransport(auth)
    read = asyncio.create_task(core.request_json("https://api.ring.com/clients_api/ring_devices"))
    while not calls:
        await asyncio.sleep(0)
    payload = {"chime": {"settings": {"volume": 5}}}
    task = asyncio.create_task(
        core.request_write(device_url(7), method="PUT", json=payload, admission=lambda: True)
    )
    while not core._background:
        await asyncio.sleep(0)
    payload["chime"]["settings"]["volume"] = 11
    gate.set()
    await read
    assert (await task).accepted
    assert calls[1][2]["json"]["chime"]["settings"]["volume"] == 5


@pytest.mark.asyncio
async def test_feature_close_cancels_only_its_own_write_not_shared_alarm_request():
    body_gate = asyncio.Event()
    first, second = Response(gate=body_gate), Response()
    auth, calls, _ = fake_auth(first, second)
    core = RingJsonTransport(auth)
    feature, _ = gateway(auth, core=core)
    alarm = RingAlarmRestGateway(auth, transport=core)
    task = asyncio.create_task(feature.test_chime(7, "ding", admission=lambda: True))
    while not calls:
        await asyncio.sleep(0)
    await feature.close()
    with pytest.raises(RingWriteCancelled) as error:
        await task
    assert error.value.possibly_sent and first.closed and not core._closed
    assert await alarm.request_json("https://api.ring.com/clients_api/ring_devices") == {}
    await alarm.close()
    assert not core._closed
    await core.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_persist", [False, True])
async def test_real_sdk_write_refresh_persists_before_handoff_and_does_not_fork_auth(fail_persist):
    persisted, calls, marks = [], [], []

    class Session:
        async def request(self, method, url, **kwargs):
            calls.append((method, str(url)))
            if str(url) == OAuth.ENDPOINT:
                return TokenResponse()
            assert len(persisted) == 1
            assert kwargs["headers"]["Authorization"] == "Bearer access-b"
            return Response()

    auth = real_auth(Session(), persisted, expired=True, fail_persist=fail_persist)
    receipt = await volume(RingJsonTransport(auth), on_admitted=lambda: marks.append(True))
    assert calls[0][1] == OAuth.ENDPOINT
    if fail_persist:
        assert receipt == RingWriteReceipt("rejected", "token-refresh-failed")
        assert len(calls) == 1 and not persisted and not marks
    else:
        assert receipt.accepted and calls[1] == ("PUT", device_url(7))
        assert len(calls) == 2 and marks == [True]


@pytest.mark.asyncio
async def test_queue_revocation_and_deadline_both_reject_before_http_handoff():
    gate = asyncio.Event()
    auth, calls, _ = fake_auth(Response(gate=gate), Response())
    core = RingJsonTransport(auth)
    read = asyncio.create_task(core.request_json("https://api.ring.com/clients_api/ring_devices"))
    while not calls:
        await asyncio.sleep(0)
    timed = await volume(core, timeout=0.01)
    assert timed == RingWriteReceipt("rejected", "timeout")
    permit = [True]
    task = asyncio.create_task(
        core.request_write(device_url(7), method="PUT", json=VOLUME, admission=lambda: permit[0])
    )
    while not core._background:
        await asyncio.sleep(0)
    permit[0] = False
    gate.set()
    await read
    assert await task == RingWriteReceipt("rejected", "admission-denied")
    assert len(calls) == 1 and not core._background


@pytest.mark.asyncio
async def test_write_queue_is_bounded_and_recovery_keeps_shared_priority_slot():
    gate = asyncio.Event()
    auth, calls, _ = fake_auth(Response(gate=gate), Response(), Response())
    core = RingJsonTransport(auth, max_pending_background=1)
    first = asyncio.create_task(volume(core))
    while not calls:
        await asyncio.sleep(0)
    queued = asyncio.create_task(volume(core))
    while not core._background:
        await asyncio.sleep(0)
    assert await volume(core) == RingWriteReceipt("rejected", "busy")
    alarm = RingAlarmRestGateway(auth, transport=core)
    assert await alarm.request_json("https://api.ring.com/clients_api/ring_devices") == {}
    assert len(calls) == 2 and calls[1][0] == "GET"
    gate.set()
    assert (await first).accepted and (await queued).accepted
    assert len(calls) == 3 and core._active == 0
    await alarm.close()
    await core.close()


@pytest.mark.asyncio
async def test_oauth_endpoint_or_body_change_cannot_redirect_credentials():
    auth, calls, _ = fake_auth()

    def prepare(url, **kwargs):
        return "https://evil.test/", kwargs["headers"], "private-body"

    auth._oauth_client.add_token = prepare
    assert await volume(RingJsonTransport(auth)) == RingWriteReceipt(
        "rejected", "token-preparation-changed-endpoint"
    )
    assert not calls


@pytest.mark.asyncio
async def test_arbitrary_guard_error_text_never_enters_public_receipt():
    auth, calls, _ = fake_auth()

    def guard():
        raise RingJsonError("private credential detail")

    receipt = await RingJsonTransport(auth).request_write(
        device_url(7), method="PUT", json=VOLUME, admission=guard
    )
    assert receipt == RingWriteReceipt("rejected", "admission-denied") and not calls


@pytest.mark.asyncio
async def test_no_preflight_callback_can_smuggle_async_or_failed_write_marker():
    auth, calls, _ = fake_auth()

    async def marker():
        return True

    receipt = await volume(RingJsonTransport(auth), on_admitted=marker)
    assert receipt == RingWriteReceipt("rejected", "invalid-admission") and not calls


def test_feature_cannot_inject_another_accounts_shared_core():
    auth, _, _ = fake_auth()
    other, _, _ = fake_auth()
    with pytest.raises(ValueError):
        RingDeviceRestGateway(auth, transport=RingJsonTransport(other))


@pytest.mark.asyncio
async def test_actual_aiohttp_put_disconnect_cannot_replay_submitted_write():
    """Exercise aiohttp's real retry loop, redirecting only this fixture to loopback."""
    received = []
    handlers = set()

    async def disconnect(reader, writer):
        current = asyncio.current_task()
        handlers.add(current)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            received.append(await reader.readexactly(length))
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(current)

    server = await asyncio.start_server(disconnect, "127.0.0.1", 0)
    local = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/fixture"
    try:
        async with aiohttp.ClientSession() as session:
            # Show that this installed aiohttp would actually replay the same
            # fully submitted PUT after the server drops its response.
            with pytest.raises(aiohttp.ServerDisconnectedError):
                await session.put(local, json=VOLUME)
            assert len(received) == 2
            received.clear()
            auth, _, _ = fake_auth()

            class LoopbackSession:
                _middlewares = ()

                async def request(self, method, url, *, middlewares, **kwargs):
                    assert str(url) == device_url(7)
                    assert len(middlewares) == 1
                    guard = middlewares[0]

                    async def local_guard(request, handler):
                        # Production still only admits the exact Ring route.
                        # Adapt the URL identity at this fixture boundary so
                        # the actual aiohttp transport can run on localhost.
                        identity = SimpleNamespace(url=url, method=request.method)

                        async def send(_identity):
                            return await handler(request)

                        return await guard(identity, send)

                    return await session.request(
                        method, local, middlewares=(local_guard,), **kwargs
                    )

            auth._session = LoopbackSession()
            core = RingJsonTransport(auth)
            receipt = await volume(core)
            assert receipt == RingWriteReceipt("uncertain", "write-replay-blocked", None, True)
            assert len(received) == 1
            assert core._active == 0 and not core._requests
            await core.close()
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_request_middleware_rechecks_authority_and_rejects_changed_destination(revoked):
    auth, _, _ = fake_auth()
    allowed = [True]
    sent = []

    class Session:
        _middlewares = ()

        async def request(self, method, url, *, middlewares, **_kwargs):
            await asyncio.sleep(0)
            allowed[0] = not revoked
            request = SimpleNamespace(
                url=url if revoked else "https://evil.test/", method=method
            )

            async def send(request):
                sent.append(request)
                return Response()

            return await middlewares[-1](request, send)

    auth._session = Session()
    receipt = await RingJsonTransport(auth).request_write(
        device_url(7), method="PUT", json=VOLUME, admission=lambda: allowed[0]
    )
    assert receipt.outcome == "uncertain" and receipt.possibly_sent
    assert receipt.code == (
        "admission-denied" if revoked else "token-preparation-changed-endpoint"
    )
    assert not sent
