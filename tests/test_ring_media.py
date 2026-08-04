"""Lifecycle tests for the headless Ring WebRTC receiver."""

import asyncio
import queue
import threading
from types import SimpleNamespace

import aiortc
import pytest
from aiortc.mediastreams import MediaStreamError
from ring_doorbell.webrtcstream import RingWebRtcStream

from halo_server.media import ring_media


class FakePeerConnection:
    def __init__(self):
        self.handlers = {}
        self.localDescription = None
        self.closed = False
        self.answer_started = asyncio.Event()
        self.answer_cancelled = False

    def addTransceiver(self, track_or_kind, **_kwargs):
        kind = track_or_kind if isinstance(track_or_kind, str) else track_or_kind.kind
        return SimpleNamespace(kind=kind, receiver=object())

    def on(self, event):
        def register(callback):
            self.handlers[event] = callback
            return callback

        return register

    async def createOffer(self):
        return SimpleNamespace(sdp="offer", type="offer")

    async def setLocalDescription(self, offer):
        self.localDescription = offer

    async def setRemoteDescription(self, _answer):
        self.answer_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.answer_cancelled = True
            raise

    async def addIceCandidate(self, _candidate):
        pass

    async def close(self):
        self.closed = True


class FakeMicrophoneTrack:
    kind = "audio"
    instances = []

    def __init__(self, source=None):
        self.source = source
        self.stopped = False
        self.instances.append(self)

    def stop(self):
        self.stopped = True


class FakeDevice:
    name = "Test Camera"

    def __init__(self, on_generate=None):
        self.on_generate = on_generate
        self.generated = False
        self.closed_sessions = []

    def get_ice_servers(self):
        return []

    async def generate_async_webrtc_stream(
        self,
        _sdp,
        session_id,
        on_rtc_message,
        *,
        keep_alive_timeout,
    ):
        self.generated = True
        assert keep_alive_timeout == 300
        if self.on_generate is not None:
            await self.on_generate(session_id, on_rtc_message)

    async def close_webrtc_stream(self, session_id):
        self.closed_sessions.append(session_id)

    async def on_webrtc_candidate(self, *_args):
        pass


def test_microphone_pcm_buffer_is_fixed_size_and_drops_oldest_frame():
    source = ring_media.MicrophonePcmBuffer(max_frames=2)
    first = b"\x01" * ring_media.MIC_FRAME_BYTES
    second = b"\x02" * ring_media.MIC_FRAME_BYTES
    third = b"\x03" * ring_media.MIC_FRAME_BYTES

    with pytest.raises(ValueError, match="exactly"):
        source.push(b"short")
    source.push(first)
    source.push(second)
    source.push(third)

    assert source.pull() == second
    assert source.pull() == third
    assert source.pull() is None
    assert source.snapshot() == ring_media.MicrophonePcmSnapshot(
        queued_frames=0,
        accepted_frames=3,
        consumed_frames=2,
        dropped_frames=1,
    )

    source.clear(reset_statistics=True)
    assert source.snapshot() == ring_media.MicrophonePcmSnapshot(0, 0, 0, 0)


def test_microphone_pcm_buffer_gates_intercom_until_speaker_is_ready():
    source = ring_media.MicrophonePcmBuffer(max_frames=2)
    generation = source.begin_intercom()
    payload = b"\x01\x00" * ring_media.MIC_SAMPLES

    assert not source.push_intercom(generation + 1, payload)
    assert source.push_intercom(generation, payload)

    assert source.pull() is None
    assert not source.set_speaker_ready(generation + 1, True)
    assert source.set_speaker_ready(generation, True)
    assert source.pull() == payload

    source.end_intercom()
    snapshot = source.snapshot()
    assert snapshot.intercom_active is False
    assert snapshot.speaker_ready is False
    assert snapshot.queued_frames == 0


