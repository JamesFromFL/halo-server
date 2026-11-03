"""Lifecycle tests for the headless Ring WebRTC receiver."""

import asyncio
import json
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
        self._webrtc_streams = {}
        self.messages = []
        self.activation_tasks = []

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
        assert keep_alive_timeout is None
        device = self

        class Socket:
            async def send(self, message):
                device.messages.append(json.loads(message))

            async def close(self):
                pass

        stream = RingWebRtcStream(None, 1, keep_alive_timeout=keep_alive_timeout)
        stream.dialog_id = "synthetic-dialog"
        stream.session_id = "synthetic-session"
        stream.websocket = Socket()
        stream._offered_event.set()
        self._webrtc_streams[session_id] = stream

        def notify(message):
            if message.answer:
                stream._sdp_answer_event.set()
            on_rtc_message(message)
            if message.answer:
                self.activation_tasks.append(asyncio.create_task(stream._activate()))

        if self.on_generate is not None:
            await self.on_generate(session_id, notify)

    async def close_webrtc_stream(self, session_id):
        self.closed_sessions.append(session_id)
        if stream := self._webrtc_streams.pop(session_id, None):
            for task in self.activation_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.activation_tasks, return_exceptions=True)
            await stream.close()

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


def test_media_compatibility_gate_accepts_installed_validated_contracts():
    ring_media.validate_media_compatibility()


def test_media_compatibility_gate_rejects_unvalidated_aiortc(monkeypatch):
    monkeypatch.setattr(aiortc, "__version__", "99.0.0")

    with pytest.raises(ring_media.MediaCompatibilityError, match="aiortc 99.0.0"):
        ring_media.validate_media_compatibility()


