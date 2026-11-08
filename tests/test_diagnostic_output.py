"""Opt-in flight recording preserves generated media and transport chronology."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_diagnostic_capture import PausedCapture
from test_homekit_audio_codec import source_audio
from test_homekit_audio_timing import make_track
from test_homekit_live import CONFIG, encoded_picture, native_crypto, prepared, reconstruct

from halo_server.homekit.controller import NativeLiveHandle, NativeRecordingHandle
from halo_server.media.homekit_audio_codec import AudioAdapter
from halo_server.media.opus import AudioConfiguration
from halo_server.media.rtp import annexb_nalus


class Trace:
    def __init__(self, *, broken=False):
        self.events, self.media_items = [], []
        self.broken = broken
        self.streams = 0

    def allocate_stream(self):
        self.streams += 1
        return self.streams

    def event(self, stage, track, **fields):
        if self.broken:
            raise BaseException("diagnostics are independent of media")
        self.events.append((stage, track, fields))

    def media(self, stage, track, payload, **fields):
        if self.broken:
            raise BaseException("diagnostics are independent of media")
        self.media_items.append((stage, track, payload, fields))

    def trigger(self, _reason):
        if self.broken:
            raise BaseException("diagnostics are independent of media")


@pytest.mark.asyncio
@pytest.mark.parametrize("adapt", [False, True])
async def test_real_video_wire_payload_and_pts_are_unchanged_by_recording(adapt):
    results = []
    trace = Trace()
    for recorder in (None, trace, Trace(broken=True)):
        manager, controller, identity, result, receiver = await prepared()
        controller.handle.diagnostic_trace = recorder
        source = encoded_picture()
        controller.handle.buffer.append_video(source, 90000)
        session = manager._sessions[identity]
        session.output_base = 12000
        session.packetizer.sequence = 400
        crypto = native_crypto(result.ssrc)
        clear = []
        try:
            config = replace(CONFIG, width=160, height=128) if adapt else CONFIG
            await manager.start(controller.boot, controller.scope, identity, config)
            async with asyncio.timeout(5):
                while not clear or not clear[-1][1] & 0x80:
                    protected = await asyncio.get_running_loop().sock_recv(receiver, 2048)
                    if 192 <= protected[1] <= 223:
                        continue
                    clear.append(crypto.unprotect(protected))
            results.append((reconstruct(clear), [item[:8] for item in clear]))
        finally:
            receiver.close()
            await manager.close()
    assert results[0] == results[1] == results[2]
    assert trace.streams == 1
    assert [item[0] for item in trace.media_items] == ["encoded_output"]
    assert annexb_nalus(trace.media_items[0][2]) == results[0][0]
    assert (trace.media_items[0][2] != source) is adapt
    frame_events = [fields for stage, _, fields in trace.events if stage == "encoded_output"]
    assert frame_events == [trace.media_items[0][3]]
    assert frame_events[0]["size"] == len(trace.media_items[0][2])
    stages = [stage for stage, _, _ in trace.events]
    for required in (
        "source_read",
        "conversion_start",
        "conversion_end",
        "timeline",
        "rtp_send",
        "packet_schedule",
        "packet_sent",
        "rtp_sent",
    ):
        assert required in stages
    scheduled = [fields for stage, _, fields in trace.events if stage == "packet_sent"]
    assert all(item["actual_us"] >= item["scheduled_us"] for item in scheduled)
    assert all(fields["stream_id"] == 1 for _, _, fields in trace.events)
    assert all(
        type(value) in (int, bool) for _, _, fields in trace.events for value in fields.values()
    )


@pytest.mark.asyncio
async def test_real_audio_output_unchanged_with_failing_or_active_trace(monkeypatch):
    source = source_audio(rate=48000, channels=2, duration=60, count=4)
    results = []
    trace = Trace()
    for recorder in (None, trace, Trace(broken=True)):
        track = make_track(monkeypatch, [100.0])
        track._clock = time.monotonic
        track.config = AudioConfiguration(24000, 20, "cbr", 24)
        track._adapter = AudioAdapter(track.config)
        track._adapter_factory = AudioAdapter
        track._diagnostic_trace, track._diagnostic_stream_id = recorder, 7
        track._base, track._sequence = 12000, 400
        sent = []
        # Observe clear transport bytes at protection entry; the existing real
        # SRTP/video test independently covers encrypted transport equivalence.
        original = track.secure.protect

        def protect(payload, sent=sent, original=original):
            sent.append(payload)
            return original(payload)

        async def send(_packet, **_options):
            return True

        monkeypatch.setattr(track.secure, "protect", protect)
        monkeypatch.setattr(track, "_send", send)
        try:
            for index, payload in enumerate(source):
                packet = SimpleNamespace(
                    payload=payload,
                    rtp_timestamp=index * 2880,
                    received_at=time.monotonic(),
                    capture_epoch=1,
                )
                batch = SimpleNamespace(packets=(packet,), capture_epoch=1, resynchronized=False)
                await track._audio_batch(batch)
                while track._outputs:
                    await track._send_output(track._outputs[0])
            results.append([(packet[:8], packet[12:]) for packet in sent])
        finally:
            await track.close()
    assert results[0] and results[0] == results[1] == results[2]
    outputs = [(payload, fields["pts"]) for _, _, payload, fields in trace.media_items]
    assert len(outputs) == len(results[0])
    frame_events = [fields for stage, _, fields in trace.events if stage == "encoded_output"]
    assert frame_events == [fields for _, _, _, fields in trace.media_items]
    assert [payload for payload, _ in outputs] == [payload for _, payload in results[0]]
    assert all(b[1] - a[1] == 960 for a, b in zip(outputs, outputs[1:], strict=False))
    stages = {stage for stage, _, _ in trace.events}
    assert {
        "source_read",
        "conversion_start",
        "conversion_end",
        "packet_schedule",
        "rtp_send",
        "rtp_sent",
        "timeline",
    } <= stages
    assert all(fields["stream_id"] == 7 for _, _, fields in trace.events)
    assert all(
        fields["actual_us"] >= fields["scheduled_us"]
        for stage, _, fields in trace.events
        if stage == "rtp_sent"
    )


def test_live_handle_propagates_trace_but_recording_handle_never_does():
    trace = Trace()
    claim = SimpleNamespace(resource=SimpleNamespace(diagnostic_trace=trace))
    assert NativeLiveHandle(None, claim).diagnostic_trace is trace
    assert NativeRecordingHandle(None, claim).diagnostic_trace is None
    assert NativeLiveHandle(None, SimpleNamespace(resource=None)).diagnostic_trace is None


@pytest.mark.asyncio
async def test_normal_view_end_preserves_media_through_closed_cleanup(tmp_path):
    capture = PausedCapture(
        tmp_path / "end-window", pre_trigger_seconds=0.1, post_trigger_seconds=0.01
    )
    manager, controller, identity, _, receiver = await prepared()
    controller.handle.diagnostic_trace = capture
    controller.handle.buffer.append_video(encoded_picture(), 90000)
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        async with asyncio.timeout(2):
            while not manager._sessions[identity].trace_first_frame:
                await asyncio.sleep(0.001)
        await manager.stop(controller.boot, controller.scope, identity)
        capture.release.set()
        # Longer than the entire pre-trigger window, modelling the normal
        # 25-second cleanup tail after closing a 10-second rolling excerpt.
        await asyncio.sleep(0.15)
        assert capture.event("lifecycle", "session", reason="stop")
        result = capture.close()
        assert result["finished"]
        assert result["trigger"]["reason"] == "stop"
        assert result["trigger"]["kind"] == "trigger"
        assert (capture.directory / "media.bin").read_bytes()
        index = [
            json.loads(line)
            for line in (capture.directory / "media.jsonl").read_text().splitlines()
        ]
        assert index[0]["stage"] == "encoded_output"
    finally:
        capture.release.set()
        capture.close()
        receiver.close()
        await manager.close()
