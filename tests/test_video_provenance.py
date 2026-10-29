"""Bounded source receipts and retirement authority across native video awaits."""

import asyncio
from dataclasses import replace

import pytest
from test_media_h264 import encode_source

from halo_server.media import transcode
from halo_server.media.h264 import VideoConfiguration
from halo_server.media.transcode import MAX_SOURCE_RECEIPTS, AccessUnitAdapter, VideoTransformError


@pytest.fixture(scope="module")
def bitstreams():
    return (
        encode_source(profile="high", level="5.0", frames=4),
        encode_source(profile="high", frames=4),
    )


def configured():
    return AccessUnitAdapter(VideoConfiguration(320, 240, 15, "high", "3.1", 1000))


@pytest.mark.asyncio
async def test_delayed_multiple_outputs_keep_original_receipts_before_batch_retirement(
    monkeypatch, bitstreams
):
    source, outputs = bitstreams

    class Worker:
        def __init__(self, _configuration):
            self.calls = 0

        async def feed(self, _payload, _timestamp, *, force_idr):
            self.calls += 1
            if self.calls % 2:
                return ()
            start = self.calls - 2
            return tuple((source[index][1], outputs[index][0]) for index in (start, start + 1))

        async def close(self):
            pass

    monkeypatch.setattr(transcode, "VideoWorker", Worker)
    adapter = configured()
    result = []
    try:
        for index, (payload, timestamp) in enumerate(source):
            result.extend(
                await adapter.feed(
                    payload, timestamp, capture_epoch=7, received_at=100 + index * 0.2
                )
            )
        assert [unit.timestamp for unit in result] == [timestamp for _payload, timestamp in source]
        assert [unit.received_at for unit in result] == [100, 100.2, 100.4, 100.6]
        assert all(
            unit.capture_epoch == 7 and unit.configuration_generation == 1 for unit in result
        )
        assert not adapter._source_receipts
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_actual_native_bframe_reordering_resolves_each_original_input_receipt():
    source = encode_source(profile="high", frames=10, bframes=2)
    adapter = configured()
    receipts = {timestamp: 100 + index * 0.01 for index, (_payload, timestamp) in enumerate(source)}
    outputs = []
    try:
        for payload, timestamp in source:
            outputs.extend(
                await adapter.feed(
                    payload, timestamp, capture_epoch=3, received_at=receipts[timestamp]
                )
            )
        assert len(outputs) >= 6
        assert all(output.received_at == receipts[output.timestamp] for output in outputs)
        assert all(
            output.capture_epoch == 3 and output.configuration_generation == 1 for output in outputs
        )
        assert [output.timestamp for output in outputs] == sorted(
            output.timestamp for output in outputs
        )
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_native_faster_source_drops_leave_metadata_bounded_without_false_origin_loss():
    source = encode_source(fps=60, frames=170)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 30, "main", "3.1", 1000))
    receipts = {timestamp: 100 + index * 0.01 for index, (_payload, timestamp) in enumerate(source)}
    outputs = []
    try:
        for payload, timestamp in source:
            outputs.extend(
                await adapter.feed(
                    payload, timestamp, capture_epoch=4, received_at=receipts[timestamp]
                )
            )
            assert len(adapter._source_receipts) <= MAX_SOURCE_RECEIPTS
        assert [output.timestamp for output in outputs] == list(range(0, 255000, 3000))
        assert all(output.received_at == receipts[output.timestamp] for output in outputs)
        assert all(output.capture_epoch == 4 for output in outputs)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_copy_and_legacy_calls_keep_additive_metadata_compatibility():
    source = encode_source(frames=2)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "baseline", "3.1", 2000))
    try:
        first = (await adapter.feed(*source[0]))[0]
        assert first.mode == "copy" and first.payload == source[0][0]
        assert first.capture_epoch == 0 and first.configuration_generation == 1
        assert first.received_at is None
        second = (await adapter.feed(*source[1], received_at=10))[0]
        assert second.payload == source[1][0] and second.received_at == 10.0
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [True, -1, float("nan"), float("inf"), "100", 10**1000],
    ids=("boolean", "negative", "nan", "infinite", "text", "overflow"),
)
async def test_invalid_receipts_fail_before_worker_creation(receipt, monkeypatch, bitstreams):
    source, _outputs = bitstreams
    made = []
    monkeypatch.setattr(transcode, "VideoWorker", lambda _config: made.append(True))
    adapter = configured()
    try:
        with pytest.raises(VideoTransformError, match="receipt"):
            await adapter.feed(*source[0], capture_epoch=1, received_at=receipt)
        assert not made and adapter._worker is None and not adapter._source_receipts
        assert adapter.configuration_generation == 1
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["epoch_reset", "reconfigure"])
@pytest.mark.parametrize("serialized", [False, True])
async def test_retirement_during_old_close_cannot_resurrect_or_erase_new_worker(
    monkeypatch, bitstreams, operation, serialized
):
    source, outputs = bitstreams
    entered, release = asyncio.Event(), asyncio.Event()
    made = []

    class Old:
        calls = 0

        def __init__(self):
            self.lock = asyncio.Lock()

        async def close_inner(self):
            async with self.lock:
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    await release.wait()

        async def close(self):
            if serialized:
                task = asyncio.create_task(self.close_inner())
                await asyncio.shield(task)
            else:
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    await release.wait()

    class New:
        def __init__(self, _configuration):
            made.append(self)

        async def feed(self, _payload, timestamp, *, force_idr):
            return ((timestamp, outputs[0][0]),)

        async def close(self):
            pass

    monkeypatch.setattr(transcode, "VideoWorker", New)
    adapter = configured()
    original = adapter.configuration
    adapter._epoch, adapter._worker = 1, Old()
    pending = asyncio.create_task(
        adapter.feed(*source[0], capture_epoch=2, received_at=100)
        if operation == "epoch_reset"
        else adapter.reconfigure(replace(original, max_bit_rate=900))
    )
    close = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if serialized:
            close = asyncio.create_task(adapter.close())
            await asyncio.sleep(0)
        else:
            await adapter.close()
        assert adapter._closed
        release.set()
        with pytest.raises(VideoTransformError, match="retired"):
            await asyncio.wait_for(pending, 1)
        if close is not None:
            await asyncio.wait_for(close, 1)
        assert not made and adapter._worker is None and not adapter._source_receipts
        assert adapter.configuration == original and adapter.configuration_generation == 1
    finally:
        release.set()
        await asyncio.gather(pending, *(tuple([close]) if close else ()), return_exceptions=True)
        await adapter.close()


