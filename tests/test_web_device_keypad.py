"""Authenticated keypad controls are distinct from local brightness-lock policy."""

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
        "target": 45,
        "operation_id": str(uuid4()),
        **changes,
    }


@pytest.mark.asyncio
async def test_keypad_brightness_exact_boundaries_auth_and_no_replay(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {
            "status": "confirmed",
            "code": None,
            "brightness": params["target"],
            "possibly_sent": True,
        }

    console.owner.devices_keypad_brightness_set = apply
    value = payload()
    path = "/devices/keypad/brightness"
    assert (await console.call("POST", path, json=value)).status in {401, 403}
    headers = await console.enroll()
    assert (await console.call("POST", path, json=value)).status == 403
    for _ in range(2):
        response = await console.call("POST", path, json=value, headers=headers)
        assert response.status == 200
        assert (await response.json())["result"]["status"] == "completed"
    assert calls == [value]
    for change in (
        {"target": True},
        {"target": -1},
        {"target": 101},
        {"target": 1.2},
        {"target": "45"},
        {"brightness": 50},
        {"mode": "away"},
        {"device_id": "raw"},
        {"expected_epoch": True},
    ):
        assert (
            await console.call("POST", path, json=value | change, headers=headers)
        ).status == 400


@pytest.mark.asyncio
async def test_keypad_owner_grant_requires_recent_password_but_local_lock_does_not(console):  # noqa: F811
    policies = []
    locks = []

    async def configure(**params):
        policies.append(params)
        return {"enabled": params["enabled"], "locked": params["locked"], "policy_revision": 2}

    async def lock(**params):
        locks.append(params)
        return {"locked": params["locked"], "policy_revision": 3}

    console.owner.devices_keypad_policy_set = configure
    console.owner.devices_keypad_lock_set = lock
    headers = await console.enroll()
    token = console.client.cookie_jar.filter_cookies(console.server.make_url("/"))[COOKIE].value
    console.web.auth.get(token).reauthenticated_at -= 301
    value = {
        k: v
        for k, v in payload().items()
        if k in {"profile_id", "account_generation", "device_id", "operation_id"}
    }
    policy = value | {"expected_revision": 1, "enabled": True, "locked": True}
    assert (
        await console.call("POST", "/devices/keypad/policy", json=policy, headers=headers)
    ).status == 403
    assert (
        await console.call(
            "POST", "/devices/keypad/policy", json=policy | {"enabled": False}, headers=headers
        )
    ).status == 200
    locking = value | {"expected_policy_revision": 2, "locked": True, "operation_id": str(uuid4())}
    for _ in range(2):
        assert (
            await console.call("POST", "/devices/keypad/lock", json=locking, headers=headers)
        ).status == 200
    assert len(locks) == 1 and len(policies) == 1
    for change in (
        {"locked": 1},
        {"brightness": 50},
        {"enabled": True},
        {"expected_policy_revision": True},
    ):
        assert (
            await console.call(
                "POST", "/devices/keypad/lock", json=locking | change, headers=headers
            )
        ).status == 400


@pytest.mark.asyncio
async def test_keypad_uncertain_and_locked_results_are_not_success_or_replayed(console):  # noqa: F811
    calls = []

    async def apply(**params):
        calls.append(params)
        return {
            "status": "rejected",
            "code": "brightness-locked",
            "brightness": 40,
            "possibly_sent": False,
        }

    console.owner.devices_keypad_brightness_set = apply
    headers = await console.enroll()
    value = payload()
    for _ in range(2):
        result = await (
            await console.call("POST", "/devices/keypad/brightness", json=value, headers=headers)
        ).json()
        assert result["result"]["status"] == "rejected"
        assert not result["result"]["possibly_sent"]
    assert calls == [value]
