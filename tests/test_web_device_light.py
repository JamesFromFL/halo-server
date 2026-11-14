"""Light HTTPS policy, ordinary controls, exact shapes and no-replay outcomes."""

from uuid import uuid4

import pytest
from test_web_http import console as console  # noqa: F401

from halo_server.web.auth import COOKIE


def set_payload(**changes):
    return {
        "profile_id": str(uuid4()),
        "account_generation": 1,
        "device_id": "a" * 64,
        "provider_generation": 1,
        "expected_epoch": 2,
        "expected_revision": 3,
        "expected_policy_revision": 1,
        "target": True,
        "operation_id": str(uuid4()),
        **changes,
    }


@pytest.mark.asyncio
async def test_light_requires_auth_csrf_but_not_password_for_every_toggle(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {"status": "confirmed", "code": None, "on": True, "possibly_sent": True}

    console.owner.devices_light_set = apply
    value = set_payload()
    path = "/devices/light/set"
    assert (await console.call("POST", path, json=value)).status in {401, 403}
    headers = await console.enroll()
    assert (await console.call("POST", path, json=value)).status == 403
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    console.web.auth.get(token).reauthenticated_at -= 301
    for _ in range(2):
        response = await console.call("POST", path, json=value, headers=headers)
        assert response.status == 200
        assert (await response.json())["result"]["status"] == "completed"
    assert calls == [value]
    for change in (
        {"target": "on"},
        {"target": 1},
        {"target": []},
        {"target": None},
        {"duration_seconds": 86400},
        {"brightness": 90},
        {"group_id": "g"},
        {"device_id": "raw-id"},
        {"expected_epoch": True},
    ):
        assert (
            await console.call("POST", path, json=value | change, headers=headers)
        ).status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome", [("uncertain", "unknown"), ("rejected", "rejected"), ("busy", "rejected")]
)
async def test_light_uncertain_not_success_or_replayed(console, status, outcome):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {
            "status": status,
            "code": "outcome-unknown",
            "on": True,
            "possibly_sent": status == "uncertain",
        }

    console.owner.devices_light_set = apply
    value = set_payload(target=False)
    headers = await console.enroll()
    for _ in range(2):
        response = await console.call("POST", "/devices/light/set", json=value, headers=headers)
        result = (await response.json())["result"]
        assert result["status"] == outcome
        assert result["possibly_sent"] == (status == "uncertain")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_light_policy_enable_recent_auth_and_duration_allowlist(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {
            "enabled": params["enabled"],
            "policy_revision": 2,
            "on_duration_seconds": params["on_duration_seconds"],
        }

    console.owner.devices_light_policy_set = apply
    headers = await console.enroll()
    value = {
        "profile_id": str(uuid4()),
        "account_generation": 1,
        "device_id": "b" * 64,
        "expected_revision": 1,
        "enabled": True,
        "on_duration_seconds": 300,
        "operation_id": str(uuid4()),
    }
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    console.web.auth.get(token).reauthenticated_at -= 301
    assert (
        await console.call("POST", "/devices/light/policy", json=value, headers=headers)
    ).status == 403
    assert (
        await console.call(
            "POST", "/devices/light/policy", json=value | {"enabled": False}, headers=headers
        )
    ).status == 200
    assert len(calls) == 1
    for change in (
        {"on_duration_seconds": 86400},
        {"on_duration_seconds": True},
        {"on_duration_seconds": 0},
        {"panic_enabled": True},
        {"location_id": "raw"},
    ):
        response = await console.call(
            "POST",
            "/devices/light/policy",
            json=value | {"enabled": False} | change,
            headers=headers,
        )
        assert response.status == 400
