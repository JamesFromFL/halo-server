"""Measured codec reserve is separate from cadence-transition monitoring."""

from dataclasses import asdict
from fractions import Fraction

import av
import pytest
from test_video_wire_encoder import encode_moving_source

from halo_server.media.h264 import VideoConfiguration, split_annexb
from halo_server.media.transcode_worker import Encoder, open_encoder
from halo_server.media.video_budget import CodecRateMonitor


@pytest.mark.parametrize("initial", (13, 18))
def test_reserve_includes_startup_corrective_and_consecutive_forced_idr_windows(initial):
    config = VideoConfiguration(640, 360, 30, "high", "3.1", 132)
    source = encode_moving_source(15)
    owner = Encoder(asdict(config))
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    rows, refreshes = [], []
    stamp = 0
    seed = initial * 2
    for index in range(seed + 165):
        if index:
            stamp += 90000 // initial if index < seed else 6000
        request = index + 1 if index in {3, seed + 95, seed + 96} else 0
        previous = owner.refreshes
        outputs = owner.feed(source[index % len(source)][1], stamp, request)
        assert len(outputs) == 1 and outputs[0][:2] == (stamp, request)
        assert not owner.pending
        output = outputs[0][2]
        if request or owner.refreshes != previous:
            assert {5, 7, 8} <= {nal[0] & 31 for nal in split_annexb(output)}
        if owner.refreshes != previous:
            refreshes.append(owner.nominal_fps)
        assert len(decoder.decode(av.Packet(output))) == 1
        rows.append((stamp, len(output)))
    assert refreshes == [initial, 15]
    assert not owner.inputs and not owner.forces
    windows = [
        (stamp, sum(count for other, count in rows if stamp - 90000 < other <= stamp) * 8)
        for stamp, _count in rows
    ]
    assert any(stamp < 90000 for stamp, _bits in windows)
    assert any(stamp >= 90000 for stamp, _bits in windows)
    assert max(bits for _stamp, bits in windows) <= config.max_bit_rate * 1000


def test_true_cadence_change_keeps_pictures_and_monitor_reports_actual_source_windows():
    # A fixed-CFR nominal can lag a genuine source-rate increase. This test
    # proves honest monitoring and content preservation, not universal window
    # compliance. The independent work counterexample remains documented.
    config = VideoConfiguration(640, 360, 30, "high", "3.1", 132)
    source = encode_moving_source(30)
    owner, monitor = Encoder(asdict(config)), CodecRateMonitor()
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    rows = []
    for index in range(340):
        stamp = index * 6000 if index < 45 else 270000 + (index - 45) * 3000
        outputs = owner.feed(source[index % len(source)][1], stamp, 0)
        assert len(outputs) == 1 and outputs[0][0] == stamp and not owner.pending
        assert len(decoder.decode(av.Packet(outputs[0][2]))) == 1
        size = len(outputs[0][2])
        rows.append((stamp, size))
        monitor.observe(
            epoch=1, generation=1, selected_kbps=132, source_pts=stamp, encoded_bytes=size
        )
    assert len(rows) == 340 and owner.nominal_fps == 30 and owner.refreshes == 2
    windows = [
        sum(size for other, size in rows if stamp - 90000 < other <= stamp) * 8
        for stamp, _size in rows
    ]
    status = monitor.snapshot()
    assert status["unknown_windows"] == 0
    assert status["peak_window_payload_bits"] == max(windows)
    assert status["above_selected_windows"] == sum(bits > 132000 for bits in windows)


def test_maximum_advertised_output_preserves_frozen_pre_reserve_native_bytes():
    # This compatibility oracle deliberately reproduces the pre-R7f public
    # encoder setup. Actual NAL-byte equality catches accidental preset/VBV
    # changes above 720p without a fragile wall-time assertion in unit tests.
    config = asdict(VideoConfiguration(1920, 1080, 30, "high", "4.0", 2000))
    reference = av.CodecContext.create("libx264", "w")
    reference.width, reference.height, reference.pix_fmt = 1920, 1080, "yuv420p"
    reference.time_base, reference.framerate = Fraction(1, 90000), Fraction(30)
    reference.bit_rate, reference.thread_count = 1_640_000, 1
    reference.gop_size, reference.max_b_frames = 60, 0
    reference.sample_aspect_ratio = Fraction(1)
    reference.options = {
        "preset": "veryfast",
        "tune": "zerolatency",
        "profile": "high",
        "level": "4.0",
        "forced-idr": "1",
        "x264-params": "force-cfr=1:bframes=0:scenecut=0:repeat-headers=1:keyint=60:"
        "vbv-maxrate=1640:vbv-bufsize=820",
    }
    reference.open()
    actual = open_encoder(config, nominal_fps=30)
    independent = av.CodecContext.create("h264", "r")
    independent.thread_count = 1
    for index in range(9):
        frames = [av.VideoFrame(1920, 1080, "yuv420p") for _ in range(2)]
        for frame in frames:
            for plane_index, plane in enumerate(frame.planes):
                plane.update(bytes([70 + index if plane_index == 0 else 128]) * plane.buffer_size)
            frame.pts, frame.time_base = index * 3000, Fraction(1, 90000)
            frame.pict_type = (
                av.video.frame.PictureType.I if index in {0, 4} else av.video.frame.PictureType.NONE
            )
        expected, encoded = reference.encode(frames[0]), actual.encode(frames[1])
        assert len(expected) == len(encoded) == 1
        assert encoded[0].pts == expected[0].pts == index * 3000
        assert encoded[0].dts == expected[0].dts
        assert bytes(encoded[0]) == bytes(expected[0])
        decoded = independent.decode(encoded[0])
        assert len(decoded) == 1 and (decoded[0].width, decoded[0].height) == (1920, 1080)
    assert not reference.encode(None) and not actual.encode(None)
