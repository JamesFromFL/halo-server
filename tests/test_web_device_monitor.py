"""TLS/CSRF protection and durable no-replay behavior of monitoring preference."""

from uuid import uuid4

import pytest
from test_web_http import console as console  # noqa: F401


@pytest.mark.asyncio
async def test_monitoring_requires_auth_csrf_and_only_saves_once(console):  # noqa: F811
    calls = []

    async def save(**params):
        calls.append(params)
        return {"enabled": params["enabled"], "policy_revision": 1, "state": "starting"}

    console.owner.devices_monitoring_set = save
    payload = {
        "profile_id": "profile-a",
        "account_generation": 1,
        "expected_revision": 0,
        "enabled": True,
        "operation_id": str(uuid4()),
    }
    assert (await console.call("POST", "/devices/monitoring", json=payload)).status in {401, 403}
    headers = await console.enroll()
    assert (await console.call("POST", "/devices/monitoring", json=payload)).status == 403
    for _ in range(2):
        response = await console.call("POST", "/devices/monitoring", headers=headers, json=payload)
        receipt = (await response.json())["result"]
        assert response.status == 200 and receipt["status"] == "completed"
    assert calls == [{key: value for key, value in payload.items() if key != "operation_id"}]
    for changes in ({"panic_enabled": True}, {"enabled": "yes"}, {"enabled": False}):
        response = await console.call(
            "POST", "/devices/monitoring", headers=headers, json=payload | changes
        )
        assert response.status in {400, 409}
    assert len(calls) == 1
