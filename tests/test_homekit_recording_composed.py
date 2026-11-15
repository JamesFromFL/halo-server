"""Actual controller, warm source history and native fMP4 process together."""

import asyncio
import io
from dataclasses import replace
from fractions import Fraction
from uuid import uuid4

import av
import pytest
from test_homekit_controller import POLICY, Backend

from halo_server.homekit.controller import HomeKitController
from halo_server.media.buffer import EncodedPacket, MediaStreamKind
from halo_server.media.h264 import VideoConfiguration, split_annexb
from halo_server.media.homekit_recording import HomeKitRecordingManager
from halo_server.media.opus import OriginalOpusBuffer, parse_opus
from halo_server.media.recording import RecordingAudioConfiguration, RecordingConfiguration
from halo_server.media.recording_capture import RecordingCaptureManager, RecordingSourceBuffer


def encoded_video():
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = 1280, 720, "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 15), Fraction(15)
    encoder.thread_count = 1
    encoder.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "profile": "main",
        "level": "4.0",
        "x264-params": "keyint=30:min-keyint=30:scenecut=0:bframes=0:threads=1",
    }
    packets = []
    for number in range(91):
        frame = av.VideoFrame(1280, 720, "yuv420p")
        for index, plane in enumerate(frame.planes):
            plane.update(bytes([48 + number % 64 if index == 0 else 128]) * plane.buffer_size)
        frame.pts = number
        packets.extend(encoder.encode(frame))
    packets.extend(encoder.encode(None))
    headers = split_annexb(bytes(packets[0]))
    sps = next(nal for nal in headers if nal[0] & 31 == 7)
    pps = next(nal for nal in headers if nal[0] & 31 == 8)
    return packets, sps, pps


def encoded_audio():
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate, encoder.layout, encoder.format = 48000, "mono", "s16"
    encoder.time_base, encoder.bit_rate, encoder.thread_count = Fraction(1, 48000), 32000, 1
    packets = []
    for number in range(306):
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.sample_rate, frame.time_base, frame.pts = 48000, Fraction(1, 48000), number * 960
        frame.planes[0].update(bytes(frame.planes[0].buffer_size))
        packets.extend(encoder.encode(frame))
    packets.extend(encoder.encode(None))
    return [bytes(packet) for packet in packets]


class Resource:
    def __init__(self, source, audio, sps, pps, floor):
        self.buffer, self.original_audio_buffer = source, audio
        self.closed = False
        self.video_configuration_callback = lambda: (source.capture_epoch, 1, sps, pps, floor)
        self.source_clock_callback = tuple

    def is_current(self):
        return not self.closed

    def renew(self):
        return not self.closed

    def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("audio", [False, True])