@pytest.mark.asyncio
async def test_native_result_after_close_never_returns_media(monkeypatch, bitstreams):
    source, outputs = bitstreams
    entered, release = asyncio.Event(), asyncio.Event()

    class Worker:
        async def feed(self, _payload, timestamp, *, force_idr):
            entered.set()
            await release.wait()
            return ((timestamp, outputs[0][0]),)

        async def close(self):
            pass

    monkeypatch.setattr(transcode, "VideoWorker", lambda _configuration: Worker())
    adapter = configured()
    pending = asyncio.create_task(adapter.feed(*source[0], capture_epoch=1, received_at=100))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.close()
        release.set()
        with pytest.raises(VideoTransformError, match="retired"):
            await asyncio.wait_for(pending, 1)
        assert adapter._worker is None and not adapter._source_receipts
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await adapter.close()


@pytest.mark.asyncio
async def test_failed_retirement_keeps_exact_owner_for_retry():
    class Worker:
        fail = True

        async def close(self):
            if self.fail:
                raise VideoTransformError("Synthetic close failed")

    adapter = configured()
    old = adapter._worker = Worker()
    adapter._record_receipt(1, 0, 100)
    with pytest.raises(VideoTransformError, match="close failed"):
        await adapter.close()
    assert adapter._worker is old and adapter._closed and not adapter._source_receipts
    old.fail = False
    await adapter.close()
    assert adapter._worker is None


