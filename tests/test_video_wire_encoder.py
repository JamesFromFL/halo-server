"""Native moving-scene output must leave room for actual RTP/SRTP serialization."""

from fractions import Fraction

import av
import numpy as np
import pytest

from halo_server.media.h264 import VideoConfiguration, parse_sps, split_annexb
from halo_server.media.rtp import H264Packetizer
from halo_server.media.srtp import SecureVideo
from halo_server.media.transcode import VideoWorker


def encode_moving_source(fps):
    """Six seconds of 720p gradient/checker motion and a moving colored rectangle."""
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = 1280, 720
    encoder.pix_fmt = "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 90000), Fraction(fps)
    encoder.thread_count, encoder.bit_rate = 1, 8_000_000
    encoder.gop_size, encoder.max_b_frames = fps * 2, 0
    encoder.options = {
        "preset": "veryfast",
        "tune": "zerolatency",
        "profile": "high",
        "level": "4.0",
        "x264-params": f"threads=1:bframes=0:scenecut=0:repeat-headers=1:keyint={fps * 2}",
    }
    encoder.open()
    assert not encoder.options
    yy, xx = np.indices((720, 1280))
    result = []
    for index in range(fps * 6):
        image = np.empty((720, 1280, 3), dtype=np.uint8)
        image[:, :, 0] = ((xx + index * 7) % 256).astype(np.uint8)
        image[:, :, 1] = ((yy * 2 + index * 3) % 256).astype(np.uint8)
        image[:, :, 2] = (((xx // 12 + yy // 12 + index) % 2) * 200 + 25).astype(np.uint8)
        left = (index * 19) % (1280 - 90)
        image[100:220, left : left + 90, :] = [240, 30, 110]
        frame = av.VideoFrame.from_ndarray(image, format="rgb24")
        frame = frame.reformat(format="yuv420p", threads=1)
        frame.pts, frame.time_base = index * 90000 // fps, Fraction(1, 90000)
        packets = encoder.encode(frame)
        assert len(packets) == 1
        result.append((frame.pts, bytes(packets[0])))
    assert not encoder.encode(None)
    return tuple(result)


@pytest.fixture(scope="module", params=(15, 30))
def moving_source(request):
    return request.param, encode_moving_source(request.param)


@pytest.mark.asyncio
@pytest.mark.parametrize("width,height,kbps", [(640, 360, 132), (1280, 720, 299)])
async def test_native_moving_video_has_small_decodable_idrs_and_bounded_protected_rate(
    moving_source, width, height, kbps
):
    fps, source = moving_source
    worker = VideoWorker(VideoConfiguration(width, height, 30, "high", "3.1", kbps, mtu=1378))
    packetizer = H264Packetizer(123, sequence=65500)
    secure = SecureVideo(b"k" * 16, b"s" * 14, 123)
    continuous = av.CodecContext.create("h264", "r")
    continuous.thread_count = 1
    rows, codec_rows, idrs, decoded_count = [], [], [], 0
    try:
        for index, (timestamp, payload) in enumerate(source):
            encoded = await worker.feed(payload, timestamp)
            assert len(encoded) == (0 if index == 0 else 1)
            if not encoded:
                continue
            assert encoded[0][0] == source[index - 1][0]
            timestamp = encoded[0][0]
            output = encoded[0][1]
            nals = split_annexb(output)
            types = {nal[0] & 31 for nal in nals}
            if 5 in types:
                assert {5, 7, 8} <= types
                sps = parse_sps(next(nal for nal in nals if nal[0] & 31 == 7))
                assert (sps.width, sps.height, sps.profile, sps.level) == (
                    width,
                    height,
                    "high",
                    "3.1",
                )
                independent = av.CodecContext.create("h264", "r")
                independent.thread_count = 1
                decoded = independent.decode(av.Packet(output))
                assert len(decoded) == 1 and (decoded[0].width, decoded[0].height) == (
                    width,
                    height,
                )
            decoded = continuous.decode(av.Packet(output))
            assert len(decoded) == 1 and (decoded[0].width, decoded[0].height) == (width, height)
            decoded_count += len(decoded)
            codec_rows.append((timestamp, len(output)))
            protected_bytes = 0
            for fragment in packetizer.packetize(output, timestamp, payload_type=99, mtu=1378):
                protected = secure.protect(fragment.packet)
                assert len(protected) == len(fragment.packet) + 10 and len(protected) <= 1200
                protected_bytes += len(protected)
            rows.append((timestamp / 90000, protected_bytes))
            if 5 in types:
                idrs.append((timestamp, protected_bytes))
        assert decoded_count == len(source) - 1 == fps * 6 - 1
        assert worker.last_pending_frames == 1
        assert [timestamp for timestamp, _count in idrs] == [0, 180000, 360000]
        # Startup partial windows can already establish an overshoot. Include
        # them as well as all covered one-second windows over original PTS.
        codec_windows = [
            (stamp, sum(count for other, count in codec_rows if stamp - 90000 < other <= stamp) * 8)
            for stamp, _count in codec_rows
        ]
        assert any(stamp < 90000 for stamp, _bits in codec_windows)
        assert any(stamp >= 90000 for stamp, _bits in codec_windows)
        assert max(bits for _stamp, bits in codec_windows) <= kbps * 1000
        # The selected limit describes codec bytes over one second. This
        # separate finite peak is the approved receiver-first transport model,
        # not an assertion that the codec limit is the physical wire speed.
        wire_rate = min(64_000_000, max(2_000_000, kbps * 1000 * 4))
        assert all(count * 8 / wire_rate < 0.2 for _timestamp, count in idrs)
        assert sum(count for _timestamp, count in rows) * 8 / 6 <= kbps * 1000

        # Charge the native pending frame in addition to the explicit 250 ms
        # original-source age and 50 ms conversion allowance. This verifies
        # representative headroom, not wall-time guarantees on arbitrary hosts.
        next_packet = 0
        for received, count in rows:
            ready = received + 0.250 + 0.050 + 1 / fps
            finish = max(ready, next_packet) + count * 8 / wire_rate
            assert finish <= received + 0.5
            next_packet = finish
        # Independent 250 ms age + 250 ms backlog already exhausts the budget:
        # no encoder setting can truthfully admit a positive-size AU here.
        assert 0.250 + 0.250 + rows[0][1] * 8 / wire_rate > 0.5
    finally:
        await worker.close()
        secure.close()
    assert worker.pid is None and not worker._reserved
