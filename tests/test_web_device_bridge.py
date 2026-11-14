"""Real HTTPS bridge preferences use owner authentication, CSRF and no replay."""

from uuid import uuid4

import pytest
from test_web_http import console as console  # noqa: F401


def payload():
    return {
        "profile_id": str(uuid4()),
        "account_generation": 1,
        "expected_revision": 0,
        "enabled": True,
        "auto_import": True,
        "included_devices": [],
        "excluded_devices": [],
        "excluded_locations": [],
        "excluded_kinds": [],
        "excluded_mappings": [],
        "operation_id": str(uuid4()),
    }


@pytest.mark.asyncio
async def test_bridge_http_auth_scope_validation_and_no_replay(console):  # noqa: F811
    calls = []

    async def save(**params):
        calls.append(params)
        return {
            "revision": 1,
            "configured": True,
            "enabled": True,
            "publication": "not_implemented",
        }

    console.owner.devices_bridge_set = save
    value = payload()
    assert (await console.call("POST", "/devices/bridge", json=value)).status in {401, 403}
    headers = await console.enroll()
    assert (await console.call("POST", "/devices/bridge", json=value)).status == 403
    for _ in range(2):
        response = await console.call("POST", "/devices/bridge", headers=headers, json=value)
        result = (await response.json())["result"]
        assert response.status == 200 and result["status"] == "completed"
    assert calls == [{key: item for key, item in value.items() if key != "operation_id"}]
    for changes in (
        {"panic_enabled": True},
        {"enabled": "true"},
        {"excluded_mappings": ["siren"]},
        {"account_generation": True},
        {"excluded_devices": ["raw-household-id"]},
        {"excluded_devices": ["a" * 64] * 2},
        {"enabled": False},
    ):
        response = await console.call(
            "POST", "/devices/bridge", headers=headers, json=value | changes
        )
        assert response.status in {400, 409}
    assert len(calls) == 1
