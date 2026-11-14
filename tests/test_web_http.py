"""Real TLS/browser-cookie requests against the complete typed web application."""

import asyncio
import copy
import ssl
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from halo_server.paths import ServerPaths
from halo_server.web.auth import COOKIE, IDLE_SECONDS, issue_setup
from halo_server.web.defaults import OWNER_USERNAME
from halo_server.web.errors import WebError
from halo_server.web.routes import PREFIX, WebConsole, create_application
from halo_server.web.settings import (
    SettingsStore,
    access_info,
    configure,
    tls_context,
    web_directory,
)

PASSWORD = "a long private fixture password"
PAIRING_REVISION = "a" * 64


class OwnerFixture:
    def __init__(self):
        self.available = True
        self.calls = []
        self.reveal_entered = self.reveal_release = None
        self.pairing_entered = self.pairing_release = None
        self.view = {
            "schema": 1,
            "epoch": "account-a",
            "observed_at": 123,
            "scope": {"profile_id": "profile-a", "account_generation": 1},
            "homekit": {"adapter_boot_id": "adapter-a", "registry_revision": 2},
            "devices": [{"camera_id": "camera-a"}],
        }
        self.pairing = {
            "profile_id": "profile-a",
            "account_generation": 1,
            "registry_revision": 2,
            "boot_id": "adapter-a",
            "cameras": [
                {
                    "camera_id": "camera-a",
                    "accessory_id": "accessory-a",
                    "pairing_revision": PAIRING_REVISION,
                    "pending_reset": False,
                }
            ],
        }

    async def owner_state(self):
        self.calls.append("state")
        if not self.available:
            raise OSError("offline")
        return copy.deepcopy(self.view)

    async def homekit_pairing_list(self):
        self.calls.append("pairing")
        result = copy.deepcopy(self.pairing)
        if self.pairing_entered:
            self.pairing_entered.set()
            await self.pairing_release.wait()
        return result

    async def homekit_pairing_reveal(self, page, camera):
        self.calls.append("reveal")
        if self.reveal_entered:
            self.reveal_entered.set()
            await self.reveal_release.wait()
        return {
            key: page[key]
            for key in ("profile_id", "account_generation", "registry_revision", "boot_id")
        } | {
            "camera_id": camera["camera_id"],
            "accessory_id": camera["accessory_id"],
            "pairing_revision": camera["pairing_revision"],
            "pin": "123-45-678",
            "setup_uri": "X-HM://000000000FIXT",
            "expires_in_ms": 60000,
        }

    async def homekit_cameras(self):
        self.calls.append("selection")
        return {
            "profile_id": "profile-a",
            "account_generation": 1,
            "revision": 2,
            "cameras": [
                {"camera_id": "camera-a", "selected": False},
                {"camera_id": "camera-b", "selected": True},
            ],
        }

    async def homekit_select(self, profile, generation, camera_ids, revision):
        self.calls.append(("select", profile, generation, camera_ids, revision))
        return {"revision": 3}

    async def doctor(self, *, probe=False):
        self.calls.append(("doctor", probe))
        return {"schema": 1, "findings": []}

    async def support_report(self):
        return {"schema": 1, "kind": "support", "findings": []}


