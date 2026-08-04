import shutil
import subprocess
import time
from fractions import Fraction
from threading import Event
from urllib.parse import urlsplit

import av
import numpy as np
import pytest

from halo_server.media import rtsp
from halo_server.media.audio import NormalizedOpusEncoder
from halo_server.media.buffer import EncodedMediaBuffer
from halo_server.media.rtsp import (
    BufferedMediaConsumer,
    RtspMediaGateway,
    _mount_path,
    _passthrough_launch,
)


def test_rtsp_launch_is_h264_passthrough_with_normalized_mono_opus():
    launch = _passthrough_launch()

    assert "video/x-h264,stream-format=byte-stream,alignment=au" in launch
    assert "audio/x-opus,channel-mapping-family=0,channels=1,rate=48000" in launch
    assert "h264parse" in launch
    assert "rtph264pay" in launch
    assert "rtpopuspay" in launch
    assert "x264enc" not in launch
    assert "opusenc" not in launch


def test_rtsp_launch_parses_with_installed_gstreamer():
    from halo_server.media.rtsp import Gst

    Gst.init(None)
    pipeline = Gst.parse_launch(_passthrough_launch())
    pipeline.set_state(Gst.State.NULL)


def test_rtsp_gateway_requires_loopback_and_valid_port():
    with pytest.raises(ValueError, match="loopback"):
        RtspMediaGateway(host="0.0.0.0", port=18554, open_consumer=lambda _id: None)
    with pytest.raises(ValueError, match="port"):
        RtspMediaGateway(host="127.0.0.1", port=65536, open_consumer=lambda _id: None)


def test_rtsp_gateway_fails_fast_when_required_gstreamer_element_is_missing(monkeypatch):
    gateway = RtspMediaGateway(
        host="127.0.0.1",
        port=0,
        open_consumer=lambda _id: None,
    )
    monkeypatch.setattr(rtsp, "_missing_rtsp_elements", lambda: ("h264parse",))

    with pytest.raises(rtsp.RtspGatewayError, match="h264parse"):
        gateway.start()

    assert gateway.bound_port is None


def test_rtsp_mount_does_not_expose_ring_camera_id():
    path = _mount_path("sensitive-ring-device-id")

    assert path.startswith("/halo/")
    assert "sensitive" not in path
    assert len(path) == len("/halo/") + 24


def test_rtsp_gateway_starts_syncs_and_returns_ephemeral_digest_url():
    gateway = RtspMediaGateway(
        host="127.0.0.1",
        port=0,
        open_consumer=lambda _id: None,
        password="temporary-secret",
    )
    try:
        gateway.start(["42"])

        descriptor = gateway.stream_descriptor("42")
        parsed = urlsplit(descriptor["url"])

        assert gateway.bound_port
        assert descriptor["prebuffer"] == 10_000
        assert descriptor["video"] == {"codec": "h264"}
        assert descriptor["audio"] == {"codec": "opus"}
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == gateway.bound_port
        assert parsed.username == "halo"
        assert parsed.password == "temporary-secret"
        assert parsed.path == _mount_path("42")

        previous_password = parsed.password
        gateway.rotate_credentials()
        rotated = urlsplit(gateway.stream_descriptor("42")["url"])
        assert rotated.password != previous_password

        gateway.sync_cameras(["43"])
        assert gateway.stream_descriptor("42") is None
        assert gateway.stream_descriptor("43") is not None
    finally:
        gateway.stop()


def test_rtsp_gateway_is_consumable_as_h264_and_opus():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    buffer = EncodedMediaBuffer()
    try:
        video_packets = _h264_packets()
    except Exception as exc:
        pytest.skip(f"test H.264 encoder is unavailable: {exc}")
    audio_encoder = NormalizedOpusEncoder()
    audio_packets = []
    silence = np.zeros(960, dtype="<i2").tobytes()
    for _index in range(150):
        audio_packets.extend(audio_encoder.encode(silence, 1, 48_000))
    for index, payload in enumerate(video_packets):
        buffer.append_video(payload, index * 3_000)
    for packet in audio_packets:
        buffer.append_audio(packet.payload, packet.rtp_timestamp)
    assert buffer.ready

    closed = Event()
    gateway = RtspMediaGateway(
        host="127.0.0.1",
        port=0,
        password="temporary-secret",
        open_consumer=lambda _camera_id: BufferedMediaConsumer(
            buffer=buffer,
            renew_callback=lambda: True,
            close_callback=closed.set,
        ),
    )
    try:
        gateway.start(["42"])
        descriptor = gateway.stream_descriptor("42")
        command = [
            ffmpeg,
            "-v",
            "info",
            "-rtsp_transport",
            "tcp",
            "-i",
            descriptor["url"],
            "-t",
            "1",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            pytest.fail(f"ffmpeg timed out:\n{exc.stderr or ''}")
    finally:
        gateway.stop()

    assert result.returncode == 0, result.stderr
    assert closed.wait(2)


def test_rtsp_gateway_waits_for_cold_camera_keyframe():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    buffer = EncodedMediaBuffer()
    try:
        video_packets = _h264_packets()
    except Exception as exc:
        pytest.skip(f"test H.264 encoder is unavailable: {exc}")
    audio_encoder = NormalizedOpusEncoder()
    audio_packets = []
    silence = np.zeros(960, dtype="<i2").tobytes()
    for _index in range(150):
        audio_packets.extend(audio_encoder.encode(silence, 1, 48_000))
    closed = Event()
    gateway = RtspMediaGateway(
        host="127.0.0.1",
        port=0,
        password="temporary-secret",
        open_consumer=lambda _camera_id: BufferedMediaConsumer(
            buffer=buffer,
            renew_callback=lambda: True,
            close_callback=closed.set,
        ),
    )
    try:
        gateway.start(["42"])
        descriptor = gateway.stream_descriptor("42")
        process = subprocess.Popen(
            [
                ffmpeg,
                "-v",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                descriptor["url"],
                "-t",
                "1",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(3)
        for index, payload in enumerate(video_packets):
            buffer.append_video(payload, index * 3_000)
        for packet in audio_packets:
            buffer.append_audio(packet.payload, packet.rtp_timestamp)
        _stdout, stderr = process.communicate(timeout=10)
    finally:
        gateway.stop()

    assert process.returncode == 0, stderr
    assert closed.wait(2)


def _h264_packets() -> list[bytes]:
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width = 320
    encoder.height = 240
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 30)
    encoder.framerate = Fraction(30, 1)
    encoder.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "x264-params": "keyint=30:min-keyint=30:scenecut=0",
    }
    encoder.open()
    packets = []
    for index in range(90):
        pixels = np.full((240, 320, 3), index % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
        frame.pts = index
        packets.extend(bytes(packet) for packet in encoder.encode(frame))
    packets.extend(bytes(packet) for packet in encoder.encode(None))
    return [packet for packet in packets if packet]
