"""Actual provider normalization, daemon policy and owner readback on a fake socket."""

import asyncio
from uuid import uuid4

import pytest
from test_device_monitor import fixture, preference
from test_light_device_commands import Gateway, Transport

from halo_server.devices.provider import NoncameraProvider


@pytest.mark.asyncio
async def test_provider_daemon_owner_light_confirmation_and_optout(tmp_path):
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
            while not monitor.light._rows:
                await asyncio.sleep(0)
        row = next(row for row in monitor.snapshot()["devices"] if row["kind"] == "beams-light")
        assert row["light"]["on"] is False
        assert row["light"]["authorization"] == "allowed"
        assert not row["light"]["can_set"]
        policy = {
            "profile_id": sessions.active_scope.profile_id,
            "account_generation": sessions.active_scope.generation,
            "device_id": row["id"],
            "expected_revision": 0,
            "enabled": True,
            "on_duration_seconds": 300,
        }
        await service.handle_admin("devices_light_policy_set", policy)
        state = next(item for item in monitor.snapshot()["devices"] if item["id"] == row["id"])[
            "light"
        ]
        assert state["can_set"] and state["on_duration_seconds"] == 300
        params = {
            "profile_id": sessions.active_scope.profile_id,
            "account_generation": sessions.active_scope.generation,
            "device_id": row["id"],
            "provider_generation": state["provider_generation"],
            "expected_epoch": state["epoch"],
            "expected_revision": state["observed_revision"],
            "expected_policy_revision": state["policy_revision"],
            "target": True,
            "operation_id": str(uuid4()),
        }
        result = await service.handle_admin("devices_light_set", params)
        assert result == {
            "status": "confirmed",
            "code": "observed",
            "possibly_sent": True,
            "on": True,
        }
        await asyncio.sleep(0)
        state = next(item for item in monitor.snapshot()["devices"] if item["id"] == row["id"])[
            "light"
        ]
        assert state["on"] is True and not state["pending"]
        transport = monitor._binding.provider._runtimes["home"].transport
        assert len(transport.sent) == 1 and transport.sent[0][3] == 300
        await service.handle_admin(
            "devices_light_policy_set", policy | {"enabled": False, "expected_revision": 1}
        )
        denied = await service.handle_admin(
            "devices_light_set",
            params
            | {
                "expected_policy_revision": 2,
                "expected_revision": state["observed_revision"],
                "operation_id": str(uuid4()),
                "target": False,
            },
        )
        assert denied["status"] == "rejected" and len(transport.sent) == 1
    finally:
        await monitor.close()
