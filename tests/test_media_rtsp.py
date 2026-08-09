import shutil
import subprocess
import time
from fractions import Fraction
from threading import Event, Thread
from urllib.parse import urlsplit

import av
import numpy as np
import pytest

from halo_server.media import rtsp
from halo_server.media.audio import NormalizedOpusEncoder
from halo_server.media.buffer import EncodedMediaBuffer, h264_nal_types
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


def test_rtsp_factory_requires_interleaved_tcp_transport():
    factory = rtsp._BufferedCameraFactory("42", lambda _camera_id: None)

    assert factory.get_protocols() == rtsp.GstRtsp.RTSPLowerTrans.TCP


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


def test_media_configure_does_not_wait_for_consumer_acquisition():
    rtsp.Gst.init(None)
    open_started = Event()
    allow_open = Event()
    consumer_closed = Event()
    callback_returned = Event()

    class FakeSource:
        def __init__(self):
            self.signals = []

        def emit(self, signal, *_args):
            self.signals.append(signal)
            return rtsp.Gst.FlowReturn.OK

    video_source = FakeSource()
    audio_source = FakeSource()

    class FakeElement:
        def get_by_name(self, name):
            return video_source if name == "vsrc" else audio_source

    class FakeMedia:
        def __init__(self):
            self.handlers = {}

        def get_element(self):
            return FakeElement()

        def connect(self, signal, callback):
            self.handlers[signal] = callback

    media = FakeMedia()

    def open_consumer(_camera_id):
        open_started.set()
        assert allow_open.wait(1)
        return BufferedMediaConsumer(
            buffer=object(),
            renew_callback=lambda: True,
            close_callback=consumer_closed.set,
        )

    factory = rtsp._BufferedCameraFactory("42", open_consumer)

    def configure():
        factory._on_media_configure(None, media)
        callback_returned.set()

    context_thread = Thread(target=configure)
    context_thread.start()
    try:
        assert open_started.wait(0.5)
        assert callback_returned.wait(0.2)
        media.handlers["unprepared"](media)
    finally:
        allow_open.set()
        context_thread.join(timeout=1)
        factory.stop_workers()

    assert not context_thread.is_alive()
    assert consumer_closed.wait(0.5)
    assert video_source.signals == ["end-of-stream"]
    assert audio_source.signals == ["end-of-stream"]


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


def test_rtsp_gateway_replays_decoder_boundary_for_late_client():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    try:
        video_packets = _h264_packets(frame_count=180, keyframe_interval=300)
    except Exception as exc:
        pytest.skip(f"test H.264 encoder is unavailable: {exc}")

    buffer = EncodedMediaBuffer()
    opened_consumers = []

    def open_consumer(_camera_id):
        consumer = BufferedMediaConsumer(
            buffer=buffer,
            renew_callback=lambda: True,
            close_callback=lambda: None,
        )
        opened_consumers.append(consumer)
        return consumer

    gateway = RtspMediaGateway(
        host="127.0.0.1",
        port=0,
        password="temporary-secret",
        open_consumer=open_consumer,
    )
    producer_stop = Event()
    first_client = None
    producer = None
    try:
        gateway.start(["42"])
        descriptor = gateway.stream_descriptor("42")
        first_client = subprocess.Popen(
            [
                ffmpeg,
                "-v",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                descriptor["url"],
                "-t",
                "5",
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
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def feed_media():
            audio_encoder = NormalizedOpusEncoder()
            silence = np.zeros(960, dtype="<i2").tobytes()
            started = time.monotonic()
            for index, payload in enumerate(video_packets):
                delay = started + index / 30 - time.monotonic()
                if producer_stop.wait(max(0.0, delay)):
                    return
                buffer.append_video(payload, index * 3_000)
                for _audio_frame in range(2):
                    for packet in audio_encoder.encode(silence, 1, 48_000):
                        buffer.append_audio(packet.payload, packet.rtp_timestamp)

        producer = Thread(target=feed_media, daemon=True)
        producer.start()

        deadline = time.monotonic() + 5
        while len(opened_consumers) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened_consumers) == 1
        time.sleep(1.5)

        late_client = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                descriptor["url"],
                "-t",
                "0.5",
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "copy",
                "-f",
                "h264",
                "-",
            ],
            capture_output=True,
            check=False,
            timeout=8,
        )
    finally:
        producer_stop.set()
        if producer is not None:
            producer.join(timeout=2)
        if first_client is not None:
            first_client.terminate()
            try:
                first_client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                first_client.kill()
                first_client.wait(timeout=3)
        gateway.stop()

    assert late_client.returncode == 0, late_client.stderr.decode(errors="replace")
    assert len(opened_consumers) == 2
    assert 5 in h264_nal_types(late_client.stdout)


def _h264_packets(
    *,
    frame_count: int = 90,
    keyframe_interval: int = 30,
) -> list[bytes]:
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width = 320
    encoder.height = 240
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 30)
    encoder.framerate = Fraction(30, 1)
    encoder.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "x264-params": (f"keyint={keyframe_interval}:min-keyint={keyframe_interval}:scenecut=0"),
    }
    encoder.open()
    packets = []
    for index in range(frame_count):
        pixels = np.full((240, 320, 3), index % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
        frame.pts = index
        packets.extend(bytes(packet) for packet in encoder.encode(frame))
    packets.extend(bytes(packet) for packet in encoder.encode(None))
    return [packet for packet in packets if packet]
