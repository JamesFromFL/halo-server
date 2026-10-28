import asyncio
import threading
from types import SimpleNamespace

import pytest

from halo_server.media.consumers import BufferedMediaConsumer
from halo_server.media.leases import LeasePurpose
from halo_server.media.manager import CaptureRegistry, acquire_owned


@pytest.mark.asyncio
async def test_cancelled_acquire_keeps_admission_until_worker_and_release_settle():
    entered = threading.Event()
    resume = threading.Event()
    leases = set()
    calls = []

    class Session:
        def acquire(self, purpose, *, lease_id):
            entered.set()
            assert resume.wait(2)
            leases.add(lease_id)

        def release(self, lease_id):
            leases.discard(lease_id)
            calls.append("released")

    lock = asyncio.Lock()

    async def claim():
        async with lock:
            await acquire_owned(Session(), LeasePurpose.INTERCOM, "first")

    task = asyncio.create_task(claim())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # A second cancellation must not detach compensating cleanup.
        await asyncio.sleep(0)
        assert lock.locked()
        assert not task.done()
    finally:
        resume.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert leases == set()
    assert calls == ["released"]
    assert not lock.locked()


def test_timed_out_capture_remains_owned_until_receiver_really_retires():
    registry = CaptureRegistry()
    session = SimpleNamespace(receiver_retired=False)
    registry.active["camera"] = session
    registry.retire("camera", session)

    registry.finish_retirement("camera", session)

    assert registry.active == {}
    assert registry.retiring == {"camera": session}
    assert not registry.can_admit("camera")
    session.receiver_retired = True
    assert registry.can_admit("camera")
    assert registry.retiring == {}


def test_consumer_checks_current_authority_between_deliveries_and_renewal():
    allowed = [True]
    renewals = []
    closes = []
    consumer = BufferedMediaConsumer(
        buffer=object(),
        renew_callback=lambda: renewals.append(True) or True,
        close_callback=lambda: closes.append(True),
        validity_callback=lambda: allowed[0],
    )
    assert consumer.is_current()
    allowed[0] = False
    assert not consumer.is_current()
    assert not consumer.renew()
    assert renewals == []
    consumer.close()
    consumer.close()
    assert closes == [True]