async def test_warm_recording_has_real_preroll_and_independently_decodable_output(audio, tmp_path):
    video, sps, pps = encoded_video()
    opus = encoded_audio() if audio else []
    backend = Backend()
    controller = HomeKitController(backend)
    boot = str(uuid4())
    scope = (await controller.attach(boot, [backend.cameras[0].scope])).cameras[0].scope
    scope = await controller.update_policy(
        boot, scope, replace(POLICY, recording=True, recording_audio=audio)
    )
    now = [asyncio.get_running_loop().time() + 0.001]
    source = RecordingSourceBuffer(clock=lambda: now[0])
    original = OriginalOpusBuffer(clock=lambda: now[0])
    resources = []

    async def acquire(scope, *, admission, warm=False):
        assert admission()
        resource = Resource(
            source,
            original,
            sps,
            pps,
            now[0] if not resources else resources[0].video_configuration_callback()[4],
        )
        resources.append(resource)
        return resource

    backend.acquire_recording = acquire
    captures = RecordingCaptureManager(controller, clock=lambda: now[0])
    manager = HomeKitRecordingManager(controller, capture_manager=captures, clock=lambda: now[0])
    warm = None
    try:
        await manager.qualify()
        assert manager.ready
        warm = await captures.open(
            boot,
            scope,
            str(uuid4()),
            audio=audio,
            warm=True,
            owner_opt_in=True,
            recording_supported=True,
        )
        base, audio_cursor = now[0], 0
        for index, native in enumerate(video):
            receipt = base + index / 15
            now[0] = receipt
            source.append(
                EncodedPacket(
                    index + 1,
                    MediaStreamKind.VIDEO,
                    bytes(native),
                    index * 6000,
                    receipt,
                    capture_epoch=1,
                )
            )
            if audio:
                while audio_cursor < len(opus) and audio_cursor / 50 <= index / 15:
                    original.append_audio(
                        opus[audio_cursor], audio_cursor * 960, received_at=base + audio_cursor / 50
                    )
                    audio_cursor += 1
            async with asyncio.timeout(2):
                while not warm.history._video or warm.history._video[-1].received_at < receipt:
                    await asyncio.sleep(0.001)
        history = warm.snapshot(event_at=now[0])
        assert history.ready and history.available_video_seconds >= 4
        if audio:
            assert history.available_audio_seconds >= 4
        configuration = RecordingConfiguration(
            VideoConfiguration(1280, 720, 15, "main", "4.0", 2000),
            RecordingAudioConfiguration() if audio else None,
        )
        stream = await manager.open(boot, scope, str(uuid4()), configuration, event_at=now[0])
        assert stream.history_ready and stream.timing_mode == "receipt_estimate"
        await warm.close()
        assert not resources[1].closed  # Active recording owns its independent lease.
        stream.finish()
        output = [packet async for packet in stream]
        assert output and output[0].kind == "init"
        init = output[0].payload
        (tmp_path / "init.mp4").write_bytes(init)
        assert len(output) >= 3
        for packet in output[1:]:
            (tmp_path / f"fragment-{packet.sequence}.mp4").write_bytes(init + packet.payload)
            assert packet.kind == "fragment" and 0 < packet.duration_seconds <= 4
            with av.open(io.BytesIO(init + packet.payload)) as movie:
                assert len(movie.streams.video) == 1
                assert bool(movie.streams.audio) == audio
                frames = list(movie.decode(video=0))
                assert frames and all(not frame.is_corrupt for frame in frames)
                assert all((frame.width, frame.height) == (1280, 720) for frame in frames)
            if audio:
                # The movie-wide AAC priming edit is applied to the first
                # demuxed packet again when opening a late fragment alone.
                # A one-packet EOS tail can therefore trim to zero in that
                # artificial container. Verify each encoded AAC payload with
                # fresh codec state, and the unchanged continuous movie below.
                with av.open(io.BytesIO(init + packet.payload)) as movie:
                    track = movie.streams.audio[0]
                    decoder = av.CodecContext.create("aac", "r")
                    decoder.thread_count = 1
                    decoder.extradata = track.codec_context.extradata
                    decoded = []
                    for encoded in movie.demux(track):
                        if encoded.size:
                            raw = av.Packet(bytes(encoded))
                            decoded.extend(decoder.decode(raw))
                    assert decoded and all(not frame.is_corrupt for frame in decoded)
        if audio:
            complete = init + b"".join(packet.payload for packet in output[1:])
            expected_samples = (
                sum(
                    parse_opus(sample.payload).duration_samples
                    for sample in history.samples
                    if sample.kind == "audio"
                )
                // 2
            )  # Source48k -> AAC24k; padding is separate from packet duration.
            with av.open(io.BytesIO(complete)) as movie:
                decoded = list(movie.decode(audio=0))
                count = sum(frame.samples for frame in decoded)
                assert expected_samples <= count < expected_samples + 1024
                assert decoded[0].pts == 0
            with av.open(io.BytesIO(complete)) as movie:
                packets = [packet for packet in movie.demux(audio=0) if packet.size]
                assert packets[-1].pts + packets[-1].duration == expected_samples
        assert not manager.pending_resources and all(resource.closed for resource in resources)
    finally:
        await manager.close()
        await controller.close()
