"""Real provider normalization through daemon admission, using a synthetic socket."""

import asyncio
from uuid import uuid4

import pytest
from test_alarm_device_commands import Gateway, Transport
from test_device_monitor import fixture, preference

from halo_server.devices.provider import NoncameraProvider


@pytest.mark.asyncio
async def test_normalized_alarm_provider_to_owner_confirmation_and_optout(tmp_path):
    service, client, sessions, monitor, _providers = fixture(tmp_path)
    monitor.bridge.publisher.start = monitor.bridge.publisher.notify
    monitor._factory = lambda auth, **options: NoncameraProvider(
        auth,
        **options,
        gateway=Gateway(),
        transport_factory=Transport,
        clock=monitor._clock,
        command_timeout=0.2,
    )
    try:
        await monitor.set_enabled(**preference(sessions))
        async with asyncio.timeout(2):
            while not monitor.alarm._rows:
                await asyncio.sleep(0)
        row = next(row for row in monitor.snapshot()["devices"] if row["kind"] == "security-panel")
        assert row["alarm"]["mode"] == "disarmed"
        assert row["alarm"]["authorization"] == "allowed"
        assert not row["alarm"]["can_set_mode"]
        policy = {
            "profile_id": sessions.active_scope.profile_id,
            "account_generation": sessions.active_scope.generation,
            "location_id": row["location_id"],
            "expected_revision": 0,
            "enabled": True,
        }
        await service.handle_admin("devices_alarm_policy_set", policy)
        state = next(item for item in monitor.snapshot()["devices"] if item["id"] == row["id"])[
            "alarm"
        ]
        assert state["can_set_mode"] and set(state["allowed_targets"]) == {
            "disarmed",
            "home",
            "away",
        }
        params = {
            "profile_id": sessions.active_scope.profile_id,
            "account_generation": sessions.active_scope.generation,
            "device_id": row["id"],
            "provider_generation": state["provider_generation"],
            "expected_epoch": state["epoch"],
            "expected_revision": state["observed_revision"],
            "expected_policy_revision": state["policy_revision"],
            "target": "away",
            "operation_id": str(uuid4()),
        }
        result = await service.handle_admin("devices_alarm_mode_set", params)
        assert result == {
            "status": "confirmed",
            "code": "observed",
            "possibly_sent": True,
            "mode": "away",
        }
        await asyncio.sleep(0)
        state = next(item for item in monitor.snapshot()["devices"] if item["id"] == row["id"])[
            "alarm"
        ]
        assert state["mode"] == "away" and not state["pending"]
        transport = monitor._binding.provider._runtimes["home"].transport
        assert len(transport.sent) == 1
        await service.handle_admin(
            "devices_alarm_policy_set", policy | {"enabled": False, "expected_revision": 1}
        )
        denied = await service.handle_admin(
            "devices_alarm_mode_set",
            params
            | {
                "expected_policy_revision": 2,
                "expected_revision": state["observed_revision"],
                "operation_id": str(uuid4()),
                "target": "disarmed",
            },
        )
        assert denied["status"] == "rejected" and len(transport.sent) == 1
    finally:
        await monitor.close()
