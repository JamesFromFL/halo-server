"""Consumer callbacks must not block the event loop's authority checks."""

import asyncio
import concurrent.futures
import threading
import time
from types import SimpleNamespace

import pytest

import halo_server.service as service_module
from halo_server.media.buffer import EncodedMediaBuffer
from halo_server.media.consumers import BufferedMediaConsumer
from halo_server.service import HaloService


@pytest.mark.asyncio
async def test_gateway_renewal_keeps_main_loop_current_checks_responsive(monkeypatch):
    """Use the actual gateway handoff that previously deadlocked with is_current."""
    loop = asyncio.get_running_loop()
    service = SimpleNamespace(_main_loop=loop)
    monkeypatch.setattr(service_module, "GATEWAY_CALL_TIMEOUT", 0.5)
    observations = []

    async def renew_on_main_loop():
        observations.append("renewed")
        assert asyncio.get_running_loop() is loop
        assert consumer.is_current()
        return True

    def inspect_on_main_loop():
        observations.append(("current", consumer.is_current()))

    def renew():
        # Queue an authority check before the gateway coroutine, just as an
        # already-active audio/video sender can run before the lease renewal.
        loop.call_soon_threadsafe(inspect_on_main_loop)
        return HaloService._run_gateway_call(service, renew_on_main_loop())

    consumer = BufferedMediaConsumer(EncodedMediaBuffer(), renew, lambda: None)
    assert await asyncio.wait_for(asyncio.to_thread(consumer.renew), 2)
    assert observations == [("current", True), "renewed"]
    consumer.close()


def test_concurrent_renewals_serialize_callbacks_without_blocking_current_checks():
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    calls = []
    active = 0

    def renew():
        nonlocal active
        active += 1
        assert active == 1
        number = len(calls) // 2 + 1
        calls.append(("start", number))
        try:
            if number == 1:
                entered.set()
                assert release.wait(2)
            return True
        finally:
            calls.append(("end", number))
            active -= 1

    consumer = BufferedMediaConsumer(EncodedMediaBuffer(), renew, lambda: None)

    def second_renew():
        second_started.set()
        return consumer.renew()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(consumer.renew)
        assert entered.wait(1)
        second = pool.submit(second_renew)
        try:
            assert second_started.wait(1)
            assert pool.submit(consumer.is_current).result(timeout=1)
            assert calls == [("start", 1)]
        finally:
            release.set()
        assert first.result(timeout=2)
        assert second.result(timeout=2)
    assert calls == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]
    consumer.close()


def test_close_fences_immediately_but_releases_only_after_inflight_renewal():
    entered, release, close_started = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def renew():
        calls.append("renew-start")
        entered.set()
        assert release.wait(2)
        calls.append("renew-end")
        return True

    def close():
        calls.append("release")

    consumer = BufferedMediaConsumer(EncodedMediaBuffer(), renew, close)

    def close_consumer():
        close_started.set()
        consumer.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        renewing = pool.submit(consumer.renew)
        assert entered.wait(1)
        closing = pool.submit(close_consumer)
        try:
            assert close_started.wait(1)
            deadline = time.monotonic() + 1
            while pool.submit(consumer.is_current).result(timeout=1):
                assert time.monotonic() < deadline
            assert not pool.submit(consumer.renew).result(timeout=1)
            assert not closing.done()
            assert calls == ["renew-start"]
        finally:
            release.set()
        assert not renewing.result(timeout=2)
        closing.result(timeout=2)
    assert calls == ["renew-start", "renew-end", "release"]
    consumer.close()
    assert calls == ["renew-start", "renew-end", "release"]


def test_failed_renewal_releases_serialization_for_retry_and_final_close():
    attempts = []

    def renew():
        attempts.append("renew")
        if len(attempts) == 1:
            raise RuntimeError("Injected renewal failure")
        return True

    consumer = BufferedMediaConsumer(
        EncodedMediaBuffer(), renew, lambda: attempts.append("release")
    )
    with pytest.raises(RuntimeError, match="renewal failure"):
        consumer.renew()
    assert consumer.is_current()
    assert consumer.renew()
    consumer.close()
    assert not consumer.renew()
    assert attempts == ["renew", "renew", "release"]


def test_failed_close_after_renewal_stays_fenced_and_retries_actual_release():
    calls = []

    def close():
        calls.append("release")
        if calls.count("release") == 1:
            raise RuntimeError("Injected release failure")

    consumer = BufferedMediaConsumer(
        EncodedMediaBuffer(), lambda: calls.append("renew") or True, close
    )
    assert consumer.renew()
    with pytest.raises(RuntimeError, match="release failure"):
        consumer.close()
    assert not consumer.is_current()
    assert not consumer.renew()
    consumer.close()
    consumer.close()
    assert calls == ["renew", "release", "release"]


def test_current_rechecks_close_after_external_validity_callback():
    entered, release, released = threading.Event(), threading.Event(), threading.Event()

    def valid():
        entered.set()
        assert release.wait(2)
        return True

    consumer = BufferedMediaConsumer(
        EncodedMediaBuffer(), lambda: True, released.set, validity_callback=valid
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        checking = pool.submit(consumer.is_current)
        assert entered.wait(1)
        closing = pool.submit(consumer.close)
        try:
            assert released.wait(1)
            closing.result(timeout=1)
        finally:
            release.set()
        assert not checking.result(timeout=2)


def test_renewal_rechecks_authority_after_callback():
    current = True

    def renew():
        nonlocal current
        current = False
        return True

    consumer = BufferedMediaConsumer(
        EncodedMediaBuffer(), renew, lambda: None, validity_callback=lambda: current
    )
    assert not consumer.renew()
    assert not consumer.is_current()
    consumer.close()