@pytest.mark.asyncio
async def test_microphone_track_never_opens_a_server_local_capture_device(monkeypatch):
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    monkeypatch.setattr(
        Gst,
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
    monkeypatch.setattr(aiortc, "__version__", "99.0.0")
    try:
        with pytest.raises(ring_media.EncodedFrameAdapterError, match="99.0.0"):
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

    from datetime import UTC, datetime

    from aiortc.rtcrtpreceiver import RTCRtpReceiver
    from aiortc.rtp import RtcpPacket

    from halo_server.media import keyframes

    class Transport:
        state = "connected"
        _stats_id = "offline"

        async def _send_rtp(self, packet):
            requested.append(RtcpPacket.parse(packet)[0].media_ssrc)

    receiver = RTCRtpReceiver("video", Transport())
    receiver._set_rtcp_ssrc(77)
    receiver._RTCRtpReceiver__active_ssrc = {1001: datetime.now(UTC), 2001: datetime.now(UTC)}
    receiver._RTCRtpReceiver__remote_streams = {1001: object(), 2001: object()}
    receiver._RTCRtpReceiver__rtx_ssrc = {2001: 1001}
    monkeypatch.setattr(keyframes, "KEYFRAME_COOLDOWN_SECONDS", 0)

    monkeypatch.setattr(ring_media, "STARTUP_KEYFRAME_REQUESTS", 2)
    monkeypatch.setattr(ring_media, "STARTUP_KEYFRAME_INTERVAL_SECONDS", 0)

    await ring_media.request_startup_keyframes(
        receiver,
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
async def test_receiver_keeps_sdk_pings_until_last_bounded_consumer_retires(monkeypatch):
    """Exercise the real SDK pinger past both 5 min and the first lease's 10 min."""
    from ring_doorbell import webrtcstream

    from halo_server.media.leases import LeasePurpose, MediaLeaseCoordinator

    peer = install_media_fakes(monkeypatch)
    stop = threading.Event()
    now = [100.0]
    leases = MediaLeaseCoordinator(clock=lambda: now[0])
    first, _ = leases.acquire(LeasePurpose.HOMEKIT_LIVE, lease_id="first")
    active = [first.id]
    ping_times = []
    tasks = []

    async def generated(session_id, _notify):
        stream = device._webrtc_streams[session_id]
        stream._last_keep_alive = now[0]
        original_send = stream.websocket.send

        async def send(message):
            if json.loads(message)["method"] == "ping":
                ping_times.append(now[0] - 100.0)
            await original_send(message)

        stream.websocket.send = send

        async def advance(seconds):
            now[0] += seconds
            if now[0] == 350.0:
                second, _ = leases.acquire(LeasePurpose.HOMEKIT_LIVE, lease_id="second")
                active.append(second.id)
            active[:] = [identifier for identifier in active if leases.renew(identifier)[0]]
            if not active:
                stop.set()
                # The actual receiver cleanup must cancel this SDK-owned task.
                await asyncio.Event().wait()
            await asyncio.sleep(0)

        with monkeypatch.context() as patch:
            patch.setattr(webrtcstream, "time", SimpleNamespace(time=lambda: now[0]))
            patch.setattr(webrtcstream, "asyncio", SimpleNamespace(sleep=advance))
            stream.ping_task = asyncio.create_task(stream.pinger())
            tasks.append(stream.ping_task)
            while not stop.is_set():
                # An unexpectedly expired pinger must fail rather than hang.
                assert not stream.ping_task.done()
                await asyncio.sleep(0)

    device = FakeDevice(on_generate=generated)
    receiver = ring_media.RingStreamReceiver(device, None)
    await asyncio.wait_for(receiver.receive(stop), 2)

    assert ping_times == list(range(5, 850, 5))
    assert now[0] - 100.0 == 850
    assert not leases.snapshot().leases
    assert tasks[0].cancelled()
    assert device._webrtc_streams == {}
    assert len(device.closed_sessions) == 1
    assert peer.closed


@pytest.mark.asyncio
async def test_receiver_installs_per_track_encoded_adapters(monkeypatch):
    peer = install_media_fakes(monkeypatch)
    stop = threading.Event()
    installed = []
    received = []

    def install(receiver, kind, callback, *, forward_to_decoder, on_incompatible=None):
        installed.append((receiver, kind, callback, forward_to_decoder, on_incompatible))
        return object()

    monkeypatch.setattr(ring_media, "install_encoded_frame_adapter", install)

    async def finish_generate(_session_id, _on_rtc_message):
        installed[0][2](b"\x00\x00\x00\x01\x65frame", 200)
        stop.set()

    def on_video(*_args):
        received.append(_args)

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

    assert [(kind, forward) for _, kind, _, forward, _ in installed] == [
        ("video", False),
        ("audio", False),
    ]
    assert installed[1][2] is on_audio
    assert received == [(b"\x00\x00\x00\x01\x65frame", 200)]
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
async def test_receiver_remote_description_failure_is_terminal(monkeypatch):
    peer = install_media_fakes(monkeypatch)

    async def reject_answer(_answer):
        raise RuntimeError("injected invalid SDP")

    peer.setRemoteDescription = reject_answer

    async def send_answer(_session_id, on_rtc_message):
        on_rtc_message(
            SimpleNamespace(
                answer="invalid answer",
                candidate=None,
                sdp_m_line_index=None,
                error_code=None,
            )
        )

    device = FakeDevice(on_generate=send_answer)
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)

    with pytest.raises(
        ring_media.RingMediaNegotiationError,
        match="remote description",
    ):
        await asyncio.wait_for(receiver.receive(threading.Event()), timeout=0.5)

    assert peer.closed is True
    assert len(device.closed_sessions) == 1


@pytest.mark.asyncio
async def test_receiver_remote_description_timeout_is_terminal(monkeypatch):
    peer = install_media_fakes(monkeypatch)

    async def send_answer(_session_id, on_rtc_message):
        on_rtc_message(
            SimpleNamespace(
                answer="answer",
                candidate=None,
                sdp_m_line_index=None,
                error_code=None,
            )
        )

    monkeypatch.setattr(ring_media, "WEBRTC_REMOTE_DESCRIPTION_TIMEOUT", 0.01)
    device = FakeDevice(on_generate=send_answer)
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)

    with pytest.raises(
        ring_media.RingMediaNegotiationError,
        match="remote description",
    ):
        await asyncio.wait_for(receiver.receive(threading.Event()), timeout=0.5)

    assert peer.answer_cancelled is True
    assert peer.closed is True


