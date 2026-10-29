"""Worker-start failures cannot strand a media-bearing queued operation."""

import asyncio
import concurrent.futures
import gc
import threading
import weakref

import pytest
from test_media_legacy_lane import Encoder

from halo_server.media import legacy_audio


@pytest.mark.parametrize("fail_at", [1, 2])
def test_worker_start_failure_retries_missing_only_and_releases_frame(monkeypatch, fail_at):
    scheduler = legacy_audio._Scheduler()
    monkeypatch.setattr(legacy_audio, "_SCHEDULER", scheduler)
    original = threading.Thread.start
    started = []

    def start(thread):
        started.append(thread)
        if len(started) == fail_at:
            raise RuntimeError("synthetic thread startup failure")
        original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    owner = legacy_audio.LegacyAudioOwner(Encoder)
    assert owner.claim()

    class QueuedFrame:
        pass

    value = QueuedFrame()
    reference = weakref.ref(value)
    failed = owner.submit(frame=value)
    del value
    assert isinstance(failed.exception(), legacy_audio.LegacyQueueError)
    assert failed.exception().__traceback__ is None
    assert failed.exception().__context__ is None
    assert owner._job is None and not scheduler.queue
    gc.collect()
    assert reference() is None
    assert len(scheduler.workers) == fail_at - 1
    assert all(worker.is_alive() for worker in scheduler.workers)
    retained_workers = tuple(scheduler.workers)

    # The failed claim remains the same owner, with no constructor attempted.
    # Retry starts only missing workers and admits exactly one real operation.
    completed = owner.submit(bytes(1920), 1, 48000)
    assert len(completed.result(timeout=2).packets) == 1
    assert len(scheduler.workers) == legacy_audio.NATIVE_WORKERS
    assert tuple(scheduler.workers[: len(retained_workers)]) == retained_workers
    assert len(started) == legacy_audio.NATIVE_WORKERS + 1
    owner.release()
    owner.fence()
    assert owner.wait_closed(2)
    assert not scheduler.owners and not scheduler.queue


@pytest.mark.asyncio
@pytest.mark.parametrize("admission_wait", [False, True])
async def test_cancelled_native_await_traceback_releases_completed_pcm(admission_wait):
    class Owner:
        def cancel_queued(self, _future):
            return False  # This synthetic operation has already started.

    future = concurrent.futures.Future()
    task = asyncio.create_task(
        legacy_audio.wait_native(future, Owner() if admission_wait else None)
    )
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    value = legacy_audio.LegacyResult(bytes(1920), 1, 48000, (), 0.001, False)
    reference = weakref.ref(value)
    future.set_result(value)
    del value, future
    saved = None
    try:
        await task
    except asyncio.CancelledError as exc:
        saved = exc  # Retain the real traceback, as an owner/task may do.
    assert saved is not None
    await asyncio.sleep(0)
    gc.collect()
    assert reference() is None
