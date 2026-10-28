"""A downstream delivery fence must never masquerade as native lease release."""

import concurrent.futures
import threading

import pytest

from halo_server.media.buffer import EncodedMediaBuffer
from halo_server.media.consumers import BufferedMediaConsumer


def test_failed_release_fences_delivery_but_retries_callback_until_actual_success():
    attempts, renewals = [], []

    def close():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("Injected lease release failure")

    consumer = BufferedMediaConsumer(
        EncodedMediaBuffer(),
        lambda: renewals.append(True) or True,
        close,
    )
    assert consumer.is_current() and consumer.renew()
    with pytest.raises(RuntimeError, match="release failure"):
        consumer.close()
    assert not consumer.is_current() and not consumer.renew()
    assert renewals == [True] and not consumer._released
    consumer.close()
    consumer.close()
    assert attempts == [True, True] and consumer._released


@pytest.mark.parametrize("first_fails", [False, True])
def test_concurrent_closes_serialize_actual_release_and_retry_only_failed_attempt(first_fails):
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    attempts = []
    active = 0

    def close():
        nonlocal active
        active += 1
        assert active == 1
        attempts.append(True)
        try:
            if len(attempts) == 1:
                entered.set()
                assert release.wait(2)
                if first_fails:
                    raise RuntimeError("Injected first owner failure")
        finally:
            active -= 1

    consumer = BufferedMediaConsumer(EncodedMediaBuffer(), lambda: True, close)

    def second_close():
        second_started.set()
        consumer.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(consumer.close)
        assert entered.wait(1)
        second = pool.submit(second_close)
        try:
            assert second_started.wait(1)
            assert not consumer.is_current() and not consumer.renew()
            assert not consumer._released and attempts == [True]
        finally:
            release.set()
        if first_fails:
            with pytest.raises(RuntimeError, match="first owner failure"):
                first.result(timeout=2)
        else:
            first.result(timeout=2)
        second.result(timeout=2)
    assert consumer._released and attempts == [True] * (2 if first_fails else 1)
    consumer.close()
    assert attempts == [True] * (2 if first_fails else 1)
