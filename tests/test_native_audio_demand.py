"""Native service modes retain original Opus without allocating the legacy lane."""

import asyncio
import time
from functools import partial
from types import SimpleNamespace

import aiortc
import av
import pytest
from aiortc.jitterbuffer import JitterFrame
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from test_media_session import SPS_PPS_IDR, FakeClient, _receiver_kwargs, fake_supervisor
from test_native_service import Camera, Sessions
from test_ring_decoded_integration import NativePeer
from test_ring_media import FakeDevice, FakeMicrophoneTrack

from halo_server.camera_privacy import ring_privacy_receipt
from halo_server.configuration import ServerConfiguration, ServerMode
from halo_server.media import decoded_audio, ring_media
from halo_server.media import session as session_module
from halo_server.media.leases import LeasePurpose
from halo_server.media.session import CameraMediaSession
from halo_server.paths import ServerPaths
from halo_server.service import HaloService


@pytest.mark.parametrize("mode", list(ServerMode))
def test_service_factory_applies_immutable_mode_to_real_capture_and_retries(tmp_path, mode):
    client = FakeClient()
    client.all_devices = [Camera()]
    client.is_authenticated = True
    sessions = Sessions(client)
    configuration = ServerConfiguration(mode=mode)
    service = HaloService(
        ServerPaths(tmp_path / "state", tmp_path / "run"),
        session_manager=sessions,
        configuration=configuration,
        media_session_factory=partial(CameraMediaSession, receiver_supervisor=fake_supervisor),
    )
    service._media_client, service._media_scope = client, sessions.active_scope
    service._account_integrations_ready = True
    client._camera_privacy_receipt = ring_privacy_receipt(client.all_devices, time.monotonic())
    service._remember_camera_privacy(client)
    capture = service._create_media_session("42")
    assert isinstance(capture, CameraMediaSession)
    capture.acquire(LeasePurpose.HOMEKIT_LIVE)
    callbacks = _receiver_kwargs(client.futures[0])
    try:
        for attempt in (1, 2):
            if attempt == 2:
                callbacks["on_attempt_end"](1, RuntimeError("synthetic source retry"))
                callbacks["on_attempt"](2)
            enabled = configuration.compatibility_enabled
            assert callbacks["decode_audio"] is enabled
            assert (callbacks.get("on_decoded_audio") is not None) is enabled
            assert (
                client.futures[0].coroutine.cr_frame.f_locals["_on_audio"] is not None
            ) is enabled
            assert (capture._audio_owner is not None) is enabled
            assert capture.diagnostics()["legacy_audio"]["enabled"] is enabled
            assert capture._audio_encoder is None  # No PCM has been requested.
            assert callbacks["on_encoded_audio"](b"\xf8\xff\xfe", attempt * 960)
            assert capture.original_audio_buffer.snapshot().packets[0].payload == b"\xf8\xff\xfe"
            assert len(capture.original_audio_buffer.snapshot().packets) == 1
    finally:
        capture.stop_all()
    assert capture.receiver_retired
    assert not capture.original_audio_buffer.snapshot().packets


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", [LeasePurpose.HOMEKIT_LIVE, LeasePurpose.HOMEKIT_RECORDING])
async def test_native_capture_receiver_delivers_original_opus_without_decoding(
    monkeypatch, purpose
):
    peer = NativePeer()
    monkeypatch.setattr(aiortc, "RTCPeerConnection", lambda _configuration: peer)
    monkeypatch.setattr(ring_media, "MicrophoneTrack", FakeMicrophoneTrack)

    def unused(*_args, **_kwargs):
        pytest.fail("Native-only capture must not allocate a decoded or legacy audio owner")

    monkeypatch.setattr(session_module, "LegacyAudioOwner", unused)
    monkeypatch.setattr(decoded_audio, "DecodedAudioLane", unused)
    original = b"\xf8\xff\xfe"
    client, device = FakeClient(), FakeDevice()
    device.id = "42"
    capture = CameraMediaSession(
        client,
        device,
        receiver_supervisor=fake_supervisor,
        legacy_audio_enabled=False,
        audio_encoder_factory=unused,
    )
    capture.acquire(purpose)
    talk = capture.acquire(LeasePurpose.INTERCOM)
    kwargs = dict(_receiver_kwargs(client.futures[0]))
    kwargs.pop("on_attempt")
    kwargs.pop("on_attempt_end")
    audio_codec = RTCRtpCodecParameters(
        mimeType="audio/opus", clockRate=48000, channels=2, payloadType=111
    )
    video_codec = RTCRtpCodecParameters(mimeType="video/H264", clockRate=90000, payloadType=102)

    async def generated(_session_id, notify):
        notify(SimpleNamespace(answer="answer", candidate=None, error_code=None))
        audio, video = peer.receivers["audio"], peer.receivers["video"]
        peer.handlers["track"](audio._track)
        video._RTCRtpReceiver__decoder_queue.put((video_codec, JitterFrame(SPS_PPS_IDR, 0)))
        audio._RTCRtpReceiver__decoder_queue.put((audio_codec, JitterFrame(original, 960)))
        async with asyncio.timeout(2):
            while not capture.original_audio_buffer.snapshot().packets:
                await asyncio.sleep(0)
        assert audio._RTCRtpReceiver__decoder_thread is None
        assert video._RTCRtpReceiver__decoder_thread is None
        assert capture._audio_owner is capture._audio_encoder is None
        assert not capture._retiring_audio
        microphone = kwargs["microphone_source"]
        pcm = b"\x01\x00" * ring_media.MIC_SAMPLES
        capture.push_intercom_audio(talk.id, pcm)
        assert microphone.set_speaker_ready(capture.intercom_generation(talk.id), True)
        assert microphone.pull() == pcm
        capture._stop.set()

    device.on_generate = generated
    receiver = ring_media.RingStreamReceiver(
        device,
        None,
        client.futures[0].coroutine.cr_frame.f_locals["_on_audio"],
        **kwargs,
    )
    try:
        await asyncio.wait_for(receiver.receive(capture._stop), 3)
        packets = capture.original_audio_buffer.read(after_sequence=None, timeout=0).packets
        assert len(packets) == 1 and packets[0].payload == original
        assert packets[0].rtp_timestamp == 960
        decoded = av.CodecContext.create("opus", "r").decode(av.Packet(packets[0].payload))
        assert len(decoded) == 1 and decoded[0].samples == 960
        assert all(packet.stream == "video" for packet in capture.buffer.snapshot().packets)
        assert capture.snapshot().opus_frame_duration_ms is None
        assert capture.diagnostics()["legacy_audio"] == {
            "enabled": False,
            "failure": None,
            "cleanup_pending": 0,
            "owner": None,
        }
    finally:
        capture.stop_all()
    assert peer.closed and capture.receiver_retired
    assert not capture.original_audio_buffer.snapshot().packets


@pytest.mark.parametrize("flag", [None, 0, "false"])
def test_legacy_audio_policy_requires_an_explicit_boolean(flag):
    with pytest.raises(ValueError, match="legacy audio enabled"):
        CameraMediaSession(FakeClient(), SimpleNamespace(id=42), legacy_audio_enabled=flag)
