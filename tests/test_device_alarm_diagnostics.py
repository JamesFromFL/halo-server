"""Owner-only blocker explanations, with no physical alarm commands."""

import pytest
from test_device_alarm import alarm_source, policy_request, ready


@pytest.mark.asyncio
async def test_target_reasons_are_optional_owned_and_withheld_when_stale(tmp_path):
    _service, _client, sessions, monitor, provider, row = await ready(tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail("Reading blocker diagnostics must never send an alarm command")

    provider.set_alarm_mode = forbidden
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        # An older provider with no new field is still valid.
        assert monitor.snapshot()["devices"][0]["alarm"]["target_reasons"] == {}
        reasons = {"home": "sensors-require-bypass", "away": "faulted-device-list-unknown"}
        monitor.alarm.receive(
            monitor._binding,
            (alarm_source(row, allowed_targets=["disarmed"], target_reasons=reasons),),
        )
        state = monitor.snapshot()["devices"][0]["alarm"]
        assert state["can_set_mode"] and state["allowed_targets"] == ["disarmed"]
        assert state["reason"] is None
        assert state["target_reasons"] == reasons
        reasons["home"] = "untrusted-provider-mutation"
        state["target_reasons"]["away"] = "untrusted-consumer-mutation"
        assert monitor.snapshot()["devices"][0]["alarm"]["target_reasons"] == {
            "home": "sensors-require-bypass",
            "away": "faulted-device-list-unknown",
        }
        monitor._clock = lambda: 1000
        stale = monitor.snapshot()["devices"][0]["alarm"]
        assert stale["target_reasons"] == {} and not stale["can_set_mode"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reasons",
    [
        None,
        [],
        {"panic": "command-pending"},
        {"home": "provider supplied private text"},
        {"home": "constructor"},
        {"home": "unknown-reason"},
        {"home": None},
        {"home": []},
        {"disarmed": "command-pending"},
    ],
)
async def test_invalid_or_contradictory_target_reasons_cannot_enter_owner_projection(
    tmp_path, reasons
):
    _service, _client, sessions, monitor, _provider, row = await ready(tmp_path)
    try:
        await monitor.alarm.configure(**policy_request(sessions, monitor, row))
        monitor.alarm.receive(
            monitor._binding,
            (alarm_source(row, allowed_targets=["disarmed"], target_reasons=reasons),),
        )
        state = monitor.snapshot()["devices"][0]["alarm"]
        assert not state["can_set_mode"] and state["target_reasons"] == {}
    finally:
        await monitor.close()
