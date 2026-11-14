"""Scoped Chime permission, actual state, explicit sound recovery and no replay."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from test_device_monitor import fixture, preference, record

from halo_server.api.admin import AdminProtocolError
from halo_server.devices.chime import valid_action
from halo_server.devices.chime_policy import ChimePolicyError, ChimePolicyStore
from halo_server.devices.models import DeviceIdentity, DeviceObservation


def chime(sessions):
    return replace(
        record(sessions),
        identity=DeviceIdentity(
            sessions.active_scope.profile_id, "ring-rest", "location", "chimes", "12"
        ),
        family="chimes",
        kind="chime",
        model="chime_pro_v2",
        capabilities=("device-health",),
        capability_evidence=("ring-rest-schema",),
        observations=(DeviceObservation("connected", True, 100),),
    )


def source(row, **changes):
    return {
        "device_id": row.key,
        "location_id": row.identity.location_key,
        "epoch": 1,
        "revision": 4,
        "readback_revision": 1,
        "volume": 5,
        "snooze_seconds_left": 0,
        "automatic_nightlight": False,
        "authorization": "allowed",
        "can_set": True,
        "reason": None,
        "pending": False,
        "outcome_unknown": False,
        "sound_active": False,
        "requested_action": None,
        **changes,
    }


async def ready(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    monitor.bridge.publisher.start = monitor.bridge.publisher.notify
    await monitor.set_enabled(**preference(sessions))
    row = chime(sessions)
    providers[0].options["on_snapshot"](
        records=(row,),
        revision=1,
        complete=True,
        observed_at=100,
        chimes=(source(row),),
    )
    await asyncio.sleep(0)
    return service, client, sessions, monitor, providers[0], row


def policy_request(sessions, monitor, row, **changes):
    return {
        "profile_id": sessions.active_scope.profile_id,
        "account_generation": sessions.active_scope.generation,
        "device_id": row.key,
        "expected_revision": monitor.chime.policy.revision,
        "enabled": True,
        "sound_tests_enabled": False,
        **changes,
    }


def command(sessions, monitor, row, **changes):
    return {
        "profile_id": sessions.active_scope.profile_id,
        "account_generation": sessions.active_scope.generation,
        "device_id": row.key,
        "provider_generation": monitor._binding.generation,
        "expected_epoch": 1,
        "expected_revision": 4,
        "expected_policy_revision": monitor.chime.policy.revision,
        "action": "volume",
        "value": 8,
        "operation_id": str(uuid4()),
        **changes,
    }


def test_private_policy_and_separate_sound_grant(tmp_path):
    store = ChimePolicyStore(tmp_path / "chime.json")
    profile = str(uuid4())
    assert not store.load().preferences and not store.path.exists()
    with pytest.raises(ChimePolicyError):
        store.configure(profile, "a" * 64, False, True, expected_revision=0)
    current = store.configure(profile, "a" * 64, True, False, expected_revision=0)
    assert current.allows(profile, "a" * 64)
    assert not current.preference(profile, "a" * 64).sound_tests_enabled
    assert store.path.stat().st_mode & 0o777 == 0o600
    current = store.configure(profile, "a" * 64, False, False, expected_revision=1)
    assert not current.preferences


@pytest.mark.parametrize(
    "action,value",
    [
        ("volume", True),
        ("volume", 12),
        ("volume", -1),
        ("snooze", 1441),
        ("nightlight", 1),
        ("test", "siren"),
        ("sound_stopped", False),
        ("panic", None),
        ([], {}),
    ],
)
def test_exact_action_shapes(action, value):
    assert not valid_action(action, value)


@pytest.mark.asyncio
async def test_enable_is_inert_and_sound_tests_require_separate_permission(tmp_path):
    service, client, sessions, monitor, provider, row = await ready(tmp_path)
    try:
        assert monitor.snapshot()["devices"][0]["chime"]["volume"] == 5
        assert not monitor.snapshot()["devices"][0]["chime"]["enabled"]
        before = client.submissions
        await service.handle_admin(
            "devices_chime_policy_set", policy_request(sessions, monitor, row)
        )
        assert client.submissions == before
        assert monitor.snapshot()["devices"][0]["chime"]["can_set"]
        result = await monitor.chime.set_chime(
            **command(sessions, monitor, row, action="test", value="ding")
        )
        assert result["status"] == "rejected" and client.submissions == before
        with pytest.raises(AdminProtocolError):
            await service.handle_admin(
                "devices_chime_set", command(sessions, monitor, row) | {"panic": True}
            )
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_owned_operation_no_optimism_replay_or_queue(tmp_path):
    _, client, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def write(device, action, value, **params):
        calls.append((device, action, value))
        assert params["admission"]()
        entered.set()
        await release.wait()
        return {"status": "confirmed", "code": "ok", "possibly_sent": True}

    provider.set_chime = write
    try:
        await monitor.chime.configure(**policy_request(sessions, monitor, row))
        params = command(sessions, monitor, row)
        first = asyncio.create_task(monitor.chime.set_chime(**params))
        await entered.wait()
        duplicate = asyncio.create_task(monitor.chime.set_chime(**params))
        assert monitor.snapshot()["devices"][0]["chime"]["volume"] == 5
        assert monitor.snapshot()["devices"][0]["chime"]["pending"]
        with pytest.raises(AdminProtocolError, match="pending"):
            await monitor.chime.set_chime(**command(sessions, monitor, row))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await duplicate)["status"] == "confirmed"
        assert len(calls) == 1
        assert monitor.snapshot()["devices"][0]["chime"]["volume"] == 5
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_sound_recovery_is_explicit_and_does_not_require_test_grant(tmp_path):
    _, _, sessions, monitor, provider, row = await ready(tmp_path)
    calls = []

    async def write(device, action, value, **params):
        calls.append(action)
        return {"status": "confirmed", "code": "sound-acknowledged", "possibly_sent": False}

    provider.set_chime = write
    try:
        await monitor.chime.configure(**policy_request(sessions, monitor, row))
        monitor.chime.receive(
            monitor._binding,
            (
                source(
                    row,
                    sound_active=True,
                    can_set=False,
                    outcome_unknown=True,
                    reason="sound-active",
                ),
            ),
        )
        assert not monitor.snapshot()["devices"][0]["chime"]["can_set"]
        assert (
            await monitor.chime.set_chime(
                **command(sessions, monitor, row, action="sound_stopped", value=None)
            )
        )["status"] == "confirmed"
        assert calls == ["sound_stopped"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_bad_source_revokes_captured_admission_and_cancel_is_uncertain(tmp_path):
    _, _, sessions, monitor, provider, row = await ready(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(device, action, value, **params):
        entered.set()
        await release.wait()
        assert not params["admission"]()
        raise asyncio.CancelledError

    provider.set_chime = write
    try:
        await monitor.chime.configure(**policy_request(sessions, monitor, row))
        task = asyncio.create_task(monitor.chime.set_chime(**command(sessions, monitor, row)))
        await entered.wait()
        monitor.chime.receive(monitor._binding, (source(row, requested_action=[]),))
        monitor.chime.receive(monitor._binding, (source(row),))
        release.set()
        assert (await task)["status"] == "uncertain"
        assert monitor.snapshot()["devices"][0]["chime"]["outcome_unknown"]
        monitor.chime.receive(monitor._binding, (source(row, revision=5, readback_revision=2),))
        assert not monitor.snapshot()["devices"][0]["chime"]["outcome_unknown"]
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uncertain_action,status,code,sent,cleared",
    [
        ("test", "confirmed", "sound-stop-acknowledged", False, True),
        ("volume", "confirmed", "sound-stop-acknowledged", False, False),
        ("snooze", "confirmed", "sound-stop-acknowledged", False, False),
        ("nightlight", "confirmed", "sound-stop-acknowledged", False, False),
        ("test", "uncertain", "result-unavailable", False, False),
        ("test", "accepted", "sound-stop-acknowledged", False, False),
        ("test", "confirmed", "state-confirmed", False, False),
        ("test", "confirmed", "sound-stop-acknowledged", True, False),
    ],
)
async def test_local_sound_ack_resolves_only_lost_sound_result_without_settings_readback(
    tmp_path, uncertain_action, status, code, sent, cleared
):
    _, _, sessions, monitor, provider, row = await ready(tmp_path)
    key = (monitor._binding.scope, row.key)
    try:
        await monitor.chime.configure(**policy_request(sessions, monitor, row))
        monitor.chime.receive(
            monitor._binding,
            (
                source(
                    row,
                    sound_active=True,
                    can_set=False,
                    outcome_unknown=True,
                    reason="sound-active",
                ),
            ),
        )
        # This is the coordinator fallback after a lost Ring-loop result. The
        # provider's later local acknowledgement does not advance readback.
        monitor.chime._uncertain[key] = (1, uncertain_action, "ding")

        async def acknowledge(_device, action, value, **_params):
            assert action == "sound_stopped" and value is None
            monitor.chime.receive(
                monitor._binding,
                (source(row, revision=5, readback_revision=1, sound_active=False),),
            )
            return {"status": status, "code": code, "possibly_sent": sent}

        provider.set_chime = acknowledge
        await monitor.chime.set_chime(
            **command(sessions, monitor, row, action="sound_stopped", value=None)
        )
        assert (key not in monitor.chime._uncertain) is cleared
        view = monitor.snapshot()["devices"][0]["chime"]
        assert view["outcome_unknown"] is not cleared
        assert view["can_set"] is cleared
    finally:
        await monitor.close()
