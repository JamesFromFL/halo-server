"""Chime provider safety/readback contracts with synthetic Ring inventory only."""

import asyncio
import copy

import pytest
from test_ring_shared_auth import Response, fake_auth

from halo_server.devices.provider import NoncameraProvider
from halo_server.ring.hub.rest import DEVICES_ENDPOINT, AlarmRestError, RingAlarmRestGateway
from halo_server.ring.json_transport import RingWriteReceipt

PROFILE = "11111111-1111-4111-8111-111111111111"


def chime(**changes):
    result = {
        "id": 7,
        "location_id": "home",
        "kind": "chime_pro_v2",
        "description": "Hall",
        "owned": True,
        "alerts": {"connection": "online"},
        "settings": {"volume": 5, "night_light_settings": {"light_sensor_enabled": False}},
        "do_not_disturb": {"seconds_left": 0},
    }
    result.update(changes)
    return result


class Gateway:
    def __init__(self, row=None):
        self.devices = {"chimes": [row or chime()]}
        self.error = None
        self.requests = []
        self.closed = False
        self.before_read = None

    async def request_json(self, url, **_kwargs):
        self.requests.append(url)
        if self.before_read:
            await self.before_read(url)
        if self.error:
            raise self.error
        if url == DEVICES_ENDPOINT:
            return copy.deepcopy(self.devices)
        return {"user_locations": [{"location_id": "home", "name": "Home"}]}

    async def close(self):
        self.closed = True


class Writes:
    def __init__(self, gateway):
        self.gateway = gateway
        self.requests = []
        self.sent = asyncio.Event()
        self.gate = None
        self.before_admit = None
        self.apply = True
        self.receipt = RingWriteReceipt("accepted", "http-accepted", 204, True)
        self.closed = False

    async def write(self, action, api_id, value, *, admission, on_admitted, **_kwargs):
        if self.before_admit:
            await self.before_admit()
        assert admission() is True
        on_admitted()
        # The production HTTP middleware rechecks authority after its marker.
        assert admission() is True
        self.requests.append((action, api_id, value))
        self.sent.set()
        if self.gate:
            await self.gate.wait()
        if self.apply and self.receipt.accepted:
            row = self.gateway.devices["chimes"][0]
            if action == "volume":
                row["settings"]["volume"] = value
            elif action == "snooze":
                row["do_not_disturb"]["seconds_left"] = (value or 0) * 60
            elif action == "nightlight":
                row["settings"]["night_light_settings"]["light_sensor_enabled"] = value
        return self.receipt

    async def set_chime_volume(self, *args, **kwargs):
        return await self.write("volume", *args, **kwargs)

    async def test_chime(self, *args, **kwargs):
        return await self.write("test", *args, **kwargs)

    async def snooze_chime(self, *args, **kwargs):
        return await self.write("snooze", *args, **kwargs)

    async def set_chime_nightlight(self, *args, **kwargs):
        return await self.write("nightlight", *args, **kwargs)

    async def close(self):
        self.closed = True


async def make(row=None, **kwargs):
    gateway = Gateway(row)
    batches = []
    provider = NoncameraProvider(
        None,
        profile_id=PROFILE,
        account_generation=1,
        gateway=gateway,
        on_snapshot=lambda **batch: batches.append(batch),
        **kwargs,
    )
    writes = Writes(gateway)
    provider._device_gateway = writes
    await provider.refresh()
    return provider, gateway, writes, batches


def source(provider):
    return provider._chime_commands.snapshots()[0]


