"""Native output receipt/epoch/configuration provenance at actual live admission."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS

import av
import pytest
from test_homekit_live import CONFIG, encoded_picture, native_crypto, reconstruct, wire_idr
from test_homekit_video_recovery import controlled, output
from test_media_h264 import encode_source

from halo_server.media.buffer import MediaStreamKind
from halo_server.media.transcode import AccessUnitAdapter


def origin(receipt, *, epoch=1, generation=1, payload=None, timestamp=0, mode="adapt"):
    unit = output(payload, timestamp)
    unit.received_at, unit.capture_epoch = receipt, epoch
    unit.configuration_generation, unit.mode = generation, mode
    return unit


class Adapter:
    configuration_generation = 1

    def __init__(self, responses):
        self.responses = list(responses)
        self.inputs = []
        self.requested = self.adapted = 0
        self.closed = False

    async def feed(self, payload, timestamp, *, capture_epoch, received_at):
        self.inputs.append((timestamp, capture_epoch, received_at))
        response = self.responses.pop(0)
        if callable(response):
            response = response(self)
        return response

    def request_keyframe(self):
        self.requested += 1

    def force_adaptation(self):
        self.adapted += 1

    async def close(self):
        self.closed = True


async def drive(manager, session, *, receipts=(100.0,), payload=None):
    inputs = iter(enumerate(receipts))

    async def read(**_kwargs):
        try:
            index, receipt = next(inputs)
        except StopIteration:
            raise asyncio.CancelledError from None
        return NS(
            capture_epoch=session.handle.capture_epoch,
            resynchronized=False,
            packets=(
                NS(
                    stream=MediaStreamKind.VIDEO,
                    payload=encoded_picture() if payload is None else payload,
                    rtp_timestamp=index * 6000,
                    received_at=receipt,
                    capture_epoch=session.handle.capture_epoch,
                ),
            ),
        )

    session.handle.read = read
    try:
        await manager._send_video(session)
    except asyncio.CancelledError:
        assert not session.retired
    if session.retired:
        async with asyncio.timeout(1):
            while manager.pending_resources:
                await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_delayed_picture_never_uses_new_triggering_input_receipt(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        session.adapter = Adapter([(origin(clock() - 0.501),)])
        await drive(manager, session)
        counters = session.metrics.snapshot()["counters"]
        assert session.adapter.inputs == [(0, session.capture_epoch, 100.0)]
        assert counters["stale_output_units"] == 1 and not sink.sent
        assert session.awaiting_idr and not session.retired


@pytest.mark.asyncio
async def test_fresh_older_picture_preserves_its_receipt_for_presentation_and_deadline(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        receipt = clock() - 0.300
        session.adapter = Adapter([(origin(receipt),)])
        observed = []

        def presentation(_session, kind, timestamp, received_at):
            observed.append((kind, timestamp, received_at))
            return received_at

        monkeypatch.setattr(manager, "_presentation_time", presentation)
        await drive(manager, session)
        assert sink.sent and not session.awaiting_idr
        assert observed == [("video", 0, receipt)]
        assert session.last_packet_at == receipt
        assert all(when <= receipt + 0.5 for when, _ in sink.sent)


@pytest.mark.asyncio
async def test_mixed_age_native_batch_sends_only_fresh_actual_idr(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        old = origin(clock() - 0.600, payload=encoded_picture(0))
        fresh = origin(clock() - 0.100, payload=encoded_picture(2), timestamp=6000)
        session.adapter = Adapter([(old, fresh)])
        await drive(manager, session)
        counters = session.metrics.snapshot()["counters"]
        assert counters["stale_output_units"] == counters["sent_access_units"] == 1
        crypto = native_crypto(session.prepared.ssrc)
        clear = [crypto.unprotect(packet) for _, packet in sink.sent]
        picture = b"".join(b"\0\0\0\1" + nal for nal in reconstruct(clear))
        decoded = av.CodecContext.create("h264", "r").decode(av.Packet(picture))[0]
        color = decoded.to_ndarray(format="rgb24").mean(axis=(0, 1))
        assert color[2] > 220 and color[0] < 30
        assert not session.awaiting_idr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing_receipt",
        "unknown_receipt",
        "nan",
        "infinite",
        "bool",
        "future",
        "negative",
        "missing_epoch",
        "old_epoch",
        "bool_epoch",
        "missing_generation",
        "old_generation",
        "bool_generation",
    ],
)
async def test_missing_or_wrong_output_metadata_fences_whole_batch_and_retires(monkeypatch, fault):
    async with controlled(monkeypatch) as (manager, controller, session, clock, sink):
        bad = origin(clock())
        if fault == "missing_receipt":
            del bad.received_at
        elif fault == "unknown_receipt":
            bad.received_at = None
        elif fault == "nan":
            bad.received_at = float("nan")
        elif fault == "infinite":
            bad.received_at = float("inf")
        elif fault == "bool":
            bad.received_at = True
        elif fault == "future":
            bad.received_at = clock() + 0.01
        elif fault == "negative":
            bad.received_at = -1
        elif fault == "missing_epoch":
            del bad.capture_epoch
        elif fault == "old_epoch":
            bad.capture_epoch += 1
        elif fault == "bool_epoch":
            bad.capture_epoch = True
        elif fault == "missing_generation":
            del bad.configuration_generation
        elif fault == "old_generation":
            bad.configuration_generation += 1
        elif fault == "bool_generation":
            bad.configuration_generation = True
        session.adapter = Adapter([(origin(clock()), bad)])
        await drive(manager, session)
        assert session.retired and controller.handle.closed and session.adapter.closed
        assert not sink.sent and manager.pending_resources == 0
        assert session.terminal["reason"] == "media_failed"


@pytest.mark.asyncio
async def test_generation_change_while_native_feed_waits_cannot_publish_old_result(monkeypatch):
    async with controlled(monkeypatch) as (manager, controller, session, clock, sink):

        def replaced(adapter):
            adapter.configuration_generation = 2
            return (origin(clock(), generation=1),)

        session.adapter = Adapter([replaced])
        await drive(manager, session)
        assert not sink.sent and session.retired and controller.handle.closed
        assert manager.pending_resources == 0


@pytest.mark.asyncio
async def test_copy_to_native_retry_passes_original_receipt_and_validates_native_metadata(
    monkeypatch,
):
    async with controlled(monkeypatch, bitrate=132) as (manager, controller, session, clock, sink):
        session.config = replace(CONFIG, max_bit_rate=132)
        receipt = clock() - 0.100
        source = wire_idr(10000)
        session.adapter = Adapter(
            [
                (origin(receipt, payload=source, mode="copy"),),
                (origin(None, payload=encoded_picture(), mode="adapt"),),
            ]
        )
        await drive(manager, session, receipts=(receipt,), payload=source)
        assert session.adapter.inputs == [(0, session.capture_epoch, receipt)] * 2
        assert session.adapter.adapted == 1
        assert not sink.sent and session.retired and controller.handle.closed
        assert manager.pending_resources == 0


@pytest.mark.asyncio
async def test_real_bframe_decoder_output_keeps_old_origin_when_new_input_releases_it(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        source = iter(encode_source(profile="high", bframes=2, frames=12))
        adapter = AccessUnitAdapter(CONFIG)
        session.adapter = adapter
        actual_feed = adapter.feed
        observed, presentations = [], []

        async def measured(payload, timestamp, **kwargs):
            units = await actual_feed(payload, timestamp, **kwargs)
            observed.extend(
                (timestamp, kwargs["received_at"], unit.timestamp, unit.received_at)
                for unit in units
            )
            return units

        async def read(**_kwargs):
            try:
                payload, timestamp = next(source)
            except StopIteration:
                raise asyncio.CancelledError from None
            receipt = clock() - (0.600 if timestamp == 0 else 0.100)
            return NS(
                capture_epoch=session.capture_epoch,
                resynchronized=False,
                packets=(
                    NS(
                        stream=MediaStreamKind.VIDEO,
                        payload=payload,
                        rtp_timestamp=timestamp,
                        received_at=receipt,
                        capture_epoch=session.capture_epoch,
                    ),
                ),
            )

        def presentation(_session, _kind, timestamp, receipt):
            presentations.append((timestamp, receipt))
            return receipt

        adapter.feed = measured
        session.handle.read = read
        monkeypatch.setattr(manager, "_presentation_time", presentation)
        original_end = manager._end
        failures = []

        def ended(*args, **kwargs):
            if kwargs.get("error") is not None:
                failures.append(kwargs["error"])
            original_end(*args, **kwargs)

        monkeypatch.setattr(manager, "_end", ended)
        with pytest.raises(asyncio.CancelledError):
            await manager._send_video(session)
            if failures:
                raise failures[0]
        initial = next(item for item in observed if item[2] == 0)
        assert initial[0] != 0 and initial[1] > initial[3]
        assert initial[3] == pytest.approx(99.4)
        counters = session.metrics.snapshot()["counters"]
        assert counters["stale_output_units"] == 1
        assert counters["sent_access_units"] > 1
        assert all(timestamp != 0 for timestamp, _receipt in presentations)
        assert not session.awaiting_idr

        crypto = native_crypto(session.prepared.ssrc)
        decoder = av.CodecContext.create("h264", "r")
        decoder.thread_count = 1
        clear, count = [], 0
        for _, packet in sink.sent:
            clear.append(crypto.unprotect(packet))
            if clear[-1][1] & 0x80:
                picture = b"".join(b"\0\0\0\1" + nal for nal in reconstruct(clear))
                count += len(decoder.decode(av.Packet(picture)))
                clear.clear()
        assert not clear and count == counters["sent_access_units"]