def test_microphone_pcm_buffer_rearms_speaker_gate_when_transport_clears():
    source = ring_media.MicrophonePcmBuffer(max_frames=2)
    generation = source.begin_intercom()
    payload = b"\x01\x00" * ring_media.MIC_SAMPLES
    source.push(payload)
    assert source.set_speaker_ready(generation, True)

    source.clear()
    source.push(payload)

    snapshot = source.snapshot()
    assert snapshot.intercom_active is True
    assert snapshot.intercom_generation == generation
    assert snapshot.speaker_ready is False
    assert source.pull() is None
    assert source.set_speaker_ready(generation, True)
    assert source.pull() == payload


def test_microphone_pcm_buffer_reports_speaker_readiness_and_terminal_failure():
    source = ring_media.MicrophonePcmBuffer()
    generation = source.begin_intercom()

    assert source.set_speaker_ready(generation, True)
    assert source.wait_speaker_ready(generation, 0.1) is True

    source.clear()
    assert source.set_speaker_failure(generation, "activation_failed")
    assert source.wait_speaker_ready(generation, 0.1) is False
    assert source.snapshot().speaker_failure == "activation_failed"

    source.end_intercom()
    assert source.wait_speaker_ready(generation, 0.1) is False

    for timeout in (0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError, match="timeout"):
            source.wait_speaker_ready(generation, timeout)


def test_sdp_diagnostics_report_codecs_without_network_or_session_data():
    sdp = "\r\n".join(
        [
            "v=0",
            "o=- 1234 2 IN IP4 192.0.2.4",
            "m=video 9 UDP/TLS/RTP/SAVPF 102",
            "a=rtpmap:102 H264/90000",
            "a=fmtp:102 packetization-mode=1;profile-level-id=640032",
            "a=candidate:secret 1 udp 1 192.0.2.4 1234 typ host",
            "m=audio 9 UDP/TLS/RTP/SAVPF 111",
            "a=rtpmap:111 opus/48000/2",
            "a=sendonly",
        ]
    )

    summary = ring_media.summarize_sdp_codecs(sdp)

    assert summary == "video:h264,audio:opus,video:h264-profile-640032"
    assert "192.0.2.4" not in summary
    assert "secret" not in summary
    assert ring_media.summarize_sdp_audio_direction(sdp) == "sendonly"


@pytest.mark.asyncio
async def test_audio_sender_snapshot_reports_content_free_rtp_counters():
    class Sender:
        async def getStats(self):
            return {
                "outbound": SimpleNamespace(
                    type="outbound-rtp",
                    kind="audio",
                    packetsSent=12,
                    bytesSent=345,
                ),
                "remote": SimpleNamespace(
                    type="remote-inbound-rtp",
                    kind="audio",
                    packetsReceived=11,
                    packetsLost=1,
                    fractionLost=2,
                    jitter=3,
                    roundTripTime=0.125,
                ),
            }

    assert await ring_media.audio_sender_snapshot(Sender()) == (
        ring_media.AudioSenderSnapshot(
            packets_sent=12,
            bytes_sent=345,
            remote_packets_received=11,
            remote_packets_lost=1,
            remote_fraction_lost=2,
            remote_jitter=3,
            round_trip_time=0.125,
        )
    )


@pytest.mark.asyncio
async def test_intercom_speaker_controller_gates_pcm_and_latches_activation(
    monkeypatch,
    caplog,
):
    caplog.set_level("INFO")
    source = ring_media.MicrophonePcmBuffer()
    stop = threading.Event()
    terminal = asyncio.Event()
    activated = asyncio.Event()
    calls = []

    async def activate_speaker(_device, session_id):
        calls.append(session_id)
        activated.set()
        return True

    class Sender:
        def __init__(self):
            self.calls = 0

        async def getStats(self):
            self.calls += 1
            return {
                "outbound": SimpleNamespace(
                    type="outbound-rtp",
                    kind="audio",
                    packetsSent=self.calls * 10,
                    bytesSent=self.calls * 100,
                )
            }

    monkeypatch.setattr(
        ring_media,
        "activate_ring_camera_speaker",
        activate_speaker,
    )
    task = asyncio.create_task(
        ring_media.manage_intercom_speaker(
            SimpleNamespace(),
            "halo-session",
            "camera",
            "session",
            source,
            Sender(),
            SimpleNamespace(currentDirection="sendrecv"),
            SimpleNamespace(connectionState="connected"),
            stop,
            terminal,
        )
    )

    generation = source.begin_intercom()
    payload = b"\x01\x00" * ring_media.MIC_SAMPLES
    source.push(payload)
    assert source.pull() is None

    await asyncio.wait_for(activated.wait(), timeout=0.5)
    for _ in range(20):
        if source.snapshot().speaker_ready:
            break
        await asyncio.sleep(0.01)
    assert source.snapshot().speaker_ready
    assert source.pull() == payload

    source.end_intercom()
    for _ in range(20):
        if "packets_sent=10 bytes_sent=100" in caplog.text:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=0.5)

    assert generation == 1
    assert calls == ["halo-session"]
    assert "packets_sent=10 bytes_sent=100" in caplog.text
    assert "speaker_latched=True" in caplog.text