async def command(provider, action="volume", value=8, *, row=None, admission=lambda: True):
    row = row or source(provider)
    return await provider.set_chime(
        row["device_id"],
        action,
        value,
        expected_revision=row["revision"],
        expected_epoch=row["epoch"],
        admission=admission,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,value",
    [
        ("volume", 0),
        ("volume", 11),
        ("snooze", 1440),
        ("snooze", 0),
        ("nightlight", True),
        ("nightlight", False),
    ],
)
async def test_settings_confirm_only_matching_post_send_full_inventory(action, value):
    provider, gateway, writes, batches = await make()
    try:
        before = source(provider)
        result = await command(provider, action, value)
        assert result == {"status": "confirmed", "code": "state-confirmed", "possibly_sent": True}
        assert writes.requests == [(action, 7, None if action == "snooze" and not value else value)]
        after = source(provider)
        assert after["readback_revision"] > before["readback_revision"]
        assert after["revision"] > before["revision"] and after["epoch"] == before["epoch"]
        assert any(batch["chimes"][0]["pending"] for batch in batches)
        assert after["can_set"] and not after["pending"]
        observations = {item.name: item for item in provider.snapshot()[0].observations}
        assert observations["connected"].value is True
        assert observations["volume"].status == "current"
        assert len(gateway.requests) == 4
    finally:
        await provider.close()
    assert writes.closed and gateway.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row,reason",
    [
        (chime(owned=False), "authorization-unknown"),
        (chime(owned=1), "authorization-unknown"),
        (chime(owned=None), "authorization-unknown"),
        (chime(kind="chime_pro_v3"), "chime-unsupported"),
        (chime(id="7"), "chime-unsupported"),
        (chime(alerts={"connection": "offline"}), "chime-offline"),
        (chime(alerts={}, online=True), "chime-offline"),
    ],
)
async def test_unqualified_owner_model_or_online_evidence_never_writes(row, reason):
    provider, _, writes, _ = await make(row)
    try:
        result = await command(provider)
        assert result["status"] == "rejected" and result["code"] == reason
        assert not writes.requests and not source(provider)["can_set"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_unknown_values_are_not_coerced_and_nightlight_requires_explicit_support():
    provider, _, writes, _ = await make(
        chime(settings={"volume": True}, do_not_disturb={"seconds_left": 0.0})
    )
    try:
        row = source(provider)
        assert row["volume"] is row["snooze_seconds_left"] is row["automatic_nightlight"] is None
        result = await command(provider, "nightlight", True)
        assert result["code"] == "nightlight-unsupported" and not writes.requests
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,value",
    [
        ("volume", True),
        ("volume", 12),
        ("snooze", -1),
        ("snooze", 1441),
        ("nightlight", 1),
        ("test", "siren"),
        ("reboot", None),
        ("tone", 42),
        ("sound_stopped", True),
        ([], None),
    ],
)
async def test_invalid_actions_are_rejected_before_handoff(action, value):
    provider, _, writes, _ = await make()
    try:
        assert (await command(provider, action, value))["code"] == "invalid-request"
        assert not writes.requests
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_stale_inventory_and_changed_identity_tokens_fail_closed():
    now = [10.0]
    provider, _, writes, _ = await make(monotonic_clock=lambda: now[0])
    try:
        old = source(provider)
        now[0] += 31
        assert (await command(provider))["code"] == "chime-stale"
        provider._chime_commands.invalidate()
        await provider.refresh()
        assert (await command(provider, row=old))["code"] == "state-changed"
        assert not writes.requests
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_queued_auth_admission_rechecks_account_policy_and_snapshot_revision():
    provider, gateway, writes, _ = await make()
    permit = [True]
    try:

        async def revoke():
            permit[0] = False

        writes.before_admit = revoke
        result = await command(provider, admission=lambda: permit[0])
        assert result["status"] == "rejected" and not result["possibly_sent"]
        assert not writes.requests
        permit[0] = True

        async def change():
            gateway.devices["chimes"][0]["settings"]["volume"] = 3
            await provider.refresh()

        writes.before_admit = change
        result = await command(provider)
        assert result["status"] == "rejected" and not writes.requests
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_same_device_has_no_queue_and_readback_is_not_optimistic():
    provider, _, writes, batches = await make()
    writes.gate = asyncio.Event()
    try:
        first = asyncio.create_task(command(provider))
        await writes.sent.wait()
        assert (await command(provider))["status"] == "busy"
        assert source(provider)["volume"] == 5
        assert batches[-1]["chimes"][0]["volume"] == 5
        writes.gate.set()
        assert (await first)["status"] == "confirmed"
        assert len(writes.requests) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", [1, 30, 301, None, True])