@pytest_asyncio.fixture
async def console(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "run")
    paths.prepare()
    configure(paths)
    setup = issue_setup(paths)
    settings = SettingsStore(paths).load()
    owner = OwnerFixture()
    application = WebConsole(paths, settings, client=owner)
    server = TestServer(create_application(application), host="127.0.0.1", scheme="https")
    await server.start_server(ssl=tls_context(paths, settings))
    # TestServer binds an ephemeral port; apply its exact authority to the fixture.
    application.settings = replace(settings, port=server.port)
    saved = SettingsStore(paths).save(application.settings, expected_revision=settings.revision)
    application.settings = saved
    application.applied_access = access_info(paths, saved)
    trust = ssl.create_default_context(cafile=str(web_directory(paths) / saved.certificate_file))
    client = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=trust), cookie_jar=aiohttp.CookieJar(unsafe=True)
    )
    result = SimpleNamespace(
        paths=paths,
        web=application,
        owner=owner,
        server=server,
        client=client,
        code=setup["setup_code"],
        csrf=None,
    )

    async def call(method, route, *, headers=None, **kwargs):
        return await client.request(
            method, str(server.make_url(PREFIX + route)), headers=headers, **kwargs
        )

    async def enroll():
        response = await call("GET", "/session")
        session = (await response.json())["result"]
        headers = {"Origin": application.settings.origin, "X-Halo-CSRF": session["csrf_token"]}
        response = await call(
            "POST",
            "/setup",
            headers=headers,
            json={"setup_code": result.code, "username": OWNER_USERNAME, "password": PASSWORD},
        )
        assert response.status == 200, await response.text()
        result.csrf = (await response.json())["result"]["csrf_token"]
        return {"Origin": application.settings.origin, "X-Halo-CSRF": result.csrf}

    result.call, result.enroll = call, enroll
    try:
        yield result
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_https_setup_cookie_rotation_host_origin_and_csrf(console):
    response = await console.call("GET", "/state")
    assert response.status == 401
    response = await console.call("GET", "/session")
    cookie = response.cookies[COOKIE]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Strict"
    assert cookie["path"] == "/" and not cookie["domain"]
    anonymous_token = cookie.value
    session = (await response.json())["result"]
    payload = {"setup_code": console.code, "username": OWNER_USERNAME, "password": PASSWORD}
    for headers in (
        {},
        {"Origin": "https://evil.example", "X-Halo-CSRF": session["csrf_token"]},
        {"Origin": console.web.settings.origin, "X-Halo-CSRF": "wrong"},
    ):
        assert (await console.call("POST", "/setup", headers=headers, json=payload)).status == 403
    headers = await console.enroll()
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    assert token != anonymous_token
    for extra in (
        {"Host": "evil.example"},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        assert (await console.call("GET", "/state", headers=extra)).status == 403
    response = await console.call("GET", "/state")
    assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    response = await console.call("POST", "/logout", headers=headers, json={})
    assert response.status == 200 and (await console.call("GET", "/state")).status == 401


@pytest.mark.asyncio
async def test_selection_preserves_other_cameras_and_never_replays(console):
    headers = await console.enroll()
    payload = {
        "camera_id": "camera-a",
        "enabled": True,
        "profile_id": "profile-a",
        "account_generation": 1,
        "expected_revision": 2,
        "operation_id": str(uuid4()),
    }
    for _ in range(2):
        response = await console.call("POST", "/selection", headers=headers, json=payload)
        assert response.status == 200 and (await response.json())["result"]["status"] == "completed"
    assert [row for row in console.owner.calls if isinstance(row, tuple)] == [
        ("select", "profile-a", 1, ["camera-a", "camera-b"], 2)
    ]
    payload["operation_id"], payload["expected_revision"] = str(uuid4()), 1
    receipt = (
        await (await console.call("POST", "/selection", headers=headers, json=payload)).json()
    )["result"]
    assert receipt["status"] == "rejected" and receipt["code"] == "configuration_conflict"
    listing = (await (await console.call("GET", "/operations")).json())["result"]["operations"]
    assert len(listing) == 2 and all("result" not in row and "digest" not in row for row in listing)


def pairing_target(console):
    return {
        key: console.owner.pairing[key]
        for key in ("profile_id", "account_generation", "registry_revision", "boot_id")
    } | {"camera_id": "camera-a", "pairing_revision": PAIRING_REVISION}


@pytest.mark.asyncio
async def test_pairing_secrets_require_recent_auth_and_never_enter_state(console):
    headers = await console.enroll()
    session = next(row for row in console.web.auth.sessions.values() if row.authenticated)
    session.reauthenticated_at -= 301
    response = await console.call(
        "POST", "/pairing/reveal", headers=headers, json=pairing_target(console)
    )
    assert response.status == 403 and "reveal" not in console.owner.calls
    response = await console.call("POST", "/reauth", headers=headers, json={"password": PASSWORD})
    headers["X-Halo-CSRF"] = (await response.json())["result"]["csrf_token"]
    response = await console.call(
        "POST", "/pairing/reveal", headers=headers, json=pairing_target(console)
    )
    assert response.status == 200 and (await response.json())["result"]["pin"] == "123-45-678"
    assert response.headers["Cache-Control"] == "no-store"
    for route in ("/state", "/pairing", "/operations"):
        assert "123-45-678" not in await (await console.call("GET", route)).text()


@pytest.mark.asyncio
async def test_logout_while_reveal_in_flight_discards_secret(console):
    headers = await console.enroll()
    console.owner.reveal_entered, console.owner.reveal_release = asyncio.Event(), asyncio.Event()
    request = asyncio.create_task(
        console.call("POST", "/pairing/reveal", headers=headers, json=pairing_target(console))
    )
    await console.owner.reveal_entered.wait()
    assert (await console.call("POST", "/logout", headers=headers, json={})).status == 200
    console.owner.reveal_release.set()
    response = await request
    assert response.status == 401 and "123-45-678" not in await response.text()


@pytest.mark.asyncio
async def test_pairing_inflight_adapter_change_is_not_recached(console):
    await console.enroll()
    console.owner.pairing_entered, console.owner.pairing_release = asyncio.Event(), asyncio.Event()
    request = asyncio.create_task(console.web.pairing_page(force=True))
    await console.owner.pairing_entered.wait()
    console.owner.view["homekit"]["adapter_boot_id"] = "adapter-b"
    console.owner.pairing_release.set()
    with pytest.raises(WebError) as error:
        await request
    assert error.value.code == "account_changed" and console.web.pairing is None


@pytest.mark.asyncio
async def test_daemon_loss_retains_truthful_stale_view_and_local_recovery(console):
    await console.enroll()
    await console.web.state.get(force=True)
    await console.web.pairing_page()
    assert console.web.pairing is not None
    console.owner.available = False
    await console.web.state.get(force=True)
    assert console.web.pairing is None
    response = await console.call("GET", "/state")
    result = (await response.json())["result"]
    assert (
        result["daemon"]["stale"]
        and not result["daemon"]["available"]
        and result["state"]["devices"]
    )
    for route in ("/doctor", "/access", "/restart", "/session"):
        assert (await console.call("GET", route)).status == 200
    doctor = (await (await console.call("GET", "/doctor")).json())["result"]
    assert not doctor["daemon_available"] and doctor["daemon"] is None


@pytest.mark.asyncio
async def test_body_bounds_typed_allowlist_and_static_assets(console):
    headers = await console.enroll()
    for data, code in (
        ("{" + " " * 65536 + "}", 413),
        ('{"probe":true,"probe":false}', 400),
        ('{"probe":true,"command":"shutdown"}', 400),
    ):
        response = await console.call(
            "POST", "/doctor", headers=headers | {"Content-Type": "application/json"}, data=data
        )
        assert response.status == code
    response = await console.call("POST", "/doctor", headers=headers, data="probe=true")
    assert response.status == 415
    for path in (
        "/api/web/v1/rpc",
        "/api/web/v1/media",
        "/build-manifest.json",
        "/authentication.json",
        "/../settings.json",
    ):
        response = await console.client.get(str(console.server.make_url(path)))
        assert response.status == 404
    response = await console.client.get(str(console.server.make_url("/")))
    assert response.status == 200 and "<html" in await response.text()


@pytest.mark.asyncio
async def test_session_expiry_revocation_and_cli_reset_take_effect_on_next_read(console):
    await console.enroll()
    session = next(row for row in console.web.auth.sessions.values() if row.authenticated)
    session.touched -= IDLE_SECONDS
    assert (await console.call("GET", "/state")).status == 401
    anonymous = (await (await console.call("GET", "/session")).json())["result"]
    headers = {"Origin": console.web.settings.origin, "X-Halo-CSRF": anonymous["csrf_token"]}
    response = await console.call(
        "POST",
        "/login",
        headers=headers,
        json={"username": OWNER_USERNAME, "password": PASSWORD},
    )
    session = (await response.json())["result"]
    headers["X-Halo-CSRF"] = session["csrf_token"]
    response = await console.call("DELETE", "/sessions/" + session["session_id"], headers=headers)
    assert response.status == 200 and (await response.json())["result"]["current"]
    assert (await console.call("GET", "/state")).status == 401
    issue_setup(console.paths, reset=True)
    assert (await (await console.call("GET", "/session")).json())["result"]["setup_required"]


@pytest.mark.asyncio
async def test_tls_configuration_reports_saved_vs_current_identity(console):
    await console.enroll()
    applied = console.web.applied_access
    configure(console.paths, port=console.server.port, renew_certificate=True)
    response = await console.call("GET", "/access")
    result = (await response.json())["result"]
    assert result["certificate_fingerprint"] == applied["certificate_fingerprint"]
    assert (
        result["restart_required"]
        and result["saved"]["certificate_fingerprint"] != result["certificate_fingerprint"]
    )


@pytest.mark.asyncio
async def test_event_feed_has_bounded_cursor_protocol_and_never_extends_idle_session(console):
    await console.enroll()
    session = next(row for row in console.web.auth.sessions.values() if row.authenticated)
    touched = session.touched
    response = await console.call("GET", "/events", headers={"Last-Event-ID": "previous-server:1"})
    assert response.status == 200 and response.content_type == "text/event-stream"
    packet = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 2)
    assert b"event: resync" in packet and b"id: " in packet
    assert session.touched == touched
    console.web.auth.revoke(session.key)
    await console.web.state.get(force=True)
    assert await asyncio.wait_for(response.content.read(), 2) == b""
    response.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disconnect", "timeout", "cancel", "unexpected"])
