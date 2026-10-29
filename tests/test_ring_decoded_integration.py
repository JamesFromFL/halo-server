"""Synthetic Ring negotiation joins the actual decoder and private native caller."""

import asyncio
import threading
from types import SimpleNamespace

import aiortc
import pytest
from aiortc.jitterbuffer import JitterFrame
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtcrtpreceiver import RemoteStreamTrack, RTCRtpReceiver, decoder_worker
from test_ring_media import FakeDevice, FakeMicrophoneTrack, FakePeerConnection
from test_ring_opus_decoder import silk_wideband_60ms as silk_wideband_60ms

from halo_server.media import decoded_audio, ring_media
from halo_server.media.audio import NormalizedOpusEncoder
from halo_server.media.decoded_audio import MAX_PCM_BYTES, MAX_PCM_FRAMES, MAX_PCM_SAMPLES
from halo_server.media.legacy_audio import LegacyAudioOwner, wait_native


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


class NativePeer(FakePeerConnection):
    """No network negotiation; actual released receiver/decoder ownership."""

    def __init__(self):
        super().__init__()
        self.receivers = {}
        self.decoder = None

    def addTransceiver(self, track_or_kind, **_kwargs):
        kind = track_or_kind if isinstance(track_or_kind, str) else track_or_kind.kind
        receiver = RTCRtpReceiver(kind, SimpleNamespace(state="connected"))
        receiver._track = RemoteStreamTrack(kind)
        self.receivers[kind] = receiver
        return SimpleNamespace(kind=kind, receiver=receiver)

    async def setRemoteDescription(self, _answer):
        self.connectionState = "connected"
        self.answer_started.set()

    async def close(self):
        # This is the pinned public peer's eventual decoder shutdown seam. The
        # wrapper must prove actual thread exit before this synchronous join.
        assert self.decoder is None or not self.decoder.is_alive()
        for receiver in self.receivers.values():
            RTCRtpReceiver._RTCRtpReceiver__stop_decoder(receiver)
        await super().close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["stop", "cancel"])
