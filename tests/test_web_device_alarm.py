"""Alarm HTTPS control authorization, exact inputs and durable no-replay outcomes."""

from uuid import uuid4

import pytest
from test_web_http import console as console  # noqa: F401

from halo_server.web.auth import COOKIE


def mode_payload():
    return {
        "profile_id": str(uuid4()),
        "account_generation": 1,
        "device_id": "a" * 64,
        "provider_generation": 1,
        "expected_epoch": 2,
        "expected_revision": 3,
        "expected_policy_revision": 1,
        "target": "home",
        "operation_id": str(uuid4()),
    }


@pytest.mark.asyncio
async def test_alarm_mode_auth_and_recent_confirmation(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {"status": "confirmed", "code": None, "mode": "home", "possibly_sent": True}

    console.owner.devices_alarm_mode_set = apply
    value = mode_payload()
    path = "/devices/alarm/mode"
    assert (await console.call("POST", path, json=value)).status in {401, 403}
    headers = await console.enroll()
    assert (await console.call("POST", path, json=value)).status == 403
    for _ in range(2):
        response = await console.call("POST", path, json=value, headers=headers)
        assert response.status == 200
        assert (await response.json())["result"]["status"] == "completed"
    assert calls == [value]
    for change in (
        {"target": "night"},
        {"target": "panic"},
        {"bypass": []},
        {"expected_epoch": True},
        {"device_id": "raw-id"},
        {"target": []},
    ):
        response = await console.call("POST", path, json=value | change, headers=headers)
        assert response.status == 400
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    console.web.auth.get(token).reauthenticated_at -= 301
    response = await console.call("POST", path, json=mode_payload(), headers=headers)
    assert response.status == 403 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome", [("uncertain", "unknown"), ("rejected", "rejected"), ("busy", "rejected")]
)
async def test_alarm_uncertain_and_rejected_are_not_success_or_replayed(console, status, outcome):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {
            "status": status,
            "code": "outcome-unknown",
            "mode": None,
            "possibly_sent": status == "uncertain",
        }

    console.owner.devices_alarm_mode_set = apply
    value = mode_payload()
    headers = await console.enroll()
    for _ in range(2):
        response = await console.call("POST", "/devices/alarm/mode", json=value, headers=headers)
        result = (await response.json())["result"]
        assert result["status"] == outcome
        assert result["possibly_sent"] == (status == "uncertain")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_alarm_optin_recent_auth_disable_remains_available(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {"enabled": params["enabled"], "policy_revision": 2}

    console.owner.devices_alarm_policy_set = apply
    headers = await console.enroll()
    value = {
        "profile_id": str(uuid4()),
        "account_generation": 1,
        "location_id": "b" * 64,
        "expected_revision": 1,
        "enabled": True,
        "operation_id": str(uuid4()),
    }
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    console.web.auth.get(token).reauthenticated_at -= 301
    assert (
        await console.call("POST", "/devices/alarm/policy", json=value, headers=headers)
    ).status == 403
    response = await console.call(
        "POST", "/devices/alarm/policy", json=value | {"enabled": False}, headers=headers
    )
    assert response.status == 200 and len(calls) == 1
    for change in ({"panic_enabled": True}, {"location_id": "raw"}, {"expected_revision": True}):
        response = await console.call(
            "POST",
            "/devices/alarm/policy",
            json=value | {"enabled": False} | change,
            headers=headers,
        )
        assert response.status == 400