async def test_event_handshake_failures_release_capacity_and_allow_https_reconnect(
    console, monkeypatch, failure
):
    await console.enroll()
    session = next(row for row in console.web.auth.sessions.values() if row.authenticated)
    entered = asyncio.Event()

    async def failed_prepare(_response, _request):
        entered.set()
        if failure == "cancel":
            await asyncio.Future()
        if failure == "disconnect":
            raise ConnectionResetError("Synthetic disconnect during event response headers")
        if failure == "timeout":
            raise TimeoutError("Synthetic event response header timeout")
        raise RuntimeError("Synthetic unexpected event response failure")

    with monkeypatch.context() as patch:
        patch.setattr(web.StreamResponse, "prepare", failed_prepare)
        # More failures than the shared subscriber capacity must not poison the
        # next connection. Cancel a real task suspended inside the handshake.
        for _ in range(9):
            entered.clear()
            request = asyncio.create_task(console.web.events(SimpleNamespace(headers={})))
            await asyncio.wait_for(entered.wait(), 2)
            if failure == "cancel":
                request.cancel()
            if failure == "unexpected":
                with pytest.raises(RuntimeError, match="Synthetic unexpected"):
                    await request
            else:
                await request
            assert not console.web.state.subscribers

    # Verify recovered capacity through the real authenticated HTTPS endpoint,
    # including normal feed retirement. Owner state remains synthetic.
    response = await console.call("GET", "/events")
    assert response.status == 200 and response.content_type == "text/event-stream"
    packet = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 2)
    assert b"event: state" in packet
    assert len(console.web.state.subscribers) == 1
    console.web.auth.revoke(session.key)
    await console.web.state.get(force=True)
    assert await asyncio.wait_for(response.content.read(), 2) == b""
    response.close()
    assert not console.web.state.subscribers


