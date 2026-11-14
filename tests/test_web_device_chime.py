"""HTTPS Chime scopes, explicit sound authorization, and durable no-replay receipts."""

from uuid import uuid4

import pytest
from test_web_http import console as console  # noqa: F401

from halo_server.web.auth import COOKIE


def payload(**changes):
    return {
        "profile_id": str(uuid4()),
        "account_generation": 1,
        "device_id": "a" * 64,
        "provider_generation": 1,
        "expected_epoch": 2,
        "expected_revision": 3,
        "expected_policy_revision": 1,
        "action": "volume",
        "value": 5,
        "operation_id": str(uuid4()),
        **changes,
    }


@pytest.mark.asyncio
async def test_chime_auth_csrf_and_separate_sound_confirmation(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {"status": "confirmed", "code": "ok", "possibly_sent": True}

    console.owner.devices_chime_set = apply
    value = payload()
    path = "/devices/chime/set"
    assert (await console.call("POST", path, json=value)).status in {401, 403}
    headers = await console.enroll()
    assert (await console.call("POST", path, json=value)).status == 403
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    session = console.web.auth.get(token)
    session.reauthenticated_at -= 301
    for _ in range(2):
        assert (await console.call("POST", path, json=value, headers=headers)).status == 200
    assert calls == [value]
    sound = payload(action="test", value="ding")
    assert (await console.call("POST", path, json=sound, headers=headers)).status == 403
    assert calls == [value]
    session.reauthenticated_at += 301
    assert (await console.call("POST", path, json=sound, headers=headers)).status == 200
    assert calls == [value, sound]
    for change in (
        {"action": "siren"},
        {"value": True},
        {"value": 12},
        {"action": "snooze", "value": 1441},
        {"action": "nightlight", "value": 1},
        {"action": "sound_stopped", "value": True},
        {"device_id": "raw"},
        {"expected_epoch": True},
        {"panic": True},
        {"action": []},
    ):
        assert (
            await console.call("POST", path, json=value | change, headers=headers)
        ).status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome",
    [("uncertain", "unknown"), ("rejected", "rejected"), ("accepted", "completed")],
)
async def test_chime_result_is_recorded_and_never_replayed(console, status, outcome):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {
            "status": status,
            "code": "http-accepted" if status == "accepted" else "outcome-unknown",
            "possibly_sent": status != "rejected",
        }

    console.owner.devices_chime_set = apply
    headers = await console.enroll()
    value = payload(action="test", value="motion")
    for _ in range(2):
        response = await console.call("POST", "/devices/chime/set", json=value, headers=headers)
        result = (await response.json())["result"]
        assert result["status"] == outcome
        assert result["result"]["status"] == status
    assert calls == [value]


@pytest.mark.asyncio
async def test_chime_enable_and_sound_grant_require_reauthentication(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {
            "enabled": params["enabled"],
            "sound_tests_enabled": params["sound_tests_enabled"],
            "policy_revision": 2,
        }

    console.owner.devices_chime_policy_set = apply
    headers = await console.enroll()
    value = {
        "profile_id": str(uuid4()),
        "account_generation": 1,
        "device_id": "a" * 64,
        "expected_revision": 1,
        "enabled": True,
        "sound_tests_enabled": True,
        "operation_id": str(uuid4()),
    }
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    console.web.auth.get(token).reauthenticated_at -= 301
    path = "/devices/chime/policy"
    assert (await console.call("POST", path, json=value, headers=headers)).status == 403
    assert (
        await console.call("POST", path, json=value | {"enabled": False}, headers=headers)
    ).status == 400
    disabled = value | {"enabled": False, "sound_tests_enabled": False}
    assert (await console.call("POST", path, json=disabled, headers=headers)).status == 200
    for change in ({"sound_tests_enabled": 1}, {"enabled": "false"}, {"siren_enabled": True}):
        assert (
            await console.call("POST", path, json=disabled | change, headers=headers)
        ).status == 400
    assert len(calls) == 1
