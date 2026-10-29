"""Source-time native video, delayed force ownership and terminal qualification."""

from dataclasses import asdict
from fractions import Fraction

import av
import numpy as np
import pytest
from test_media_h264 import encode_source
from test_video_wire_encoder import encode_moving_source

from halo_server.media import transcode
from halo_server.media.h264 import VideoConfiguration, split_annexb
from halo_server.media.transcode import AccessUnitAdapter, VideoTransformError, VideoWorker
from halo_server.media.transcode_worker import MAX_FORCE_REQUESTS, Encoder, letterbox

CONFIG = VideoConfiguration(320, 240, 30, "main", "3.1", 1000)


@pytest.mark.asyncio
async def test_native_delayed_idr_ack_never_applies_to_older_p_or_refreshes_receipt():
    source = encode_source(profile="high", level="5.0", frames=6)
    adapter = AccessUnitAdapter(CONFIG)
    try:
        assert await adapter.feed(*source[0], capture_epoch=7, received_at=100) == ()
        assert adapter._worker.last_pending_frames == 1 and adapter._force
        first = (await adapter.feed(*source[1], capture_epoch=7, received_at=101))[0]
        assert first.timestamp == 0 and first.received_at == 100 and first.is_keyframe
        assert not adapter._force and not adapter._pending_forces
        assert not (await adapter.feed(*source[2], capture_epoch=7, received_at=102))[0].is_keyframe
        adapter.request_keyframe()
        token = adapter._force_revision
        older = (await adapter.feed(*source[3], capture_epoch=7, received_at=103))[0]
        assert older.timestamp == source[2][1] and older.received_at == 102
        assert not older.is_keyframe and adapter._force and token in adapter._pending_forces
        forced = (await adapter.feed(*source[4], capture_epoch=7, received_at=104))[0]
        assert forced.timestamp == source[3][1] and forced.received_at == 103
        assert forced.is_keyframe and {5, 7, 8} <= {
            nal[0] & 31 for nal in split_annexb(forced.payload)
        }
        assert not adapter._force and not adapter._pending_forces
        independent = av.CodecContext.create("h264", "r")
        independent.thread_count = 1
        assert len(independent.decode(av.Packet(forced.payload))) == 1
        worker = adapter._worker
        assert worker.last_pending_frames == 1 and len(adapter._source_receipts) == 1
        await adapter.close()
        assert worker.pid is None and not adapter._source_receipts and not adapter._pending_forces
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_native_force_requests_survive_empty_bframe_decoder_inputs_in_order():
    worker = VideoWorker(CONFIG)
    source = encode_source(profile="high", frames=12, bframes=2)
    acknowledgments = []
    try:
        for index, (payload, stamp) in enumerate(source):
            result = await worker.feed(payload, stamp, force_idr=index < 2)
            if index == 0:
                assert result == () and worker.last_output_force_requests == ()
            for (_stamp, data), token in zip(
                result, worker.last_output_force_requests, strict=True
            ):
                if token:
                    assert {5, 7, 8} <= {nal[0] & 31 for nal in split_annexb(data)}
                    acknowledgments.append(token)
            assert worker.last_pending_frames <= 1
        assert acknowledgments == [1, 2]
    finally:
        await worker.close()


def test_unresolved_native_force_queue_is_finite_without_erasing_old_requests():
    worker = Encoder(asdict(CONFIG))

    class EmptyDecoder:
        def decode(self, _packet):
            return []

    worker.decoder = EmptyDecoder()
    for token in range(1, MAX_FORCE_REQUESTS + 1):
        assert worker.feed(b"fixture", token * 3000, token) == ()
    assert list(worker.forces) == list(range(1, MAX_FORCE_REQUESTS + 1))
    with pytest.raises(ValueError):
        worker.feed(b"fixture", 27000, MAX_FORCE_REQUESTS + 1)
    assert list(worker.forces) == list(range(1, MAX_FORCE_REQUESTS + 1))
    assert len(worker.inputs) == MAX_FORCE_REQUESTS and not worker.pending