async def test_ring_private_audio_callback_retains_pcm_and_joins_native_before_release(
    monkeypatch, silk_wideband_60ms, cancel
):
    peer, stop = NativePeer(), threading.Event()
    entered, release = threading.Event(), threading.Event()
    native_closed = threading.Event()
    native_threads, callback_threads, observed, lanes, normalized = [], [], [], [], []
    original_lane = decoded_audio.DecodedAudioLane

    def lane_factory():
        lane = original_lane()
        lanes.append(lane)
        return lane

    class Encoder:
        def __init__(self):
            self.inner = NormalizedOpusEncoder()

        def encode(self, payload, channels, rate):
            native_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(5), "Test did not release the finite native operation"
            return self.inner.encode(payload, channels, rate)

        def close(self):
            # The existing encoder owns native objects by reference; destroy
            # that reference on the same serialized native worker.
            self.inner = None
            native_closed.set()

    owner = LegacyAudioOwner(Encoder)

    async def decoded(frame):
        callback_threads.append(threading.get_ident())
        assert observed  # Original bytes reached Halo before decoded forwarding.
        assert owner.claim()
        try:
            value = await wait_native(owner.submit(frame=frame), owner)
            normalized.extend(value.packets)
        finally:
            owner.release()

    monkeypatch.setattr(aiortc, "RTCPeerConnection", lambda _configuration: peer)
    monkeypatch.setattr(ring_media, "MicrophoneTrack", FakeMicrophoneTrack)
    monkeypatch.setattr(decoded_audio, "DecodedAudioLane", lane_factory)
    monkeypatch.setattr(ring_media, "RECEIVER_TASK_CLEANUP_TIMEOUT", 0.01)
    monkeypatch.setattr(ring_media, "RECEIVER_CLOSE_TIMEOUT", 0.01)
    codec = RTCRtpCodecParameters(
        mimeType="audio/opus", clockRate=48000, channels=2, payloadType=111
    )
    video_codec = RTCRtpCodecParameters(mimeType="video/H264", clockRate=90000, payloadType=102)
    keyframe = b"\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x01\x65idr"
    video_seen = []

    async def generate(_session_id, on_message):
        on_message(SimpleNamespace(answer="answer", candidate=None, error_code=None))
        audio = peer.receivers["audio"]
        track = audio._track
        assert audio._RTCRtpReceiver__decoder_thread is None
        peer.handlers["track"](track)
        assert track._queue is lanes[0].output  # Installed before decoder startup.
        peer.decoder = threading.Thread(
            target=decoder_worker,
            args=(
                asyncio.get_running_loop(),
                audio._RTCRtpReceiver__decoder_queue,
                track._queue,
            ),
        )
        audio._RTCRtpReceiver__decoder_thread = peer.decoder
        peer.decoder.start()
        peer.receivers["video"]._RTCRtpReceiver__decoder_queue.put(
            (video_codec, JitterFrame(keyframe, 0))
        )
        audio._RTCRtpReceiver__decoder_queue.put(
            (codec, JitterFrame(silk_wideband_60ms[0], 2_000_000_000))
        )

    device = FakeDevice(on_generate=generate)
    receiver = ring_media.RingStreamReceiver(
        device,
        None,
        on_encoded_video=lambda payload, timestamp: video_seen.append((payload, timestamp)),
        on_encoded_audio=lambda payload, timestamp: observed.append((payload, timestamp)),
        on_decoded_audio=decoded,
        decode_video=False,
        on_terminating=lambda _error: owner.fence(),
        first_video_timeout=3,
        video_stall_timeout=3,
    )
    task = asyncio.create_task(receiver.receive(stop))
    loop_thread = threading.get_ident()
    try:
        await until(entered.is_set)
        lane = lanes[0]
        assert callback_threads == [loop_thread]
        assert native_threads and native_threads[0] != loop_thread
        assert lane.snapshot()["inflight_frames"] == 1
        assert owner.diagnostics()["native_inflight"]

        # Holding the async native caller neither blocks Ring's loop nor the
        # byte-exact original Opus/video observers. Only decoded backlog drops.
        audio_queue = peer.receivers["audio"]._RTCRtpReceiver__decoder_queue
        for index, payload in enumerate(silk_wideband_60ms[1:], start=1):
            audio_queue.put((codec, JitterFrame(payload, 2_000_000_000 + index * 2880)))
        peer.receivers["video"]._RTCRtpReceiver__decoder_queue.put(
            (video_codec, JitterFrame(keyframe, 9000))
        )
        assert observed == [
            (payload, 2_000_000_000 + index * 2880)
            for index, payload in enumerate(silk_wideband_60ms)
        ]
        assert video_seen == [(keyframe, 0), (keyframe, 9000)]
        snapshot = lane.snapshot()
        assert snapshot["reserved_frames"] <= MAX_PCM_FRAMES
        assert snapshot["reserved_bytes"] <= MAX_PCM_BYTES
        assert snapshot["reserved_samples"] <= MAX_PCM_SAMPLES
        assert snapshot["counters"]["input_dropped"] > 0

        if cancel:
            task.cancel()
        else:
            stop.set()
        await until(lambda: lane.snapshot()["closed"])
        await asyncio.sleep(0.03)  # Cross the intentionally short cleanup deadline.
        task.cancel()  # Repeated cancellation cannot release the native caller.
        await asyncio.sleep(0)
        assert not task.done()
        assert lane.snapshot()["inflight_frames"] == 1
        assert not owner.closed and not native_closed.is_set()
        assert len(callback_threads) == 1

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        await until(lambda: owner.closed)
        assert peer.closed and peer.decoder is not None and not peer.decoder.is_alive()
        assert lane.snapshot()["native_retired"]
        assert lane.snapshot()["inflight_frames"] == 0
        assert lane.snapshot()["reserved_bytes"] == 0
        assert native_closed.is_set() and len(device.closed_sessions) == 1
        assert not normalized  # Cancellation never delivers a late native result.
    finally:
        stop.set()
        release.set()
        owner.fence()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await until(lambda: owner.closed)