@pytest.mark.asyncio
@pytest.mark.parametrize("anonymous_cookie", [False, True])
async def test_every_private_http_route_rejects_unsigned_owner(console, anonymous_cookie):
    headers = {"Origin": console.web.settings.origin}
    if anonymous_cookie:
        response = await console.call("GET", "/session")
        headers["X-Halo-CSRF"] = (await response.json())["result"]["csrf_token"]
    routes = set()
    for route in console.server.app.router.routes():
        path = route.resource.canonical
        if not path.startswith(PREFIX):
            continue
        paths = [path]
        if "{action}" in path:
            paths = [path.replace("{action}", value) for value in ("logout", "reauth", "password")]
        elif "{read}" in path:
            paths = [
                path.replace("{read}", value)
                for value in ("selection", "controls", "recording", "configuration")
            ]
        elif "{identifier}" in path:
            paths = [path.replace("{identifier}", "fixture")]
        for candidate in paths:
            assert "{" not in candidate, "Add a representative URL for this route parameter"
            if candidate == PREFIX + "/session":
                continue
            routes.add((route.method, candidate.removeprefix(PREFIX)))
    assert ("POST", "/devices/alarm/mode") in routes
    assert ("POST", "/devices/chime/set") in routes
    assert ("POST", "/restart") in routes
    for method, path in sorted(routes):
        response = await console.call(method, path, headers=headers)
        assert response.status == 401, (method, path, response.status)
        await response.read()
    assert not console.owner.calls
    assert not console.web.operations.rows


@pytest.mark.asyncio
async def test_http_login_requires_username_and_password_and_rotates_cookie(console):
    headers = await console.enroll()
    response = await console.call("POST", "/logout", headers=headers, json={})
    assert response.status == 200
    response = await console.call("GET", "/session")
    before = response.cookies[COOKIE].value
    session = (await response.json())["result"]
    headers["X-Halo-CSRF"] = session["csrf_token"]
    for payload, status in (
        ({"password": PASSWORD}, 400),
        ({"username": OWNER_USERNAME}, 400),
        ({"username": "visitor", "password": PASSWORD}, 401),
        ({"username": OWNER_USERNAME, "password": "a wrong private password"}, 401),
    ):
        response = await console.call("POST", "/login", headers=headers, json=payload)
        assert response.status == status
        if status == 401:
            assert (await response.json())["error"]["code"] == "authentication_failed"
        assert (await console.call("GET", "/state")).status == 401
    response = await console.call(
        "POST",
        "/login",
        headers=headers,
        json={"username": OWNER_USERNAME, "password": PASSWORD},
    )
    assert response.status == 200
    assert response.cookies[COOKIE].value != before
    assert (await response.json())["result"]["authenticated"]
    assert (await console.call("GET", "/state")).status == 200


@pytest.mark.asyncio
async def test_http_setup_requires_username_without_changing_private_state(console):
    response = await console.call("GET", "/session")
    session = (await response.json())["result"]
    headers = {"Origin": console.web.settings.origin, "X-Halo-CSRF": session["csrf_token"]}
    before = console.web.auth.record
    response = await console.call(
        "POST",
        "/setup",
        headers=headers,
        json={"setup_code": console.code, "password": PASSWORD},
    )
    assert response.status == 400
    response = await console.call(
        "POST",
        "/setup",
        headers=headers,
        json={"setup_code": console.code, "username": "visitor", "password": PASSWORD},
    )
    assert response.status == 401
    assert console.web.auth.record == before
    assert (await console.call("GET", "/state")).status == 401
    await console.enroll()