@pytest.mark.asyncio
async def test_intercom_speaker_controller_retries_transient_activation_failure(monkeypatch):
    source = ring_media.MicrophonePcmBuffer()
    stop = threading.Event()
    terminal = asyncio.Event()
    attempts = 0

    async def activate_speaker(_device, _session_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ring_media.RingSpeakerControlError("temporary signaling failure")
        return True

    class Sender:
        async def getStats(self):
            return {}

    monkeypatch.setattr(ring_media, "activate_ring_camera_speaker", activate_speaker)
    task = asyncio.create_task(
        ring_media.manage_intercom_speaker(
            SimpleNamespace(),
            "halo-session",
            "camera",
            "session",
            source,
            Sender(),
            SimpleNamespace(currentDirection="sendrecv"),
            SimpleNamespace(connectionState="connected"),
            stop,
            terminal,
            activation_timeout=0.25,
            retry_interval=0.01,
            peer_ready_timeout=0.01,
        )
    )
    generation = source.begin_intercom()
    source.push(b"\x01\x00" * ring_media.MIC_SAMPLES)

    assert await asyncio.to_thread(source.wait_speaker_ready, generation, 0.5)

    stop.set()
    await asyncio.wait_for(task, timeout=0.5)
    assert attempts == 2


@pytest.mark.asyncio
async def test_intercom_sender_statistics_failure_does_not_block_activation(monkeypatch):
    source = ring_media.MicrophonePcmBuffer()
    stop = threading.Event()
    terminal = asyncio.Event()

    async def activate_speaker(_device, _session_id):
        return True

    class Sender:
        async def getStats(self):
            raise RuntimeError("statistics unavailable")

    monkeypatch.setattr(ring_media, "activate_ring_camera_speaker", activate_speaker)
    task = asyncio.create_task(
        ring_media.manage_intercom_speaker(
            SimpleNamespace(),
            "halo-session",
            "camera",
            "session",
            source,
            Sender(),
            SimpleNamespace(currentDirection="sendrecv"),
            SimpleNamespace(connectionState="connected"),
            stop,
            terminal,
            activation_timeout=0.1,
            retry_interval=0.01,
            peer_ready_timeout=0.01,
        )
    )
    generation = source.begin_intercom()
    source.push(b"\x01\x00" * ring_media.MIC_SAMPLES)

    assert await asyncio.to_thread(source.wait_speaker_ready, generation, 0.5)

    source.end_intercom()
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_intercom_speaker_controller_publishes_bounded_activation_failure(monkeypatch):
    source = ring_media.MicrophonePcmBuffer()
    stop = threading.Event()
    terminal = asyncio.Event()

    async def fail_activation(_device, _session_id):
        raise ring_media.RingSpeakerControlError("camera rejected speaker command")

    class Sender:
        async def getStats(self):
            return {}

    monkeypatch.setattr(ring_media, "activate_ring_camera_speaker", fail_activation)
    task = asyncio.create_task(
        ring_media.manage_intercom_speaker(
            SimpleNamespace(),
            "halo-session",
            "camera",
            "session",
            source,
            Sender(),
            SimpleNamespace(currentDirection="sendrecv"),
            SimpleNamespace(connectionState="connected"),
            stop,
            terminal,
            activation_timeout=0.05,
            retry_interval=0.005,
            peer_ready_timeout=0.005,
        )
    )
    generation = source.begin_intercom()
    source.push(b"\x01\x00" * ring_media.MIC_SAMPLES)

    assert not await asyncio.to_thread(source.wait_speaker_ready, generation, 0.5)
    assert source.snapshot().speaker_failure == "camera_rejected_speaker_command"

    stop.set()
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_microphone_track_sends_queued_pcm_before_silence():
    source = ring_media.MicrophonePcmBuffer()
    payload = b"\x04\x00" * ring_media.MIC_SAMPLES
    source.push(payload)
    track = ring_media.MicrophoneTrack(source)

    frame = await track.recv()

    assert bytes(frame.planes[0]) == payload


@pytest.mark.asyncio
async def test_ring_doorbell_natural_close_does_not_await_reader_task():
    stream = RingWebRtcStream(SimpleNamespace(), 1)
    callback_messages = []
    close_callbacks = 0

    async def close_callback():
        nonlocal close_callbacks
        close_callbacks += 1
        await stream.close()

    stream._on_close_callback = close_callback
    stream._on_message_callback = callback_messages.append
    stream.read_task = asyncio.current_task()

    await stream.handle_close_message(
        {
            "body": {
                "reason": {
                    "code": "stream_timeout",
                    "text": "Live View ended",
                }
            }
        }
    )

    assert close_callbacks == 1
    assert stream.read_task is None
    assert stream.is_alive is False
    assert callback_messages[0].error_code == "stream_timeout"


def test_ring_doorbell_close_patch_rejects_unvalidated_version(monkeypatch):
    monkeypatch.setattr(ring_media.importlib_metadata, "version", lambda _name: "0.9.15")

    assert ring_media.patch_ring_doorbell_webrtc_close() is False


def test_ring_doorbell_close_patch_rejects_changed_contract(monkeypatch):
    async def changed_close(self, *, closed_by_self):
        self.is_alive = not closed_by_self

    monkeypatch.setattr(RingWebRtcStream, "_close", changed_close)
    monkeypatch.delattr(
        RingWebRtcStream,
        "_halo_reader_self_await_patch_applied",
        raising=False,
    )

    assert ring_media.patch_ring_doorbell_webrtc_close() is False


@pytest.mark.asyncio
async def test_microphone_track_never_opens_a_server_local_capture_device(monkeypatch):
    monkeypatch.setattr(
        ring_media.Gst,
        "parse_launch",
        lambda _description: (_ for _ in ()).throw(AssertionError("local capture opened")),
    )
    track = ring_media.MicrophoneTrack()

    frame = await track.recv()

    assert bytes(frame.planes[0]) == bytes(frame.planes[0].buffer_size)


@pytest.mark.asyncio
async def test_microphone_frame_pacing_uses_monotonic_clock(monkeypatch):
    monkeypatch.setattr(
        ring_media.time,
        "time",
        lambda: (_ for _ in ()).throw(AssertionError("wall clock used")),
    )
    monkeypatch.setattr(ring_media.time, "monotonic", lambda: 100.0)
    track = ring_media.MicrophoneTrack()

    frame = await track.recv()

    assert frame.samples == ring_media.MIC_SAMPLES


@pytest.mark.asyncio
async def test_aiortc_connect_task_is_retained_for_matching_peer_only():
    class Peer:
        def __init__(self):
            self.started = asyncio.Event()

        async def __connect(self):
            self.started.set()
            await asyncio.Event().wait()

        def start(self):
            return asyncio.create_task(self.__connect())

    peer = Peer()
    other = Peer()
    peer_task = peer.start()
    other_task = other.start()
    await peer.started.wait()
    await other.started.wait()
    retained = []

    ring_media.retain_aiortc_connect_tasks(
        peer,
        lambda task, label: retained.append((task, label)),
    )

    assert retained == [(peer_task, "aiortc connection")]
    peer_task.cancel()
    other_task.cancel()
    await asyncio.gather(peer_task, other_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_encoded_frame_adapter_captures_h264_without_decoding():
    peer = aiortc.RTCPeerConnection()
    transceiver = peer.addTransceiver("video", direction="recvonly")
    captured = []
    try:
        adapter = ring_media.install_encoded_frame_adapter(
            transceiver.receiver,
            "video",
            lambda data, timestamp: captured.append((data, timestamp)),
            forward_to_decoder=False,
        )
        codec = SimpleNamespace(mimeType="video/H264")
        frame = SimpleNamespace(data=b"\x00\x00\x00\x01\x65frame", timestamp=90_000)

        adapter.put((codec, frame))
        adapter.put(None)

        assert captured == [(frame.data, 90_000)]
        assert adapter.get_nowait() is None
        with pytest.raises(queue.Empty):
            adapter.get_nowait()
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_encoded_frame_adapter_forwards_unsupported_codec_to_decoder():
    peer = aiortc.RTCPeerConnection()
    transceiver = peer.addTransceiver("video", direction="recvonly")
    incompatible = []
    try:
        adapter = ring_media.install_encoded_frame_adapter(
            transceiver.receiver,
            "video",
            lambda *_args: pytest.fail("VP8 must not be emitted as H.264"),
            forward_to_decoder=True,
            on_incompatible=lambda kind, mime: incompatible.append((kind, mime)),
        )
        item = (
            SimpleNamespace(mimeType="video/VP8"),
            SimpleNamespace(data=b"vp8", timestamp=1),
        )

        adapter.put(item)

        assert incompatible == [("video", "video/vp8")]
        assert adapter.get_nowait() == item
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_encoded_frame_adapter_rejects_unvalidated_aiortc_version(monkeypatch):
    peer = aiortc.RTCPeerConnection()
    transceiver = peer.addTransceiver("video", direction="recvonly")
    monkeypatch.setattr(aiortc, "__version__", "1.15.0")
    try:
        with pytest.raises(ring_media.EncodedFrameAdapterError, match="1.15.0"):
            ring_media.install_encoded_frame_adapter(
                transceiver.receiver,
                "video",
                lambda *_args: None,
                forward_to_decoder=False,
            )
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_startup_keyframe_requests_target_media_not_rtx_ssrc(monkeypatch):
    requested = []

    class Receiver:
        _RTCRtpReceiver__active_ssrc = {
            1001: object(),
            2001: object(),
        }
        _RTCRtpReceiver__rtx_ssrc = {2001: 1001}

        async def _send_rtcp_pli(self, media_ssrc):
            requested.append(media_ssrc)

    monkeypatch.setattr(ring_media, "STARTUP_KEYFRAME_REQUESTS", 2)
    monkeypatch.setattr(ring_media, "STARTUP_KEYFRAME_INTERVAL_SECONDS", 0)

    await ring_media.request_startup_keyframes(
        Receiver(),
        threading.Event(),
        asyncio.Event(),
    )

    assert requested == [1001, 1001]


@pytest.mark.asyncio
async def test_startup_keyframe_request_stops_before_private_access():
    stop = threading.Event()
    stop.set()

    await ring_media.request_startup_keyframes(
        object(),
        stop,
        asyncio.Event(),
    )


def test_encoded_frame_adapter_rejects_changed_private_contract():
    class ChangedReceiver:
        _RTCRtpReceiver__decoder_queue = queue.Queue()
        _RTCRtpReceiver__decoder_thread = None

        async def _handle_rtp_packet(self):
            return None

    with pytest.raises(ring_media.EncodedFrameAdapterError, match="contract changed"):
        ring_media.install_encoded_frame_adapter(
            ChangedReceiver(),
            "video",
            lambda *_args: None,
            forward_to_decoder=False,
        )


def install_media_fakes(monkeypatch):
    peer = FakePeerConnection()
    FakeMicrophoneTrack.instances = []
    monkeypatch.setattr(aiortc, "RTCPeerConnection", lambda _configuration: peer)
    monkeypatch.setattr(ring_media, "MicrophoneTrack", FakeMicrophoneTrack)
    return peer


@pytest.mark.asyncio
async def test_receiver_installs_per_track_encoded_adapters(monkeypatch):
    peer = install_media_fakes(monkeypatch)
    stop = threading.Event()
    installed = []

    def install(receiver, kind, callback, *, forward_to_decoder, on_incompatible=None):
        installed.append((receiver, kind, callback, forward_to_decoder, on_incompatible))
        return object()

    monkeypatch.setattr(ring_media, "install_encoded_frame_adapter", install)

    async def finish_generate(_session_id, _on_rtc_message):
        stop.set()

    def on_video(*_args):
        return None

    def on_audio(*_args):
        return None

    device = FakeDevice(on_generate=finish_generate)
    receiver = ring_media.RingStreamReceiver(
        device,
        None,
        None,
        on_encoded_video=on_video,
        on_encoded_audio=on_audio,
        decode_video=False,
        decode_audio=False,
    )

    await receiver.receive(stop)

    assert [(kind, callback, forward) for _, kind, callback, forward, _ in installed] == [
        ("video", on_video, False),
        ("audio", on_audio, False),
    ]
    assert peer.closed is True


@pytest.mark.asyncio
async def test_receiver_uses_supplied_microphone_source(monkeypatch):
    peer = install_media_fakes(monkeypatch)
    stop = threading.Event()
    source = ring_media.MicrophonePcmBuffer()

    async def finish_generate(_session_id, _on_rtc_message):
        stop.set()

    receiver = ring_media.RingStreamReceiver(
        FakeDevice(on_generate=finish_generate),
        None,
        microphone_source=source,
    )

    await receiver.receive(stop)

    assert peer.closed is True
    assert FakeMicrophoneTrack.instances[0].source is source


@pytest.mark.asyncio
async def test_receiver_cleans_up_when_ring_negotiation_fails(monkeypatch):
    peer = install_media_fakes(monkeypatch)

    async def fail_generate(_session_id, _on_rtc_message):
        raise RuntimeError("Ring negotiation failed")

    device = FakeDevice(on_generate=fail_generate)
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)

    with pytest.raises(RuntimeError, match="Ring negotiation failed"):
        await receiver.receive(threading.Event())

    assert peer.closed is True
    assert len(device.closed_sessions) == 1
    assert FakeMicrophoneTrack.instances[0].stopped is True


@pytest.mark.asyncio
async def test_receiver_cleans_up_when_cancelled(monkeypatch):
    peer = install_media_fakes(monkeypatch)
    generated = asyncio.Event()

    async def mark_generated(_session_id, _on_rtc_message):
        generated.set()

    device = FakeDevice(on_generate=mark_generated)
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)
    task = asyncio.create_task(receiver.receive(threading.Event()))
    await generated.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert peer.closed is True
    assert len(device.closed_sessions) == 1
    assert FakeMicrophoneTrack.instances[0].stopped is True


