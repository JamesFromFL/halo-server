import pytest

from halo_server.media.leases import (
    LeaseActionType,
    LeasePolicy,
    LeasePurpose,
    MediaLeaseCoordinator,
)


class FakeClock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value


def action_types(actions):
    return [action.type for action in actions]


def test_first_lease_starts_one_upstream_and_additional_leases_share_it():
    coordinator = MediaLeaseCoordinator(clock=FakeClock())

    first, first_actions = coordinator.acquire(LeasePurpose.EVENT)
    second, second_actions = coordinator.acquire(LeasePurpose.HOMEKIT_LIVE)

    assert action_types(first_actions) == [LeaseActionType.START_UPSTREAM]
    assert second_actions == []
    assert first.generation == second.generation == 1
    assert len(coordinator.snapshot().leases) == 2


def test_upstream_stops_only_after_last_lease_releases():
    coordinator = MediaLeaseCoordinator(clock=FakeClock())
    first, _ = coordinator.acquire(LeasePurpose.EVENT)
    second, _ = coordinator.acquire(LeasePurpose.HOMEKIT_LIVE)

    assert coordinator.release(first.id) == []
    actions = coordinator.release(second.id)

    assert action_types(actions) == [LeaseActionType.STOP_UPSTREAM]
    assert coordinator.snapshot().upstream_requested is False


def test_expired_last_lease_stops_upstream():
    clock = FakeClock()
    coordinator = MediaLeaseCoordinator(clock=clock)
    lease, _ = coordinator.acquire(LeasePurpose.SNAPSHOT)

    clock.value = lease.expires_at
    actions = coordinator.expire()

    assert action_types(actions) == [LeaseActionType.STOP_UPSTREAM]
    assert actions[0].reason == "leases_expired"


def test_renewal_cannot_exceed_absolute_maximum_lifetime():
    policies = {purpose: LeasePolicy(default_ttl=2, maximum_lifetime=5) for purpose in LeasePurpose}
    clock = FakeClock()
    coordinator = MediaLeaseCoordinator(policies=policies, clock=clock)
    lease, _ = coordinator.acquire(LeasePurpose.HOMEKIT_LIVE)

    clock.value = 1
    lease, _ = coordinator.renew(lease.id, ttl=100)

    assert lease.expires_at == 5
    assert lease.absolute_deadline == 5

    clock.value = 5
    assert coordinator.renew(lease.id)[0] is None


def test_idempotent_named_lease_renews_without_duplicate_start():
    clock = FakeClock()
    coordinator = MediaLeaseCoordinator(clock=clock)
    first, actions = coordinator.acquire(
        LeasePurpose.EVENT,
        lease_id="event-generation-1",
    )
    clock.value = 10

    renewed, renewed_actions = coordinator.acquire(
        LeasePurpose.EVENT,
        lease_id="event-generation-1",
    )

    assert action_types(actions) == [LeaseActionType.START_UPSTREAM]
    assert renewed_actions == []
    assert renewed.id == first.id
    assert len(coordinator.snapshot().leases) == 1


def test_expired_generation_cannot_stop_new_generation():
    clock = FakeClock()
    coordinator = MediaLeaseCoordinator(clock=clock)
    first, _ = coordinator.acquire(LeasePurpose.SNAPSHOT, ttl=1)
    clock.value = 1
    stop_actions = coordinator.expire()
    second, start_actions = coordinator.acquire(LeasePurpose.EVENT)

    assert first.generation == stop_actions[0].generation == 1
    assert second.generation == start_actions[0].generation == 2


def test_active_leases_move_to_a_new_generation_when_upstream_restarts():
    clock = FakeClock()
    coordinator = MediaLeaseCoordinator(clock=clock)
    first, _ = coordinator.acquire(LeasePurpose.EVENT, lease_id="event")
    second, _ = coordinator.acquire(LeasePurpose.HOMEKIT_LIVE, lease_id="live")

    actions = coordinator.restart_upstream("receiver_retry")
    snapshot = coordinator.snapshot()

    assert action_types(actions) == [LeaseActionType.START_UPSTREAM]
    assert actions[0].generation == 2
    assert actions[0].reason == "receiver_retry"
    assert snapshot.generation == 2
    assert {lease.id: lease.generation for lease in snapshot.leases} == {
        first.id: 2,
        second.id: 2,
    }
    assert snapshot.upstream_requested is True


def test_upstream_restart_requires_active_demand():
    coordinator = MediaLeaseCoordinator(clock=FakeClock())

    assert coordinator.restart_upstream("receiver_retry") == []


def test_stop_all_is_idempotent():
    coordinator = MediaLeaseCoordinator(clock=FakeClock())
    coordinator.acquire(LeasePurpose.EVENT)

    assert action_types(coordinator.stop_all()) == [LeaseActionType.STOP_UPSTREAM]
    assert coordinator.stop_all() == []


def test_next_deadline_tracks_earliest_consumer():
    coordinator = MediaLeaseCoordinator(clock=FakeClock())
    event, _ = coordinator.acquire(LeasePurpose.EVENT)
    snapshot, _ = coordinator.acquire(LeasePurpose.SNAPSHOT)

    assert coordinator.next_deadline() == snapshot.expires_at
    assert snapshot.expires_at < event.expires_at


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), True])
def test_invalid_lease_duration_is_rejected(duration):
    coordinator = MediaLeaseCoordinator(clock=FakeClock())

    with pytest.raises(ValueError, match="finite and positive"):
        coordinator.acquire(LeasePurpose.EVENT, ttl=duration)


def test_named_lease_cannot_change_purpose():
    coordinator = MediaLeaseCoordinator(clock=FakeClock())
    coordinator.acquire(LeasePurpose.EVENT, lease_id="same")

    with pytest.raises(ValueError, match="another purpose"):
        coordinator.acquire(LeasePurpose.HOMEKIT_LIVE, lease_id="same")


def test_per_camera_lease_count_is_bounded():
    coordinator = MediaLeaseCoordinator(clock=FakeClock(), max_leases=2)
    coordinator.acquire(LeasePurpose.EVENT)
    coordinator.acquire(LeasePurpose.HOMEKIT_LIVE)

    with pytest.raises(RuntimeError, match="limit"):
        coordinator.acquire(LeasePurpose.SCRYPTED_LIVE)


def test_named_lease_id_length_is_bounded():
    coordinator = MediaLeaseCoordinator(clock=FakeClock())

    with pytest.raises(ValueError, match="128"):
        coordinator.acquire(LeasePurpose.EVENT, lease_id="x" * 129)
