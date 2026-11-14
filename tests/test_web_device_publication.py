"""TLS routes for the separate bridge enforce owner auth, exact identity and reset replay."""

import asyncio
from uuid import uuid4

import pytest
from test_web_http import console as console  # noqa: F401

from halo_server.owner_client import AdminClientError
from halo_server.web.auth import COOKIE


def install(console):
    page = {
        "profile_id": str(uuid4()),
        "account_generation": 1,
        "registry_revision": 3,
        "publication_revision": 1,
        "accessory_id": str(uuid4()),
        "username": "02:11:22:33:44:55",
        "boot_id": str(uuid4()),
        "available": True,
        "published": True,
        "paired_known": True,
        "paired": False,
        "paired_clients": 0,
        "pairing_revision": "a" * 64,
        "pending_reset": False,
        "reset_operation_id": None,
    }
    calls = []

    async def get():
        return dict(page)

    async def reveal(**params):
        calls.append(("reveal", params))
        return {
            **params,
            "pin": "031-45-154",
            "setup_uri": "X-HM://0023ISYWYHALO",
            "expires_in_ms": 30000,
        }

    async def configure(**params):
        calls.append(("configure", params))
        return {"publication_revision": 2, "publication_enabled": params["enabled"]}

    console.owner.devices_bridge_pairing_get = get
    console.owner.devices_bridge_pairing_reveal = reveal
    console.owner.devices_bridge_publication_set = configure
    return page, calls


def identity(page, **updates):
    from halo_server.devices.publication import PAIRING_FIELDS

    return {key: page[key] for key in PAIRING_FIELDS} | updates


@pytest.mark.asyncio
async def test_bridge_routes_auth_csrf_recent_auth_and_no_secret_journal(console):  # noqa: F811
    page, calls = install(console)
    path = "/devices/bridge/pairing/reveal"
    assert (await console.call("GET", "/devices/bridge/pairing")).status == 401
    assert (await console.call("POST", path, json=identity(page))).status in {401, 403}
    headers = await console.enroll()
    assert (await console.call("POST", path, json=identity(page))).status == 403
    response = await console.call("POST", path, json=identity(page), headers=headers)
    assert response.status == 200, await response.text()
    assert (await response.json())["result"]["pin"] == "031-45-154"
    assert not console.web.operations.rows
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    console.web.auth.get(token).reauthenticated_at -= 301
    assert (await console.call("POST", path, json=identity(page), headers=headers)).status == 403
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_publication_exact_payload_replay_and_scope(console):  # noqa: F811
    page, calls = install(console)
    headers = await console.enroll()
    payload = {
        "profile_id": page["profile_id"],
        "account_generation": 1,
        "expected_registry_revision": 3,
        "expected_revision": 1,
        "enabled": True,
        "operation_id": str(uuid4()),
    }
    for _ in range(2):
        response = await console.call(
            "POST", "/devices/bridge/publication", json=payload, headers=headers
        )
        assert response.status == 200
        assert (await response.json())["result"]["status"] == "completed"
    assert len(calls) == 1
    for changes in (
        {"enabled": "yes"},
        {"panic_enabled": True},
        {"expected_revision": True},
        {"enabled": False},
    ):
        response = await console.call(
            "POST", "/devices/bridge/publication", json=payload | changes, headers=headers
        )
        assert response.status in {400, 409}
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["logout", "boot", "registry", "scope"])
async def test_secret_suppressed_when_authority_changes_in_flight(console, change):  # noqa: F811
    page, calls = install(console)
    headers = await console.enroll()
    entered, release = asyncio.Event(), asyncio.Event()
    original = console.owner.devices_bridge_pairing_reveal

    async def reveal(**params):
        entered.set()
        await release.wait()
        return await original(**params)

    console.owner.devices_bridge_pairing_reveal = reveal
    task = asyncio.create_task(
        console.call("POST", "/devices/bridge/pairing/reveal", json=identity(page), headers=headers)
    )
    await entered.wait()
    if change == "logout":
        await console.call("POST", "/logout", json={}, headers=headers)
    elif change == "boot":
        page["boot_id"] = str(uuid4())
    elif change == "registry":
        page["registry_revision"] += 1
    else:
        page["account_generation"] += 1
    release.set()
    response = await task
    assert response.status in {401, 403, 409}
    assert "031-45-154" not in await response.text()


@pytest.mark.asyncio
async def test_reset_resume_same_identity_after_boot_and_generation_change(console):  # noqa: F811
    page, calls = install(console)
    headers = await console.enroll()
    operation = str(uuid4())

    async def reset(**params):
        calls.append(("reset", params))
        if len(calls) == 1:
            page.update(
                pending_reset=True,
                reset_operation_id=operation,
                boot_id=None,
                available=False,
                published=False,
            )
            raise AdminClientError("device_bridge_unavailable", "Interrupted")
        page.update(pending_reset=False, reset_operation_id=None)
        return {"completed": True, "publication_enabled": False}

    console.owner.devices_bridge_pairing_reset = reset
    response = await console.call(
        "POST",
        "/devices/bridge/pairing/reset",
        json=identity(page, operation_id=operation),
        headers=headers,
    )
    assert (await response.json())["result"]["status"] == "unknown"
    page["account_generation"] += 1
    page["registry_revision"] += 1
    response = await console.call(
        "POST",
        "/devices/bridge/pairing/reset",
        json=identity(page, operation_id=str(uuid4())),
        headers=headers,
    )
    assert response.status == 409
    response = await console.call(
        "POST",
        "/devices/bridge/pairing/reset",
        json=identity(page, operation_id=operation),
        headers=headers,
    )
    assert response.status == 200, await response.text()
    assert (await response.json())["result"]["status"] == "completed"
    assert len(calls) == 2
