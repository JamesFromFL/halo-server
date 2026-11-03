"""Public released HAP delegates over real IPC and independently decoded SRTP."""

import asyncio
import json
import socket
import struct
from pathlib import Path
from uuid import uuid4

import av
import numpy as np
import pytest
from test_homekit_audio import (
    AUDIO_KEY,
    AUDIO_SALT,
    crypto,
    opus_decoder,
    receive_media,
    tone_packets,
)
from test_homekit_bridge import ADAPTER, NODE
from test_homekit_broker import setup_broker
from test_homekit_video_composed import SCENARIO
from test_native_service import Capture

from halo_server.homekit.runtime import HomeKitRuntime
from halo_server.homekit.settings import CameraAudioSettings
from halo_server.media.homekit_live import HomeKitLiveManager
from halo_server.media.opus import OriginalOpusBuffer


class OriginalAudioCapture(Capture):
    def __init__(self, client, camera, *, legacy_audio_enabled=True):
        super().__init__(client, camera, legacy_audio_enabled=legacy_audio_enabled)
        self.original_audio_buffer = OriginalOpusBuffer()


@pytest.mark.asyncio
async def test_public_hap_audio_reaches_native_udp_and_generation_gated_speaker(tmp_path):
    broker, old_boot, _, service, client, _ = await setup_broker(tmp_path)
    await broker.retired(old_boot)
    service._media_session_factory = OriginalAudioCapture
    source = service._homekit_backend.inventory().cameras[0].scope
    preferences = CameraAudioSettings(source.profile_id, "42", str(uuid4()), True, True)
    broker._audio_settings = lambda scope: (
        preferences if scope.source_key == source.source_key else None
    )
    live = HomeKitLiveManager(service.homekit)
    await live.qualify()
    assert live.ready and live.audio_ready
    broker.live_manager = live
    video, audio = (socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2))
    for sock in (video, audio):
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
    hap_entry = (
        Path(__file__).resolve().parents[1]
        / "homekit-adapter/node_modules/@homebridge/hap-nodejs/dist/index.js"
    )
    script = (
        SCENARIO.replace(
            "active:()=>true,forceStop:()=>{}",
            "active:()=>true,audio:()=>({microphone:true,speaker:true,microphone_muted:false,speaker_muted:false,microphone_volume:100,speaker_volume:100,revision:1}),forceStop:()=>{}",
        )
        .replace(
            "video:source,audio:source",
            "video:source,audio:{...source,port:__AUDIO_PORT__,srtp_key:Buffer.from(__AUDIO_KEY__,'hex'),srtp_salt:Buffer.from(__AUDIO_SALT__,'hex')}",
        )
        .replace(
            "audio:{}}",
            "audio:{codec:hap.AudioStreamingCodecType.OPUS,channel:1,bit_rate:hap.AudioBitrate.CONSTANT,sample_rate:24,packet_time:20,pt:110,ssrc:response.audio.ssrc,max_bit_rate:24,rtcp_interval:.1,comfort_pt:13,comfortNoiseEnabled:false}}",
        )
        .replace(
            "reason:`started-${response.video.ssrc}`",
            "reason:`started-${response.audio.ssrc}-${response.audio.port}`",
        )
        .replace("__ROOT__", json.dumps(ADAPTER.as_uri()))
        .replace("__HAP__", json.dumps(hap_entry.resolve().as_uri()))
        .replace("__PORT__", str(video.getsockname()[1]))
        .replace("__KEY__", json.dumps(bytes(range(16)).hex()))
        .replace("__SALT__", json.dumps(bytes(range(14)).hex()))
        .replace("__AUDIO_PORT__", str(audio.getsockname()[1]))
        .replace("__AUDIO_KEY__", json.dumps(AUDIO_KEY.hex()))
        .replace("__AUDIO_SALT__", json.dumps(AUDIO_SALT.hex()))
    )
    entry = tmp_path / "audio.mjs"
    entry.write_text(script)
    started = asyncio.get_running_loop().create_future()

    async def notice(message):
        if message["kind"] == "status":
            if not started.done():
                started.set_result(message["payload"]["reason"])
        else:
            await broker.notice(message)

    async def retired(boot):
        service.homekit.fence()
        await live.retired(boot)
        await broker.retired(boot)

    runtime = HomeKitRuntime(
        NODE,
        entry,
        build_id="native-audio-fixture",
        on_ready=broker.ready,
        on_request=broker.request,
        on_notice=notice,
        on_retired=retired,
        max_restarts=0,
    )
    broker.runtime = runtime
    try:
        await asyncio.wait_for(runtime.start(), 4)
        result = await asyncio.wait_for(started, 4)
        assert result.startswith("started-"), result
        _, ssrc, port = result.split("-")
        capture = service._media_sessions["42"]
        for index, payload in enumerate(tone_packets()[:8]):
            capture.original_audio_buffer.append_audio(payload, index * 960)
            # Model a continuing Ring source, rather than a pre-start replay
            # batch that live-edge admission intentionally trims to its tail.
            await asyncio.sleep(0.02)
        packets = await receive_media(audio, crypto(int(ssrc)), 8)
        assert all(
            (b.timestamp - a.timestamp) & 0xFFFFFFFF == 480
            for a, b in zip(packets, packets[1:], strict=False)
        )
        assert [packet.payload for packet in packets] == list(tone_packets()[:8])
        decoder = opus_decoder()
        decoded = [
            frame for packet in packets for frame in decoder.decode(av.Packet(packet.payload))
        ]
        samples = np.concatenate([frame.to_ndarray().reshape(-1) for frame in decoded])[960:]
        peak = np.fft.rfftfreq(samples.size, 1 / 48000)[np.argmax(abs(np.fft.rfft(samples)))]
        assert 420 < peak < 460
        assert len(capture.leases) == 1 and client.submissions == 0
        peer = crypto(900)
        for index, payload in enumerate(tone_packets()[:6]):
            packet = struct.pack("!BBHII", 0x80, 110, index, index * 480, 900) + payload
            audio.sendto(peer.protect(packet), ("127.0.0.1", int(port)))
            await asyncio.sleep(0.02)
        async with asyncio.timeout(3):
            while len(capture.frames) < 7:
                await asyncio.sleep(0.005)
        assert capture.frames[0] == bytes(1920)
        pcm = np.frombuffer(b"".join(capture.frames[1:]), dtype="<i2")
        assert pcm.size == 6 * 960 and max(abs(pcm)) > 1000
        peak = np.fft.rfftfreq(pcm.size, 1 / 48000)[np.argmax(abs(np.fft.rfft(pcm)))]
        assert 420 < peak < 460 and len(capture.leases) == 2
        process = runtime._process
        await asyncio.wait_for(runtime.stop(), 4)
        assert process.returncode == 0
        assert not capture.leases and not service._intercom_leases
        assert (
            live.pending_resources
            == live.cleanup_failures
            == service.homekit.pending_resources
            == 0
        )
    finally:
        video.close()
        audio.close()
        await runtime.stop()
        await live.retired(runtime.boot_id)