@pytest.mark.asyncio
async def test_receiver_exits_and_cleans_up_when_video_track_ends(monkeypatch):
    peer = install_media_fakes(monkeypatch)

    class EndedVideoTrack:
        kind = "video"

        async def recv(self):
            raise MediaStreamError

    async def end_video(_session_id, _on_rtc_message):
        peer.handlers["track"](EndedVideoTrack())

    device = FakeDevice(on_generate=end_video)
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)

    await asyncio.wait_for(receiver.receive(threading.Event()), timeout=0.5)

    assert peer.closed is True
    assert len(device.closed_sessions) == 1
    assert FakeMicrophoneTrack.instances[0].stopped is True


@pytest.mark.asyncio
async def test_receiver_cancels_pending_signaling_tasks(monkeypatch):
    peer = install_media_fakes(monkeypatch)
    stop = threading.Event()

    async def send_answer(_session_id, on_rtc_message):
        on_rtc_message(
            SimpleNamespace(
                answer="answer",
                candidate=None,
                sdp_m_line_index=None,
                error_code=None,
            )
        )
        await peer.answer_started.wait()
        stop.set()

    device = FakeDevice(on_generate=send_answer)
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)

    await receiver.receive(stop)

    assert peer.answer_cancelled is True
    assert peer.closed is True
    assert len(device.closed_sessions) == 1
    assert FakeMicrophoneTrack.instances[0].stopped is True
