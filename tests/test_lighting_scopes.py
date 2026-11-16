"""Synthetic target-scoped observations and ownership-free lighting waiters."""

import asyncio
import threading

import pytest

import halo_server.ring.lighting as lighting
from halo_server.ring.lighting import LightingReservation, LightingRetiredError


def mutate(reservation, camera_id=None):
    token = reservation.try_acquire(camera_id)
    assert token is not None and reservation.mark_sent(token)
    revision = reservation.acknowledge(token)
    reservation.release(token)
    return revision


def test_camera_mutations_preserve_unrelated_receipts_but_not_same_target_or_wildcard():
    reservation = LightingReservation()
    assert reservation.observation_current(0, "a")
    token = reservation.try_acquire("a")
    assert reservation.mark_sent(token)
    assert not reservation.observation_current(0, "a")
    assert reservation.observation_current(0, "b")
    before_ack = reservation.revision
    acknowledged = reservation.acknowledge(token)
    assert not reservation.observation_current(before_ack, "a")
    assert reservation.observation_current(before_ack, "b")
    reservation.release(token)
    other_ack = mutate(reservation, "b")
    assert other_ack > acknowledged
    assert reservation.observation_current(acknowledged, "a")
    assert not reservation.observation_current(acknowledged, "b")
    assert reservation.observation_current(other_ack, "b")
    global_ack = mutate(reservation)
    assert not reservation.observation_current(acknowledged, "a")
    assert not reservation.observation_current(other_ack, "b")
    assert reservation.observation_current(global_ack, "a")
    assert reservation.observation_current(global_ack, "b")


@pytest.mark.parametrize("value", [-1, True, False, None, 0.0, "0", 1, 2**1024])
def test_unknown_or_future_revision_never_certifies_observation(value):
    assert not LightingReservation().observation_current(value, "a")


def test_bounded_target_history_cannot_resurrect_any_old_receipt(monkeypatch):
    monkeypatch.setattr(lighting, "MAX_LIGHTING_TARGETS", 2)
    reservation = LightingReservation()
    old = mutate(reservation, "a")
    mutate(reservation, "b")
    assert reservation.observation_current(old, "a")
    reset = mutate(reservation, "c")
    assert not reservation.observation_current(old, "a")
    assert not reservation.observation_current(old, "b")
    assert reservation.observation_current(reset, "a")
    for index in range(12):
        mutate(reservation, str(index))
        assert len(reservation._camera_revisions) <= 2
        assert not reservation.observation_current(old, "a")
    reservation.retire()
    assert not reservation.observation_current(reservation.revision, "a")


@pytest.mark.asyncio
async def test_waiter_timeout_and_cancellation_never_receive_or_release_ownership():
    reservation = LightingReservation()
    owner = reservation.try_acquire("a")
    assert not await reservation.wait_available(0.001)
    assert reservation.valid(owner) and not reservation._waiters
    waiting = asyncio.create_task(reservation.wait_available(2))
    await asyncio.sleep(0)
    assert len(reservation._waiters) == 1
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert reservation.valid(owner) and not reservation._waiters
    reservation.release(owner)
    next_owner = reservation.try_acquire("b")
    assert next_owner is not None
    reservation.release(next_owner)


@pytest.mark.asyncio
async def test_cross_thread_release_wakes_waiter_without_handing_it_a_token():
    reservation = LightingReservation()
    owner = reservation.try_acquire("a")
    waiting = asyncio.create_task(reservation.wait_available(2))
    await asyncio.sleep(0)
    worker = threading.Thread(target=reservation.release, args=(owner,))
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert await waiting is True and not reservation._waiters
    assert reservation.revision == 0  # Waiting never creates a mutation.
    next_owner = reservation.try_acquire("b")
    assert next_owner is not None
    reservation.release(next_owner)


@pytest.mark.asyncio
async def test_retirement_wakes_every_waiter_and_never_revives_reservation():
    reservation = LightingReservation()
    owner = reservation.try_acquire("a")
    waiting = [asyncio.create_task(reservation.wait_available(2)) for _ in range(3)]
    await asyncio.sleep(0)
    reservation.retire()
    for task in waiting:
        with pytest.raises(LightingRetiredError):
            await task
    with pytest.raises(LightingRetiredError):
        await reservation.wait_available(1)
    assert not reservation._waiters
    reservation.release(owner)
    assert reservation.try_acquire("b") is None
    assert not reservation.observation_current(0, "b")


@pytest.mark.asyncio
async def test_waiter_capacity_is_bounded_and_stale_release_does_not_wake(monkeypatch):
    monkeypatch.setattr(lighting, "MAX_LIGHTING_WAITERS", 2)
    reservation = LightingReservation()
    old = reservation.try_acquire("a")
    reservation.release(old)
    owner = reservation.try_acquire("b")
    waiting = [asyncio.create_task(reservation.wait_available(2)) for _ in range(2)]
    await asyncio.sleep(0)
    assert not await reservation.wait_available(1)
    reservation.release(old)
    await asyncio.sleep(0)
    assert all(not task.done() for task in waiting)
    assert reservation.valid(owner)
    reservation.release(owner)
    # New arrivals may win before notified contenders. The service must retain
    # its own per-camera FIFO lock and revalidate/retry acquisition before send.
    new_owner = reservation.try_acquire("c")
    assert all(await asyncio.gather(*waiting))
    assert reservation.valid(new_owner) and reservation.try_acquire("d") is None
    assert not reservation._waiters
    reservation.release(new_owner)


@pytest.mark.asyncio
async def test_cancellation_after_notification_scheduled_does_not_strand_token():
    reservation = LightingReservation()
    owner = reservation.try_acquire("a")
    waiting = asyncio.create_task(reservation.wait_available(2))
    await asyncio.sleep(0)
    reservation.release(owner)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await asyncio.sleep(0)  # A late notification must not mutate a canceled future.
    assert not reservation._waiters
    assert await reservation.wait_available(1)
    next_owner = reservation.try_acquire("b")
    assert next_owner is not None
    reservation.release(next_owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, True, None, "1", float("nan"), float("inf"), 2**1024])
async def test_invalid_wait_budget_does_not_allocate_waiter(timeout):
    reservation = LightingReservation()
    owner = reservation.try_acquire("a")
    assert not await reservation.wait_available(timeout)
    assert reservation.valid(owner) and not reservation._waiters
    reservation.release(owner)