@pytest.mark.asyncio
async def test_receiver_missing_answer_times_out_and_cleans_up(monkeypatch):
    peer = install_media_fakes(monkeypatch)
    monkeypatch.setattr(ring_media, "WEBRTC_ANSWER_TIMEOUT", 0.01)
    device = FakeDevice()
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)

    with pytest.raises(ring_media.RingMediaNegotiationError, match="answer timed out"):
        await asyncio.wait_for(receiver.receive(threading.Event()), timeout=0.5)

    assert peer.closed is True
    assert len(device.closed_sessions) == 1


@pytest.mark.asyncio
async def test_receiver_local_offer_timeout_closes_peer(monkeypatch):
    peer = install_media_fakes(monkeypatch)
    local_offer_cancelled = False

    async def hang_local_offer(_offer):
        nonlocal local_offer_cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            local_offer_cancelled = True
            raise

    peer.setLocalDescription = hang_local_offer
    monkeypatch.setattr(ring_media, "WEBRTC_OFFER_TIMEOUT", 0.01)
    device = FakeDevice()
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)

    with pytest.raises(ring_media.RingMediaNegotiationError, match="offer preparation"):
        await asyncio.wait_for(receiver.receive(threading.Event()), timeout=0.5)

    assert local_offer_cancelled is True
    assert peer.closed is True
    assert device.closed_sessions == []


@pytest.mark.asyncio
async def test_receiver_ring_signaling_timeout_closes_partial_session(monkeypatch):
    peer = install_media_fakes(monkeypatch)
    signaling_cancelled = False

    async def hang_generate(_session_id, _on_rtc_message):
        nonlocal signaling_cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            signaling_cancelled = True
            raise

    monkeypatch.setattr(ring_media, "WEBRTC_SIGNALING_TIMEOUT", 0.01)
    device = FakeDevice(on_generate=hang_generate)
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)

    with pytest.raises(ring_media.RingMediaNegotiationError, match="signaling timed out"):
        await asyncio.wait_for(receiver.receive(threading.Event()), timeout=0.5)

    assert signaling_cancelled is True
    assert peer.closed is True
    assert len(device.closed_sessions) == 1


@pytest.mark.asyncio
async def test_receiver_bounds_ring_and_peer_close(monkeypatch):
    peer = install_media_fakes(monkeypatch)
    stop = threading.Event()
    ring_close_started = asyncio.Event()
    peer_close_started = asyncio.Event()
    cancelled = []

    async def finish_generate(_session_id, _on_rtc_message):
        stop.set()

    async def hang_ring_close(_session_id):
        ring_close_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append("ring")
            raise

    async def hang_peer_close():
        peer_close_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append("peer")
            raise

    device = FakeDevice(on_generate=finish_generate)
    device.close_webrtc_stream = hang_ring_close
    peer.close = hang_peer_close
    monkeypatch.setattr(ring_media, "RECEIVER_CLOSE_TIMEOUT", 0.01)
    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None)

    await asyncio.wait_for(receiver.receive(stop), timeout=0.2)
    await asyncio.sleep(0)

    assert ring_close_started.is_set()
    assert peer_close_started.is_set()
    assert sorted(cancelled) == ["peer", "ring"]


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