@pytest.mark.asyncio
async def test_epoch_and_reconfigure_clear_only_retired_origins_and_advance_contract(
    monkeypatch, bitstreams
):
    source, outputs = bitstreams
    workers = []

    class Worker:
        def __init__(self, _configuration):
            self.closed = False
            workers.append(self)

        async def feed(self, _payload, timestamp, *, force_idr):
            return () if len(workers) == 1 else ((timestamp, outputs[0][0]),)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(transcode, "VideoWorker", Worker)
    adapter = configured()
    try:
        assert await adapter.feed(*source[0], capture_epoch=1, received_at=100) == ()
        await adapter.reconfigure(adapter.configuration)
        assert adapter.configuration_generation == 1 and len(adapter._source_receipts) == 1
        await adapter.reconfigure(replace(adapter.configuration, max_bit_rate=900))
        assert workers[0].closed and not adapter._source_receipts
        assert adapter.configuration_generation == 2
        result = (await adapter.feed(*source[0], capture_epoch=1, received_at=200))[0]
        assert result.received_at == 200 and result.capture_epoch == 1
        assert result.configuration_generation == 2
        result = (await adapter.feed(*source[0], capture_epoch=2, received_at=300))[0]
        assert result.received_at == 300 and result.capture_epoch == 2
        assert result.configuration_generation == 2 and workers[1].closed
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_missing_or_evicted_origin_is_explicit_and_metadata_stays_bounded(
    monkeypatch, bitstreams
):
    source, outputs = bitstreams

    class Worker:
        calls = 0

        async def feed(self, _payload, _timestamp, *, force_idr):
            self.calls += 1
            return () if self.calls <= MAX_SOURCE_RECEIPTS + 1 else ((0, outputs[0][0]),)

        async def close(self):
            pass

    monkeypatch.setattr(transcode, "VideoWorker", lambda _configuration: Worker())
    adapter = configured()
    try:
        for index in range(MAX_SOURCE_RECEIPTS + 1):
            assert (
                await adapter.feed(
                    source[0][0], index * 6000, capture_epoch=1, received_at=100 + index
                )
                == ()
            )
            assert len(adapter._source_receipts) <= MAX_SOURCE_RECEIPTS
        assert (1, 0) not in adapter._source_receipts
        assert all(type(value) is float for value in adapter._source_receipts.values())
        with pytest.raises(VideoTransformError, match="source receipt is unavailable"):
            await adapter.feed(
                source[0][0],
                (MAX_SOURCE_RECEIPTS + 1) * 6000,
                capture_epoch=1,
                received_at=999,
            )
        assert len(adapter._source_receipts) <= MAX_SOURCE_RECEIPTS
    finally:
        await adapter.close()


def test_duplicate_and_reordered_origin_lookup_never_refreshes_or_prunes_a_neighbor():
    adapter = configured()
    adapter._record_receipt(1, 0, 100)
    adapter._record_receipt(1, 0, 101)
    adapter._record_receipt(1, 6000, 102)
    assert adapter._receipts(1, ((6000, b"fixture"), (0, b"fixture"))) == (102, 100)
    assert len(adapter._source_receipts) == 2
    adapter._record_receipt(1, 0, 99)
    assert adapter._receipts(1, ((0, b"fixture"),)) == (99,)
    adapter._record_receipt(1, 0, None)
    adapter._record_receipt(1, 0, 103)
    assert adapter._receipts(1, ((0, b"fixture"),)) == (None,)
    with pytest.raises(VideoTransformError, match="receipt"):
        adapter._receipts(1, ((12345, b"fixture"),))
    with pytest.raises(VideoTransformError, match="receipt"):
        adapter._receipts(1, ((0, b"fixture"), (0, b"fixture")))
    with pytest.raises(VideoTransformError, match="bound"):
        adapter._receipts(1, tuple((index, b"fixture") for index in range(5)))