@pytest.mark.asyncio
async def test_unacknowledged_force_has_finite_input_convergence(monkeypatch):
    class EmptyWorker:
        last_output_force_requests = ()

        async def feed(self, *_args, **_kwargs):
            return ()

        async def close(self):
            pass

    monkeypatch.setattr(transcode, "VideoWorker", lambda _config: EmptyWorker())
    source = encode_source(profile="high", level="5.0", frames=1)[0][0]
    adapter = AccessUnitAdapter(CONFIG)
    try:
        for index in range(transcode.MAX_FORCE_INPUTS):
            assert await adapter.feed(source, index * 1000) == ()
        with pytest.raises(VideoTransformError, match="did not converge"):
            await adapter.feed(source, transcode.MAX_FORCE_INPUTS * 1000)
        assert len(adapter._pending_forces) == 1
        assert len(adapter._source_receipts) <= transcode.MAX_SOURCE_RECEIPTS
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_adapter_force_capacity_is_reserved_before_another_native_request(monkeypatch):
    class EmptyWorker:
        last_output_force_requests = ()
        calls = 0

        async def feed(self, *_args, **_kwargs):
            self.calls += 1
            return ()

        async def close(self):
            pass

    worker = EmptyWorker()
    monkeypatch.setattr(transcode, "VideoWorker", lambda _config: worker)
    source = encode_source(profile="high", level="5.0", frames=1)[0][0]
    adapter = AccessUnitAdapter(CONFIG)
    try:
        for index in range(transcode.MAX_NATIVE_PENDING):
            if index:
                adapter.request_keyframe()
            assert await adapter.feed(source, index * 1000) == ()
        adapter.request_keyframe()
        with pytest.raises(VideoTransformError, match="capacity is exhausted"):
            await adapter.feed(source, transcode.MAX_NATIVE_PENDING * 1000)
        assert worker.calls == len(adapter._pending_forces) == transcode.MAX_NATIVE_PENDING
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_probe_flushes_one_synthetic_frame_and_cannot_contaminate_later_live_input():
    worker = VideoWorker(CONFIG)
    try:
        result = await worker.probe()
        assert len(result) == 1 and result[0][0] == 0
        assert worker.last_output_force_requests == (1,) and worker.last_pending_frames == 0
        with pytest.raises(VideoTransformError, match="terminal"):
            await worker.feed(*encode_source(frames=1)[0])
        with pytest.raises(VideoTransformError, match="terminal"):
            await worker.probe()
    finally:
        await worker.close()


@pytest.mark.parametrize("full_range", [False, True])
def test_letterbox_preserves_known_color_metadata_and_source_display_aspect(full_range):
    frame = av.VideoFrame(320, 240, "yuv420p")
    frame.colorspace = frame.color_primaries = frame.color_trc = 1
    frame.color_range = 2 if full_range else 1
    for index, plane in enumerate(frame.planes):
        plane.update(bytes([180 if index == 0 else 128]) * plane.buffer_size)
    output = letterbox(frame, 640, 480, source_sar=Fraction(2))
    assert (output.width, output.height) == (640, 480)
    assert (output.colorspace, output.color_primaries, output.color_trc, output.color_range) == (
        1,
        1,
        1,
        frame.color_range,
    )
    data = output.to_ndarray(format="gray")
    assert abs(int(data[240, 320]) - (180 if full_range else 191)) <= 2
    assert int(data[0, 320]) <= 2


def test_native_output_groups_only_actual_packet_pts_and_rejects_unknown_origins():
    worker = Encoder(asdict(CONFIG))
    worker.pending = {0: 11, 6000: 12}

    def packet(payload, pts):
        result = av.Packet(payload)
        result.pts = result.dts = pts
        result.time_base = Fraction(1, 90000)
        return result

    assert worker.resolve([packet(b"a", 0), packet(b"b", 0), packet(b"c", 6000)]) == (
        (0, 11, b"ab"),
        (6000, 12, b"c"),
    )
    assert not worker.pending
    worker.pending = {12000: 13}
    with pytest.raises(ValueError):
        worker.resolve([packet(b"private", 999)])
    assert worker.pending == {12000: 13}