def _progress_receiver(monkeypatch, *, payload=None, audio=False, accepted=True):
    peer = install_media_fakes(monkeypatch)
    callbacks = {}

    def install(_receiver, kind, callback, **_kwargs):
        callbacks[kind] = callback

    monkeypatch.setattr(ring_media, "install_encoded_frame_adapter", install)

    async def apply_answer(_answer):
        peer.connectionState = "connected"

    peer.setRemoteDescription = apply_answer

    async def generate(_session_id, on_message):
        on_message(SimpleNamespace(answer="answer", candidate=None, error_code=None))
        if payload is not None:
            callbacks["video"](payload, 10)
        if audio:
            callbacks["audio"](b"\xf8audio", 0)

    device = FakeDevice(on_generate=generate)
    receiver = ring_media.RingStreamReceiver(
        device,
        None,
        on_encoded_video=lambda *_args: accepted,
        on_encoded_audio=lambda *_args: None,
        decode_video=False,
        decode_audio=False,
        first_video_timeout=0.03,
        video_stall_timeout=0.03,
    )
    return receiver, peer, device, callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,audio",
    [
        (None, False),
        (None, True),
        (b"not-h264", False),
        (b"\x00\x00\x00\x01\x41pframe", False),
        (b"\x00\x00\x00\x01\x65idr-without-parameters", False),
    ],
)
async def test_receiver_requires_keyframe_startup_even_when_connected_or_audio_active(
    monkeypatch, payload, audio
):
    receiver, peer, device, _callbacks = _progress_receiver(
        monkeypatch, payload=payload, audio=audio
    )
    with pytest.raises(ring_media.RingMediaProgressError, match="video_startup_timeout"):
        await asyncio.wait_for(receiver.receive(threading.Event()), 0.5)
    assert peer.closed
    assert len(device.closed_sessions) == 1


@pytest.mark.asyncio
async def test_receiver_accepted_keyframe_then_stall_retires_and_rejects_late_video(monkeypatch):
    keyframe = b"\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x01\x65idr"
    receiver, peer, device, callbacks = _progress_receiver(monkeypatch, payload=keyframe)
    with pytest.raises(ring_media.RingMediaProgressError, match="video_stalled"):
        await asyncio.wait_for(receiver.receive(threading.Event()), 0.5)
    assert peer.closed
    assert len(device.closed_sessions) == 1
    assert callbacks["video"](keyframe, 20) is False


@pytest.mark.asyncio
async def test_receiver_rejected_keyframe_does_not_hide_startup_timeout(monkeypatch):
    keyframe = b"\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x01\x65idr"
    receiver, _peer, _device, _callbacks = _progress_receiver(
        monkeypatch, payload=keyframe, accepted=False
    )
    with pytest.raises(ring_media.RingMediaProgressError, match="video_startup_timeout"):
        await asyncio.wait_for(receiver.receive(threading.Event()), 0.5)


@pytest.mark.asyncio
async def test_receiver_startup_deadline_survives_signaling_cancellation_suppression(monkeypatch):
    receiver, peer, device, callbacks = _progress_receiver(monkeypatch)
    accepted = []
    receiver._on_encoded_video = lambda *_args: accepted.append(True)
    original_generate = device.on_generate

    async def slow_generate(session_id, on_message):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Model an SDK await that returns after swallowing timeout cancellation.
            await original_generate(session_id, on_message)
            keyframe = b"\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x01\x65idr"
            assert callbacks["video"](keyframe, 20) is False

    device.on_generate = slow_generate
    with pytest.raises(ring_media.RingMediaProgressError, match="video_startup_timeout"):
        await asyncio.wait_for(receiver.receive(threading.Event()), 0.5)
    assert not accepted
    assert peer.closed


@pytest.mark.asyncio
async def test_receiver_retains_cleanup_barrier_when_ring_close_suppresses_cancellation(
    monkeypatch,
):
    peer = install_media_fakes(monkeypatch)
    stop = threading.Event()
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def generate(_session_id, _on_message):
        stop.set()

    async def close_ring(_session_id):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            blocked.set()
            await release.wait()

    device = FakeDevice(on_generate=generate)
    device.close_webrtc_stream = close_ring
    monkeypatch.setattr(ring_media, "RECEIVER_CLOSE_TIMEOUT", 0.01)
    task = asyncio.create_task(ring_media.RingStreamReceiver(device, None).receive(stop))
    await asyncio.wait_for(blocked.wait(), 0.5)
    assert peer.closed  # Stalled Ring teardown does not prevent closing the peer.
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()  # Cancellation is a request, not teardown completion.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)