async def test_unrelated_or_invalid_existing_snooze_never_confirms_requested_five_minutes(actual):
    provider, gateway, writes, _ = await make()
    gateway.devices["chimes"][0]["do_not_disturb"]["seconds_left"] = actual
    writes.apply = False
    try:
        result = await command(provider, "snooze", 5)
        assert result["status"] == "uncertain"
        assert source(provider)["outcome_unknown"]
        assert (await command(provider))["code"] == "outcome-unknown"
        gateway.devices["chimes"][0]["do_not_disturb"]["seconds_left"] = 0
        await provider.refresh()
        assert not source(provider)["outcome_unknown"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_clear_snooze_requires_fresh_explicit_zero():
    provider, gateway, writes, _ = await make()
    gateway.devices["chimes"][0]["do_not_disturb"] = {}
    writes.apply = False
    try:
        assert (await command(provider, "snooze", 0))["status"] == "uncertain"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_sound_acceptance_is_not_playback_confirmation_and_requires_owner_ack():
    provider, _, writes, _ = await make()
    try:
        assert (await command(provider, "test", "ding"))["status"] == "accepted"
        assert source(provider)["sound_active"] and not source(provider)["can_set"]
        await provider.refresh()
        assert source(provider)["sound_active"]
        assert (await command(provider, "test", "motion"))["code"] == "sound-active"
        assert (await command(provider))["code"] == "sound-active"
        assert (await command(provider, "sound_stopped", None))["code"] == "sound-stop-acknowledged"
        assert not source(provider)["sound_active"] and source(provider)["can_set"]
        assert len(writes.requests) == 1
        assert (await command(provider, "test", "motion"))["status"] == "accepted"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_uncertain_sound_holds_latch_even_after_new_inventory_until_acknowledged():
    provider, _, writes, _ = await make()
    writes.receipt = RingWriteReceipt("uncertain", "timeout", None, True)
    try:
        assert (await command(provider, "test", "ding"))["status"] == "uncertain"
        await provider.refresh()
        assert source(provider)["outcome_unknown"] and source(provider)["sound_active"]
        assert (await command(provider, "sound_stopped", None))["status"] == "confirmed"
        assert not source(provider)["outcome_unknown"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_explicit_http_rejection_does_not_leave_sound_latch_or_retry():
    provider, _, writes, _ = await make()
    writes.receipt = RingWriteReceipt("rejected", "permission-denied", 403, True)
    try:
        result = await command(provider, "test", "ding")
        assert result["status"] == "rejected" and len(writes.requests) == 1
        assert not source(provider)["sound_active"] and not source(provider)["outcome_unknown"]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("sound", [False, True])
async def test_post_send_cancellation_preserves_uncertainty_and_provider_close_settles(sound):
    provider, _, writes, _ = await make()
    writes.gate = asyncio.Event()
    task = asyncio.create_task(
        command(provider, "test" if sound else "volume", "ding" if sound else 8)
    )
    await writes.sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source(provider)["outcome_unknown"] and source(provider)["sound_active"] is sound
    assert not source(provider)["pending"]
    await provider.close()
    assert writes.closed


@pytest.mark.asyncio
async def test_ten_second_total_contract_is_bounded_even_when_readback_hangs():
    provider, gateway, writes, _ = await make(command_timeout=0.03)
    gate = asyncio.Event()

    async def block(_url):
        await gate.wait()

    gateway.before_read = block
    try:
        async with asyncio.timeout(0.5):
            result = await command(provider)
        assert result["status"] == "uncertain" and len(writes.requests) == 1
        assert source(provider)["outcome_unknown"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_missing_or_unknown_model_after_send_cannot_confirm_same_value():
    provider, gateway, writes, _ = await make()

    async def changed():
        gateway.devices["chimes"][0]["kind"] = "future"

    writes.before_admit = changed
    try:
        result = await command(provider)
        assert result["status"] == "uncertain"
        assert source(provider)["reason"] == "chime-unsupported"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_rate_limit_retry_after_prevents_command_readback_and_background_hammering():
    now = [10.0]
    provider, gateway, _, _ = await make(monotonic_clock=lambda: now[0])
    try:
        gateway.error = AlarmRestError("rate-limited", retry_after=120)
        await provider.refresh()
        calls = len(gateway.requests)
        assert not source(provider)["can_set"]
        gateway.error = None
        now[0] += 119
        await provider.refresh()
        assert len(gateway.requests) == calls
        now[0] += 1
        await provider.refresh()
        assert len(gateway.requests) == calls + 2
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_gateway_reuses_real_alarm_core_and_never_closes_sdk_auth_session():
    auth, _, _ = fake_auth(Response())
    alarm = RingAlarmRestGateway(auth)
    provider = NoncameraProvider(auth, profile_id=PROFILE, account_generation=1, gateway=alarm)
    feature = provider._get_device_gateway()
    assert feature._transport is alarm.shared_transport
    assert provider._get_device_gateway() is feature
    await provider.close()
    assert feature._closed and alarm.shared_transport._closed


@pytest.mark.asyncio
async def test_readonly_discovery_gateway_without_shared_core_cannot_fork_transport():
    provider, _, _, _ = await make()
    provider._device_gateway = None
    try:
        assert (await command(provider))["code"] == "transport-unavailable"
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("kind", "chime_v2"), ("device_id", "new-hardware")])
async def test_replacement_model_or_hardware_fences_in_flight_write(field, value):
    provider, gateway, writes, _ = await make()
    writes.gate = asyncio.Event()
    try:
        old = source(provider)
        task = asyncio.create_task(command(provider))
        await writes.sent.wait()
        gateway.devices["chimes"][0][field] = value
        await provider.refresh()
        assert source(provider)["epoch"] != old["epoch"]
        writes.gate.set()
        assert (await task)["status"] == "uncertain"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_incomplete_readback_never_advances_generic_recovery_marker():
    provider, gateway, _, _ = await make()
    try:
        before = source(provider)["readback_revision"]
        del gateway.devices["chimes"][0]["settings"]["night_light_settings"]
        await provider.refresh()
        assert source(provider)["readback_revision"] == before
        assert source(provider)["automatic_nightlight"] is None
        gateway.devices["chimes"][0]["settings"]["night_light_settings"] = {
            "light_sensor_enabled": True
        }
        await provider.refresh()
        assert source(provider)["readback_revision"] > before
        before = source(provider)["readback_revision"]
        gateway.devices["chimes"][0]["settings"]["volume"] = True
        await provider.refresh()
        assert source(provider)["readback_revision"] == before
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_write_retry_after_preserved_up_to_one_hour_blocks_followup_commands():
    now = [10.0]
    provider, gateway, writes, _ = await make(monotonic_clock=lambda: now[0])
    writes.receipt = RingWriteReceipt("rejected", "rate-limited", 429, True, 3600)
    try:
        assert (await command(provider))["code"] == "rate-limited"
        calls = len(gateway.requests)
        now[0] += 3599
        await provider.refresh()
        assert len(gateway.requests) == calls
        assert (await command(provider))["code"] == "rate-limited"
        now[0] += 1
        await provider.refresh()
        assert len(gateway.requests) == calls + 2
        assert source(provider)["can_set"]
    finally:
        await provider.close()