@pytest.mark.asyncio
@pytest.mark.parametrize("full_range", [False, True])
async def test_native_known_bt709_rgb_bars_range_and_nonsquare_source_pixels(full_range):
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = 320, 240, "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 90000), Fraction(15)
    encoder.sample_aspect_ratio = Fraction(2)
    encoder.colorspace = encoder.color_primaries = encoder.color_trc = 1
    encoder.color_range = 2 if full_range else 1
    encoder.thread_count, encoder.bit_rate, encoder.max_b_frames = 1, 2_000_000, 0
    encoder.options = {
        "preset": "veryfast",
        "tune": "zerolatency",
        "profile": "high",
        "level": "4.0",
    }
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    colors = ((0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255))
    for index, color in enumerate(colors):
        image[:, index * 64 : (index + 1) * 64] = color
    source = []
    for index in range(3):
        rgb = av.VideoFrame.from_ndarray(image, format="rgb24")
        frame = rgb.reformat(
            format="yuv420p",
            src_colorspace="ITU709",
            dst_colorspace="ITU709",
            src_color_range=2,
            dst_color_range=encoder.color_range,
            dst_color_primaries=1,
            dst_color_trc=1,
            threads=1,
        )
        frame.pts, frame.time_base = index * 6000, Fraction(1, 90000)
        source.extend(bytes(packet) for packet in encoder.encode(frame))
    assert len(source) == 3 and not encoder.encode(None)
    worker = VideoWorker(VideoConfiguration(640, 480, 30, "main", "3.1", 2000))
    decoded = []
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    try:
        for index, payload in enumerate(source):
            for stamp, output in await worker.feed(payload, index * 6000):
                decoded.extend(decoder.decode(av.Packet(output)))
                assert stamp == (len(decoded) - 1) * 6000
        assert len(decoded) == 2 and worker.last_pending_frames == 1
        frame = decoded[0]
        assert (frame.colorspace, frame.color_primaries, frame.color_trc, frame.color_range) == (
            1,
            1,
            1,
            2 if full_range else 1,
        )
        assert decoder.sample_aspect_ratio == 1
        rgb = frame.reformat(
            format="rgb24",
            src_colorspace="ITU709",
            dst_colorspace="ITU709",
            src_color_range=frame.color_range,
            dst_color_range=2,
            threads=1,
        ).to_ndarray()
        for index, color in enumerate(colors):
            assert np.max(np.abs(rgb[240, index * 128 + 64].astype(int) - np.asarray(color))) <= 8
        assert rgb[0].max() <= 2 and rgb[479].max() <= 2
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "width,height,kbps,minimum_psnr", [(640, 360, 132, 23), (1280, 720, 299, 20)]
)
async def test_moving_source15_selected30_has_real_source_time_budget_and_bounded_pending_tail(
    width, height, kbps, minimum_psnr
):
    source = encode_moving_source(15)
    adapter = AccessUnitAdapter(VideoConfiguration(width, height, 30, "high", "3.1", kbps))
    reference_decoder, decoder = (
        av.CodecContext.create("h264", "r"),
        av.CodecContext.create("h264", "r"),
    )
    reference_decoder.thread_count = decoder.thread_count = 1
    previous = None
    rows, errors = [], []
    try:
        for index, (stamp, payload) in enumerate(source):
            packet = av.Packet(payload)
            packet.pts = packet.dts = stamp
            packet.time_base = Fraction(1, 90000)
            frame = reference_decoder.decode(packet)[0]
            reference = (
                letterbox(frame, width, height).to_ndarray(format="yuv420p")[:height].astype(float)
            )
            outputs = await adapter.feed(
                payload, stamp, capture_epoch=2, received_at=100 + stamp / 90000
            )
            assert len(outputs) == (0 if index == 0 else 1)
            if outputs:
                output = outputs[0]
                assert output.timestamp == source[index - 1][0]
                assert output.received_at == 100 + output.timestamp / 90000
                decoded = decoder.decode(av.Packet(output.payload))
                assert len(decoded) == 1
                actual = decoded[0].to_ndarray(format="yuv420p")[:height].astype(float)
                errors.append(float(np.mean((previous - actual) ** 2)))
                rows.append((output.timestamp, len(output.payload)))
                if output.is_keyframe:
                    assert {5, 7, 8} <= {nal[0] & 31 for nal in split_annexb(output.payload)}
                    independent = av.CodecContext.create("h264", "r")
                    independent.thread_count = 1
                    assert len(independent.decode(av.Packet(output.payload))) == 1
            previous = reference
        assert len(rows) == len(source) - 1 and adapter._worker.last_pending_frames == 1
        assert [stamp for stamp, _count in rows] == [stamp for stamp, _data in source[:-1]]
        mean_codec = sum(count for _stamp, count in rows) * 8 / 6
        assert kbps * 1000 * 0.65 < mean_codec <= kbps * 1000
        windows = [
            (stamp, sum(count for other, count in rows if stamp - 90000 < other <= stamp) * 8)
            for stamp, _count in rows
        ]
        assert any(stamp < 90000 for stamp, _bits in windows)
        assert any(stamp >= 90000 for stamp, _bits in windows)
        assert max(bits for _stamp, bits in windows) <= kbps * 1000
        psnr = 10 * np.log10(255**2 / (sum(errors) / len(errors)))
        # A floor for this exact fixture; neither every frame/metric nor real
        # Ring/Home image quality is asserted to improve.
        assert psnr > minimum_psnr
    finally:
        await adapter.close()
