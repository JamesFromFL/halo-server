"""Local-only privacy preferences require scoped, authenticated, non-replayed writes."""

from uuid import uuid4

import pytest
from test_web_http import console as console  # noqa: F401

from halo_server.owner_client import AdminClientError
from halo_server.web.auth import COOKIE


def payload(**changes):
    return {
        "profile_id": str(uuid4()),
        "account_generation": 1,
        "camera_id": "camera-a",
        "expected_revision": 0,
        "mode": "homekit",
        "operation_id": str(uuid4()),
        **changes,
    }


@pytest.mark.asyncio
async def test_privacy_authentication_scope_and_no_replay(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {"status": "completed", "revision": 1}

    console.owner.camera_privacy_set = apply
    value = payload()
    assert (await console.call("POST", "/privacy", json=value)).status in {401, 403}
    headers = await console.enroll()
    assert (await console.call("POST", "/privacy", json=value)).status == 403
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    session = console.web.auth.get(token)
    session.reauthenticated_at -= 301
    for mode in ("ring", "homekit"):
        response = await console.call(
            "POST", "/privacy", json=value | {"mode": mode}, headers=headers
        )
        assert response.status == 403
        assert (await response.json())["error"]["code"] == "recent_auth_required"
    assert not calls
    session.reauthenticated_at += 301
    for _ in range(2):
        response = await console.call("POST", "/privacy", json=value, headers=headers)
        assert response.status == 200
        assert (await response.json())["result"]["status"] == "completed"
    assert calls == [{key: item for key, item in value.items() if key != "operation_id"}]
    response = await console.call(
        "POST", "/privacy", json=value | {"mode": "ring"}, headers=headers
    )
    assert response.status == 409


@pytest.mark.asyncio
async def test_privacy_rejects_malformed_requests_before_dispatch(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)

    console.owner.camera_privacy_set = apply
    headers = await console.enroll()
    value = payload()
    for change in (
        {"mode": True},
        {"mode": []},
        {"mode": "override"},
        {"account_generation": True},
        {"expected_revision": -1},
        {"expected_revision": True},
        {"profile_id": ""},
        {"camera_id": ""},
        {"operation_id": "bad"},
        {"recording": True},
        {"alarm_mode": "disarmed"},
    ):
        response = await console.call("POST", "/privacy", json=value | change, headers=headers)
        assert response.status == 400, (change, await response.text())
    assert not calls


@pytest.mark.asyncio
async def test_privacy_revision_conflict_is_not_replayed(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        raise AdminClientError("configuration_conflict", "Preference changed")

    console.owner.camera_privacy_set = apply
    headers = await console.enroll()
    value = payload()
    for _ in range(2):
        response = await console.call("POST", "/privacy", json=value, headers=headers)
        result = (await response.json())["result"]
        assert result["status"] == "rejected"
        assert result["possibly_sent"] is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_privacy_read_is_authenticated_and_does_not_start_media(console):  # noqa: F811
    calls = []
    page = {"profile_id": None, "account_generation": 0, "available": False, "cameras": []}

    async def read():
        calls.append("privacy")
        return page

    console.owner.camera_privacy_list = read
    assert (await console.call("GET", "/privacy")).status == 401
    await console.enroll()
    response = await console.call("GET", "/privacy")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert (await response.json())["result"] == page
    assert calls == ["privacy"]
