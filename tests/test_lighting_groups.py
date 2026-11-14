"""Lighting-group fixtures only; never connects to Ring or actuates hardware."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from halo_server.devices.group_commands import GroupCommands
from halo_server.devices.groups import GroupSchemaError, parse_groups, parse_report
from halo_server.ring.json_transport import RingJsonError, RingWriteReceipt
from halo_server.ring.lighting import LightingReservation

PROFILE = "11111111-1111-4111-8111-111111111111"


def inventory(group="garden", *, location="home", members=None):
    return {
        "device_groups": [
            {
                "device_group_id": group,
                "location_id": location,
                "name": "Garden",
                "devices": members
                if members is not None
                else [
                    {
                        "doorbot_id": 7,
                        "location_id": location,
                        "type": "floodlight_v2",
                        "name": "Floodlight",
                    }
                ],
            }
        ]
    }


def report(on=False, *, member=False):
    return {
        "device_group_id": "garden",
        "location_id": "home",
        "lights_on": on,
        "devices": [{"id": "7", "lights_on": member}],
    }


class Gateway:
    def __init__(self):
        self.inventory = inventory()
        self.report = report()
        self.calls = []
        self.behavior = "confirm"
        self.before_write = None
        self.read_entered = asyncio.Event()
        self.read_release = asyncio.Event()
        self.write_entered = asyncio.Event()
        self.write_release = asyncio.Event()
        self.block_read = False
        self.post_reads = 0

    async def read_light_groups(self, location, **kwargs):
        if isinstance(self.inventory, Exception):
            raise self.inventory
        return copy.deepcopy(self.inventory)

    async def read_light_group_report(self, location, group, **kwargs):
        frozen = copy.deepcopy(self.report)
        if self.block_read:
            self.read_entered.set()
            await self.read_release.wait()
        if isinstance(frozen, Exception):
            raise frozen
        if self.calls:
            self.post_reads += 1
        return frozen

    async def set_light_group(
        self, location, group, on, *, admission, on_admitted, duration_seconds, **kwargs
    ):
        if self.before_write:
            await self.before_write()
        assert admission() is True
        on_admitted()
        self.calls.append((location, group, on, duration_seconds))
        self.write_entered.set()
        if self.behavior == "blocked":
            await self.write_release.wait()
        if self.behavior == "denied":
            return RingWriteReceipt("rejected", "permission-denied", 403, True)
        if self.behavior == "limited":
            return RingWriteReceipt("rejected", "rate-limited", 429, True, 120)
        if self.behavior == "unknown":
            raise RingJsonError("network-unavailable", possibly_sent=True)
        if self.behavior == "confirm":
            self.report["lights_on"] = on
        return RingWriteReceipt("accepted", "accepted", 204, True)


async def setup(*, timeout=0.2, ownership=True):
    clock = [100.0]
    provider = SimpleNamespace(
        profile_id=PROFILE,
        account_generation=2,
        provider_generation=3,
        _active=lambda: True,
        _rest_valid=True,
        _clock=lambda: clock[0],
        _monotonic=lambda: clock[0],
    )

    async def publish():
        pass

    provider._publish = publish
    gateway = Gateway()
    reservation = LightingReservation()
    groups = GroupCommands(provider, gateway, reservation, timeout=timeout)
    owners = {("home", "7", "floodlight_v2"): ownership}
    await groups.refresh({"home": "Home"}, owners)
    return groups, gateway, provider, reservation, clock, owners


def args(groups, *, target=True):
    row = groups.snapshots()[0]
    return dict(
        device_id=row["device_id"],
        target=target,
        expected_revision=row["revision"],
        expected_epoch=row["epoch"],
        duration_seconds=60 if target else None,
        admission=lambda: True,
    )


def test_aggregate_never_fills_or_overrides_members():
    group = parse_groups(inventory(), "home")[0]
    observed = parse_report(report(True, member=False), group)
    assert observed.on is True and observed.members == (("7", False),)
    observed = parse_report(report(False, member=None), group)
    assert observed.on is False and observed.members == (("7", None),)


@pytest.mark.parametrize(
    "patch",
    [
        {"partial": True},
        {"has_more": 1},
        {"next_page": "token"},
        {"count": 2},
        {"pagination": {"next": "token"}},
        {"limit": 100},
        {"complete": 1},
        {"meta": {"total": True}},
        {"location_id": "elsewhere"},
    ],
)
def test_incomplete_inventory_cannot_authorize(patch):
    with pytest.raises(GroupSchemaError):
        parse_groups(inventory() | patch, "home")


@pytest.mark.parametrize("identity", ["", ".", "..", "a/b", "a%2fb", "a\\b", "a\u202eb", None, 7])
def test_invalid_group_identity_rejected(identity):
    with pytest.raises(GroupSchemaError):
        parse_groups(inventory(identity), "home")


@pytest.mark.parametrize("member_id", [True, "7", 0, -1, 2**63])
def test_list_member_api_id_requires_integer(member_id):
    payload = inventory()
    payload["device_groups"][0]["devices"][0]["doorbot_id"] = member_id
    with pytest.raises(GroupSchemaError):
        parse_groups(payload, "home")


@pytest.mark.parametrize("member_id", [7, True, "07", "0", "-1", "７", str(2**63)])
def test_report_member_api_id_requires_canonical_decimal(member_id):
    payload = report()
    payload["devices"][0]["id"] = member_id
    with pytest.raises(GroupSchemaError):
        parse_report(payload, parse_groups(inventory(), "home")[0])


def test_duplicate_or_changed_membership_is_not_a_report():
    group = parse_groups(inventory(), "home")[0]
    for members in ([], [{"id": "8"}], [{"id": "7"}, {"id": "7"}]):
        with pytest.raises(GroupSchemaError):
            parse_report(report() | {"devices": members}, group)


@pytest.mark.asyncio
async def test_group_identity_and_truthful_off_with_member_still_on():
    groups, gateway, _, reservation, _, _ = await setup()
    gateway.report = report(True, member=True)
    await groups.read_report(groups.snapshots()[0]["device_id"])
    result = await groups.request(**args(groups, target=False))
    assert result == {"status": "confirmed", "code": "observed", "possibly_sent": True, "on": False}
    assert gateway.calls == [("home", "garden", False, None)]
    row = groups.records()[0]
    assert row.kind == "light-group" and row.identity.provider == "ring-light-group"
    assert row.observations[0].value is False
    assert groups.details(row.key)["members"][0]["on"] is True
    token = reservation.try_acquire()
    assert token is not None
    reservation.release(token)
    await groups.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", [False, None, "owner", 1])
async def test_group_ownership_requires_exact_literal_proof(ownership):
    groups, gateway, _, _, _, _ = await setup(ownership=ownership)
    assert groups.snapshots()[0]["authorization"] == "unknown"
    assert (await groups.request(**args(groups)))["status"] == "rejected"
    assert not gateway.calls
    await groups.close()


@pytest.mark.asyncio
async def test_empty_mixed_or_mismatched_members_are_read_only():
    groups, gateway, _, _, _, owners = await setup()
    for members in (
        [],
        [{"doorbot_id": 7, "type": "other", "location_id": "home"}],
        [
            {"doorbot_id": 7, "type": "floodlight_v2", "location_id": "home"},
            {"doorbot_id": 8, "type": "floodlight_v2", "location_id": "home"},
        ],
    ):
        gateway.inventory = inventory(members=members)
        await groups.refresh({"home": "Home"}, owners)
        assert groups.snapshots()[0]["authorization"] == "unknown"
    assert not gateway.calls
    await groups.close()


@pytest.mark.asyncio
async def test_membership_change_and_reappearance_revoke_old_intent():
    groups, gateway, _, _, _, owners = await setup()
    old = args(groups)
    gateway.inventory = {"device_groups": []}
    await groups.refresh({"home": "Home"}, owners)
    assert groups.records() == ()
    gateway.inventory = inventory()
    await groups.refresh({"home": "Home"}, owners)
    assert groups.snapshots()[0]["epoch"] != old["expected_epoch"]
    assert (await groups.request(**old))["status"] == "rejected"
    await groups.close()


@pytest.mark.asyncio
async def test_incomplete_refresh_retains_identity_but_revokes_state_and_commands():
    groups, gateway, _, _, _, owners = await setup()
    old = groups.records()[0].key
    gateway.inventory = inventory() | {"partial": True}
    await groups.refresh({"home": "Home"}, owners)
    assert groups.records()[0].key == old and not groups.complete
    assert groups.snapshots()[0]["on"] is None
    assert (await groups.request(**args(groups)))["status"] == "rejected"
    assert not gateway.calls
    await groups.close()


@pytest.mark.asyncio
async def test_stale_or_unknown_group_report_never_becomes_false():
    groups, gateway, _, _, clock, _ = await setup()
    clock[0] += 121
    assert groups.snapshots()[0]["on"] is None
    assert groups.records()[0].observations[0].status == "unknown"
    gateway.report = report(None)
    await groups.read_report(groups.snapshots()[0]["device_id"])
    assert groups.snapshots()[0]["on"] is None
    assert not groups.snapshots()[0]["can_set"]
    await groups.close()


@pytest.mark.asyncio
async def test_shared_reservation_rejects_without_queue_or_write():
    groups, gateway, _, reservation, _, _ = await setup()
    token = reservation.try_acquire()
    assert (await groups.request(**args(groups)))["status"] == "busy"
    assert not gateway.calls
    reservation.release(token)
    assert (await groups.request(**args(groups)))["status"] == "confirmed"
    await groups.close()


@pytest.mark.asyncio
async def test_policy_revocation_immediately_before_http_handoff():
    groups, gateway, _, reservation, _, _ = await setup()
    enabled = True

    async def revoke():
        nonlocal enabled
        enabled = False

    gateway.before_write = revoke
    request = args(groups) | {"admission": lambda: enabled}
    assert (await groups.request(**request))["possibly_sent"] is False
    assert not gateway.calls and reservation.revision == 0
    await groups.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["unchanged", "unknown", "blocked"])
async def test_uncertain_write_is_not_retried_and_blocks_until_fresh_report(behavior):
    groups, gateway, _, _, _, _ = await setup(timeout=0.03)
    gateway.behavior = behavior
    result = await groups.request(**args(groups))
    assert result["status"] == "uncertain" and result["possibly_sent"]
    assert len(gateway.calls) == 1 and gateway.post_reads <= 2
    assert groups.snapshots()[0]["outcome_unknown"]
    assert (await groups.request(**args(groups)))["status"] == "rejected"
    await groups.read_report(groups.snapshots()[0]["device_id"])
    assert not groups.snapshots()[0]["outcome_unknown"]
    await groups.close()


@pytest.mark.asyncio
async def test_write_permission_denial_is_not_cleared_by_successful_get():
    groups, gateway, _, _, _, owners = await setup()
    gateway.behavior = "denied"
    assert (await groups.request(**args(groups)))["status"] == "rejected"
    await groups.refresh({"home": "Home"}, owners)
    assert groups.snapshots()[0]["authorization"] == "denied"
    assert not groups.snapshots()[0]["can_set"]
    assert len(gateway.calls) == 1
    await groups.close()


@pytest.mark.asyncio
async def test_read_admitted_before_write_cannot_confirm_it():
    groups, gateway, _, _, _, _ = await setup()
    gateway.report["lights_on"] = True
    gateway.block_read = True
    old_read = asyncio.create_task(groups.read_report(groups.snapshots()[0]["device_id"]))
    await gateway.read_entered.wait()
    gateway.report["lights_on"] = False
    gateway.behavior = "unchanged"
    command = asyncio.create_task(groups.request(**args(groups)))
    await gateway.write_entered.wait()
    gateway.block_read = False
    gateway.read_release.set()
    await old_read
    assert (await command)["status"] == "uncertain"
    assert groups.snapshots()[0]["on"] is False
    await groups.close()


@pytest.mark.asyncio
async def test_cancelled_group_write_records_uncertainty_and_releases_after_cleanup():
    groups, gateway, _, reservation, _, _ = await setup()
    gateway.behavior = "blocked"
    command = asyncio.create_task(groups.request(**args(groups)))
    await gateway.write_entered.wait()
    assert reservation.try_acquire() is None
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    assert groups.snapshots()[0]["outcome_unknown"]
    token = reservation.try_acquire()
    assert token is not None
    reservation.release(token)
    await groups.close()


def test_lighting_reservation_retired_and_stale_tokens_never_release_new_owner():
    reservation = LightingReservation()
    first = reservation.try_acquire()
    assert reservation.mark_sent(first) and reservation.revision == 1
    assert reservation.mark_sent(first) and reservation.revision == 1
    reservation.release(first)
    second = reservation.try_acquire()
    reservation.release(first)
    assert reservation.try_acquire() is None and reservation.valid(second)
    assert not reservation.mark_sent(first)
    reservation.retire()
    assert not reservation.valid(second) and not reservation.mark_sent(second)
    reservation.release(second)
    assert reservation.try_acquire() is None


@pytest.mark.asyncio
async def test_blocked_location_does_not_block_healthy_location():
    groups, gateway, _, _, _, _ = await setup()
    release, healthy = asyncio.Event(), asyncio.Event()

    async def read(location, **kwargs):
        if location == "a":
            await release.wait()
            raise RingJsonError("network-unavailable")
        healthy.set()
        return inventory(location=location)

    gateway.read_light_groups = read
    refresh = asyncio.create_task(groups.refresh({"a": "A", "b": "B"}, {}, report_limit=0))
    await asyncio.wait_for(healthy.wait(), 1)
    assert any(row.identity.location_id == "b" for row in groups.records())
    release.set()
    await refresh
    assert not groups.complete
    assert groups.has_group("b", "garden")
    assert not groups.has_group("a", "garden")
    await groups.close()


@pytest.mark.asyncio
async def test_overall_timeout_preserves_healthy_subset_and_rotates_next_attempt(monkeypatch):
    import halo_server.devices.group_commands as module

    groups, gateway, _, _, _, _ = await setup()
    monkeypatch.setattr(module, "DISCOVERY_TIMEOUT_SECONDS", 0.02)
    groups._refresh_cursor = 0
    calls = []

    async def read(location, **kwargs):
        calls.append(location)
        if location in {"a", "b"}:
            await asyncio.Event().wait()
        return inventory(location=location)

    gateway.read_light_groups = read
    await groups.refresh({"a": "A", "b": "B", "c": "C"}, {}, report_limit=0)
    assert calls == ["a", "b"] and not groups.complete
    await groups.refresh({"a": "A", "b": "B", "c": "C"}, {}, report_limit=0)
    assert calls[2] == "c"
    assert groups.has_group("c", "garden")
    row = next(row for row in groups._groups.values() if row.inventory.location_id == "c")
    assert row.inventory_valid  # unrelated timeout must not invalidate this success
    await groups.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [30, 60, 300, 900])
async def test_supported_finite_on_durations(duration):
    groups, gateway, _, _, clock, _ = await setup()
    assert (await groups.request(**(args(groups) | {"duration_seconds": duration})))[
        "status"
    ] == "confirmed"
    assert gateway.calls[0][3] == duration
    clock[0] += duration + 1
    # Time passing alone never sends Off or asserts a physical False.
    assert len(gateway.calls) == 1
    assert groups.snapshots()[0]["on"] is not False
    await groups.close()


@pytest.mark.asyncio
async def test_rate_limit_prevents_immediate_command_retry():
    groups, gateway, _, _, _, _ = await setup()
    gateway.behavior = "limited"
    result = await groups.request(**args(groups))
    assert result["status"] == "rejected"
    assert groups.snapshots()[0]["reason"] == "retry-later"
    await groups.request(**args(groups))
    assert len(gateway.calls) == 1
    await groups.close()


@pytest.mark.asyncio
async def test_ownership_revocation_is_immediate_without_a_group_get():
    groups, gateway, _, _, _, _ = await setup()
    before = args(groups)
    groups.update_ownership({})
    assert groups.snapshots()[0]["authorization"] == "unknown"
    assert (await groups.request(**before))["status"] == "rejected"
    assert not gateway.calls
    groups.update_ownership({}, valid=False)
    assert groups.snapshots()[0]["on"] is None
    assert not groups.complete
    await groups.close()


@pytest.mark.asyncio
async def test_account_retirement_prevents_delayed_group_handoff():
    groups, gateway, provider, reservation, _, _ = await setup()

    async def retire():
        provider._active = lambda: False
        reservation.retire()

    gateway.before_write = retire
    assert (await groups.request(**args(groups)))["possibly_sent"] is False
    assert not gateway.calls and reservation.try_acquire() is None
    other = LightingReservation()
    assert other.try_acquire() is not None
    await groups.close()


@pytest.mark.asyncio
async def test_external_lighting_mutation_invalidates_cached_and_crossed_group_reports():
    groups, gateway, _, reservation, _, _ = await setup()
    key = groups.snapshots()[0]["device_id"]
    gateway.block_read = True
    read = asyncio.create_task(groups.read_report(key))
    await gateway.read_entered.wait()
    token = reservation.try_acquire()
    assert reservation.mark_sent(token)
    reservation.release(token)
    assert groups.snapshots()[0]["on"] is None
    assert groups.details(key)["members"][0]["on"] is None
    gateway.block_read = False
    gateway.read_release.set()
    assert await read is False
    assert groups.snapshots()[0]["on"] is None
    assert await groups.read_report(key) is True
    assert groups.snapshots()[0]["on"] is False
    await groups.close()


@pytest.mark.asyncio
async def test_list_retry_after_is_respected_between_background_refreshes():
    groups, gateway, _, _, clock, owners = await setup()
    calls = []

    async def limited(location, **kwargs):
        calls.append(location)
        raise RingJsonError("rate-limited", retry_after=120)

    gateway.read_light_groups = limited
    await groups.refresh({"home": "Home"}, owners)
    await groups.refresh({"home": "Home"}, owners)
    clock[0] += 119
    await groups.refresh({"home": "Home"}, owners)
    assert calls == ["home"]
    clock[0] += 1
    await groups.refresh({"home": "Home"}, owners)
    assert calls == ["home", "home"]
    await groups.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("device_id", [[], {}, None, True])
async def test_malformed_command_identity_is_rejected_without_exception(device_id):
    groups, gateway, _, _, _, _ = await setup()
    assert (await groups.request(**(args(groups) | {"device_id": device_id})))[
        "status"
    ] == "rejected"
    assert not gateway.calls
    await groups.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_locations", [False, True])
async def test_group_list_crossing_owner_or_location_change_cannot_restore_old_authority(
    changed_locations,
):
    groups, gateway, _, _, _, owners = await setup()
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(location, **kwargs):
        entered.set()
        await release.wait()
        return inventory()

    gateway.read_light_groups = read
    refresh = asyncio.create_task(groups.refresh({"home": "Home"}, owners, report_limit=0))
    await entered.wait()
    groups.update_ownership({}, locations={} if changed_locations else {"home": "Home"})
    release.set()
    await refresh
    row = groups.snapshots()[0]
    assert row["authorization"] == "unknown" and not row["can_set"]
    assert not groups.complete
    assert (await groups.request(**args(groups)))["status"] == "rejected"
    assert not gateway.calls
    await groups.close()


@pytest.mark.asyncio
async def test_identical_context_receipt_does_not_supersede_inflight_group_discovery():
    groups, gateway, _, _, _, owners = await setup()
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(location, **kwargs):
        entered.set()
        await release.wait()
        return inventory()

    gateway.read_light_groups = read
    refresh = asyncio.create_task(groups.refresh({"home": "Home"}, owners, report_limit=0))
    await entered.wait()
    groups.update_ownership(dict(owners), locations={"home": "Renamed Home"})
    release.set()
    await refresh
    assert groups.complete and groups.snapshots()[0]["authorization"] == "allowed"
    await groups.close()


@pytest.mark.asyncio
async def test_unknown_aggregate_does_not_resolve_an_uncertain_write():
    groups, gateway, _, _, _, _ = await setup(timeout=0.03)
    gateway.behavior = "unknown"
    await groups.request(**args(groups))
    before = groups.snapshots()[0]
    assert before["outcome_unknown"]
    gateway.report["lights_on"] = None
    await groups.read_report(before["device_id"])
    after = groups.snapshots()[0]
    assert after["on"] is None and after["outcome_unknown"]
    assert after["readback_revision"] == before["readback_revision"]
    gateway.report["lights_on"] = False
    await groups.read_report(before["device_id"])
    assert not groups.snapshots()[0]["outcome_unknown"]
    await groups.close()
