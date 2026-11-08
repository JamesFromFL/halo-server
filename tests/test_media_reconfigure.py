"""Real H.264, live-manager SRTP and report scheduling across configuration changes."""

import asyncio
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

import av
import pytest
from aiortc.rtp import RtcpPacket
from test_diagnostic_output import Trace
from test_homekit_live import native_crypto, observe_video_clock, prepared, reconstruct

from halo_server.media import homekit_live
from halo_server.media.diagnostic_capture import _validated_fields
from halo_server.media.h264 import VideoConfiguration, annexb, split_annexb
from halo_server.media.transcode import AccessUnitAdapter, VideoTransformError

CONFIG = VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000)


def source_pictures(*, width=320):
    """Eleven real one-fps pictures with source IDRs only at zero and eight."""
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = width, 240, "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 90000), Fraction(1)
    encoder.bit_rate, encoder.thread_count = 200000, 1
    encoder.gop_size, encoder.max_b_frames = 8, 0
    encoder.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "profile": "baseline",
        "level": "3.1",
        "x264-params": "keyint=8:min-keyint=8:scenecut=0:repeat-headers=1:bframes=0",
    }
    encoder.open()
    result = []
    for index in range(11):
        frame = av.VideoFrame(width, 240, "yuv420p")
        for number, plane in enumerate(frame.planes):
            plane.update(bytes([70 + index if number == 0 else 128]) * plane.buffer_size)
        frame.pts, frame.time_base = index * 90000, Fraction(1, 90000)
        packets = encoder.encode(frame)
        assert len(packets) == 1
        result.append((bytes(packets[0]), index * 90000))
    assert not encoder.encode(None)
    assert [
        index
        for index, (payload, _) in enumerate(result)
        if any(nal[0] & 31 == 5 for nal in split_annexb(payload))
    ] == [0, 8]
    return result


def adapter_state(adapter):
    # Copy mutable provenance maps so clearing them cannot hide a reset.
    return {
        name: value.copy() if isinstance(value, dict) else value
        for name, value in vars(adapter).items()
        if name != "configuration"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("width,mode", [(320, "copy"), (640, "adapt")])
async def test_rtcp_only_change_preserves_adapter_and_all_eleven_decoded_pictures(width, mode):
    adapter = AccessUnitAdapter(CONFIG)
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    decoded_pts = []
    try:
        for index, (payload, timestamp) in enumerate(source_pictures(width=width)):
            if index == 4:
                before = adapter_state(adapter)
                worker_pid = adapter._worker.pid if adapter._worker is not None else None
                await adapter.reconfigure(replace(CONFIG, rtcp_interval=1.0))
                assert adapter.configuration.rtcp_interval == 1.0
                assert adapter_state(adapter) == before
                assert adapter.mode == mode and not adapter._waiting_idr
                if worker_pid is not None:
                    assert adapter._worker.pid == worker_pid
            outputs = await adapter.feed(
                payload, timestamp, capture_epoch=1, received_at=100.0 + index
            )
            assert len(outputs) == 1
            output = outputs[0]
            assert output.mode == mode and output.configuration_generation == 1
            assert output.capture_epoch == 1 and output.received_at == 100.0 + index
            packet = av.Packet(output.payload)
            packet.pts, packet.time_base = output.timestamp, Fraction(1, 90000)
            decoded_pts.extend(frame.pts for frame in decoder.decode(packet))
        assert decoded_pts == [index * 90000 for index in range(11)]
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"width": 640},
        {"fps": 10},
        {"profile": "high"},
        {"level": "4.0"},
        {"max_bit_rate": 800},
    ],
)
async def test_copy_media_changes_keep_explicit_source_idr_recovery(change):
    source = source_pictures()
    adapter = AccessUnitAdapter(CONFIG)
    try:
        await adapter.feed(*source[0], capture_epoch=1, received_at=100.0)
        await adapter.reconfigure(replace(CONFIG, rtcp_interval=1.0, **change))
        assert adapter.configuration_generation == 2
        assert adapter.mode == "waiting" and adapter._waiting_idr
        assert not adapter._source_receipts and adapter._timestamp is None
        assert await adapter.feed(*source[1], capture_epoch=1, received_at=101.0) == ()
        resumed = await adapter.feed(*source[8], capture_epoch=1, received_at=108.0)
        assert len(resumed) == 1 and resumed[0].is_keyframe
        assert resumed[0].configuration_generation == 2
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [320, 640])
async def test_mtu_change_preserves_input_decoder_and_next_source_p_frame(width):
    source = source_pictures(width=width)
    adapter = AccessUnitAdapter(CONFIG)
    try:
        first = (await adapter.feed(*source[0], capture_epoch=1, received_at=100))[0]
        before = adapter_state(adapter)
        await adapter.reconfigure(replace(CONFIG, mtu=512))
        assert adapter_state(adapter) == before
        second = (await adapter.feed(*source[1], capture_epoch=1, received_at=101))[0]
        assert second.timestamp == first.timestamp + 90000
        assert second.configuration_generation == first.configuration_generation == 1
        with pytest.raises(VideoTransformError, match="payload type"):
            await adapter.reconfigure(replace(CONFIG, payload_type=100))
        assert adapter.configuration == replace(CONFIG, mtu=512)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_repeated_adapted_quality_changes_keep_every_picture_receipt_and_timestamp_wrap():
    adapter = AccessUnitAdapter(CONFIG)
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    changes = {
        2: replace(CONFIG, width=160, height=128, max_bit_rate=132),
        3: CONFIG,
        4: replace(CONFIG, fps=10),
        5: replace(CONFIG, max_bit_rate=299),
        6: replace(CONFIG, profile="high", level="4.0"),
        7: CONFIG,
    }
    decoded, generation = [], 1
    base = (1 << 32) - 180000
    try:
        for index, (payload, pts) in enumerate(source_pictures(width=640)):
            if index in changes:
                worker, pid = adapter._worker, adapter._worker.pid
                await adapter.reconfigure(changes[index])
                generation += 1
                assert adapter._worker is worker and worker.pid == pid
                assert adapter.mode == "adapt" and not adapter._waiting_idr
            output = (
                await adapter.feed(
                    payload, (base + pts) & 0xFFFFFFFF, capture_epoch=1, received_at=100 + index
                )
            )[0]
            assert output.configuration_generation == generation
            assert output.timestamp == base + pts and output.received_at == 100 + index
            if index in changes:
                assert output.is_keyframe
                fresh = av.CodecContext.create("h264", "r")
                fresh.thread_count = 1
                assert len(fresh.decode(av.Packet(output.payload))) == 1
            packet = av.Packet(output.payload)
            packet.pts, packet.time_base = output.timestamp, Fraction(1, 90000)
            decoded.extend(frame.pts for frame in decoder.decode(packet))
        assert decoded == [base + index * 90000 for index in range(11)]
        assert not adapter._source_receipts and not adapter._pending_forces
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_rtcp_only_live_reconfiguration_keeps_all_pictures_and_srtp_timeline():
    manager, controller, identity, prepared_video, receiver = await prepared()
    crypto = native_crypto(prepared_video.ssrc)
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    sequences, timestamps, decoded = [], [], []
    session = manager._sessions[identity]
    session.packetizer.sequence = 65530  # Exercise native SRTP rollover in the same view.
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        assert isinstance(session.adapter, AccessUnitAdapter)
        for index, (payload, timestamp) in enumerate(source_pictures()):
            if index == 4:
                assert not session.awaiting_idr and session.retransmit
                sequence, cached = next(reversed(session.retransmit.items()))
                assert session.repair_queue.enqueue(sequence, cached) == "queued"
                before = (
                    session.socket,
                    session.secure,
                    session.secure._tx,
                    session.packetizer.sequence,
                    session.timeline,
                    session.timeline.snapshot(),
                    session.awaiting_idr,
                    session.idr_requested,
                    session.recovery_since,
                    session.recovery_attempts,
                    session.recovery_revision,
                    dict(session.retransmit),
                    session.retransmit_bytes,
                    session.repair_queue.snapshot(),
                    adapter_state(session.adapter),
                )
                await manager.reconfigure(
                    controller.boot, controller.scope, identity, replace(CONFIG, rtcp_interval=1.0)
                )
                assert (
                    session.socket,
                    session.secure,
                    session.secure._tx,
                    session.packetizer.sequence,
                    session.timeline,
                    session.timeline.snapshot(),
                    session.awaiting_idr,
                    session.idr_requested,
                    session.recovery_since,
                    session.recovery_attempts,
                    session.recovery_revision,
                    dict(session.retransmit),
                    session.retransmit_bytes,
                    session.repair_queue.snapshot(),
                    adapter_state(session.adapter),
                ) == before
                # Avoid an intentional duplicate repair in the decoder stream.
                session.repair_queue.clear()
            controller.handle.buffer.append_video(payload, timestamp)
            clear = []
            async with asyncio.timeout(2):
                while True:
                    protected = await asyncio.get_running_loop().sock_recv(receiver, 2048)
                    if protected[1] == 200:
                        crypto.unprotect_rtcp(protected)
                        continue
                    packet = crypto.unprotect(protected)
                    sequences.append(int.from_bytes(packet[2:4], "big"))
                    clear.append(packet)
                    if packet[1] & 0x80:
                        timestamps.append(int.from_bytes(packet[4:8], "big"))
                        break
            picture = av.Packet(annexb(reconstruct(clear)))
            picture.pts, picture.time_base = timestamp, Fraction(1, 90000)
            decoded.extend(frame.pts for frame in decoder.decode(picture))
        assert decoded == [index * 90000 for index in range(11)]
        assert timestamps == [(timestamps[0] + index * 90000) & 0xFFFFFFFF for index in range(11)]
        assert sequences == [(65530 + index) & 0xFFFF for index in range(len(sequences))]
        assert 0 in sequences
        assert session.adapter.configuration_generation == 1
        assert session.adapter.mode == "copy" and not session.awaiting_idr
        assert session.config.rtcp_interval == session.adapter.configuration.rtcp_interval == 1.0
        assert controller.opens == 1 and session.prepared == prepared_video
    finally:
        await manager.close()
        receiver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"max_bit_rate": 800}, {"mtu": 512}])
async def test_media_and_mtu_live_changes_clear_repairs_but_only_codec_changes_require_idr(change):
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    session.state, session.config, session.handle = "active", CONFIG, controller.handle
    session.adapter = AccessUnitAdapter(CONFIG)
    session.awaiting_idr = False
    cached = (100.0, bytes(1000), 970, controller.handle.capture_epoch, 101.0)
    session.retransmit[1], session.retransmit_bytes = cached, len(cached[1])
    assert session.repair_queue.enqueue(1, cached) == "queued"
    try:
        await manager.reconfigure(
            controller.boot, controller.scope, identity, replace(CONFIG, **change)
        )
        codec_change = "mtu" not in change
        assert session.awaiting_idr is codec_change
        assert session.recovery_revision == int(codec_change)
        assert session.adapter.configuration_generation == 1 + int(codec_change)
        assert not session.retransmit and session.retransmit_bytes == 0
        assert session.repair_queue.snapshot()["pending_packets"] == 0
    finally:
        await manager.close()
        receiver.close()


@pytest.mark.asyncio
async def test_live_quality_changes_keep_srtp_sequence_and_emit_exact_transition_evidence():
    manager, controller, identity, video, receiver = await prepared()
    session = manager._sessions[identity]
    session.packetizer.sequence = 65530
    trace = controller.handle.diagnostic_trace = Trace()
    crypto = native_crypto(video.ssrc)
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    sequences, timestamps, decoded = [], [], []
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        transport = session.socket, session.secure, session.timeline
        for index, (payload, pts) in enumerate(source_pictures(width=640)):
            if index in (2, 3, 4, 5):
                selected = (
                    replace(CONFIG, width=160, height=128, max_bit_rate=132)
                    if index % 2 == 0
                    else CONFIG
                )
                pid = session.adapter._worker.pid
                if index == 3:
                    await manager.reconfigure(
                        controller.boot, controller.scope, identity, replace(CONFIG, fps=12)
                    )
                await manager.reconfigure(controller.boot, controller.scope, identity, selected)
                assert session.adapter._worker.pid == pid
                assert (session.socket, session.secure, session.timeline) == transport
            controller.handle.buffer.append_video(payload, pts)
            clear = []
            async with asyncio.timeout(3):
                while True:
                    protected = await asyncio.get_running_loop().sock_recv(receiver, 2048)
                    if 192 <= protected[1] <= 223:
                        crypto.unprotect_rtcp(protected)
                        continue
                    packet = crypto.unprotect(protected)
                    sequences.append(int.from_bytes(packet[2:4], "big"))
                    clear.append(packet)
                    if packet[1] & 0x80:
                        timestamps.append(int.from_bytes(packet[4:8], "big"))
                        break
            picture = av.Packet(annexb(reconstruct(clear)))
            picture.pts, picture.time_base = pts, Fraction(1, 90000)
            decoded.extend(frame.pts for frame in decoder.decode(picture))
        assert decoded == [index * 90000 for index in range(11)]
        assert timestamps == [(timestamps[0] + i * 90000) & 0xFFFFFFFF for i in range(11)]
        assert sequences == [(65530 + i) & 0xFFFF for i in range(len(sequences))]
        assert controller.opens == 1
        assert session.adapter.decoder_generation == 1
        assert session.adapter.configuration_generation == 6
        assert not session.awaiting_idr
        for stage in ("reconfigure_request", "reconfigure_applied", "reconfigure_first_idr"):
            events = [fields for name, _, fields in trace.events if name == stage]
            expected = [1, 3, 4, 5] if stage == "reconfigure_first_idr" else [1, 2, 3, 4, 5]
            assert [fields["request_revision"] for fields in events] == expected
            assert all(_validated_fields(stage, "video", fields) is not None for fields in events)
            if stage == "reconfigure_applied":
                assert all(
                    fields["decoder_preserved"] and fields["changed_fields"] for fields in events
                )
        encoded = [f for name, _, f in trace.events if name == "encoded_output"]
        assert [f["config_epoch"] for f in encoded] == [1, 1, 2, 4, 5, 6, 6, 6, 6, 6, 6]
        superseded = [f for name, _, f in trace.events if name == "reconfigure_superseded"]
        assert [f["request_revision"] for f in superseded] == [2]
    finally:
        await manager.close()
        receiver.close()


@pytest.mark.asyncio
async def test_mtu_commit_waits_for_inflight_repair_and_preserves_existing_recovery():
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    session.state, session.config, session.handle = "active", CONFIG, controller.handle
    session.adapter = AccessUnitAdapter(CONFIG)
    session.awaiting_idr, session.recovery_revision = True, 4
    await session.send_lock.acquire()
    task = asyncio.create_task(
        manager.reconfigure(controller.boot, controller.scope, identity, replace(CONFIG, mtu=512))
    )
    try:
        async with asyncio.timeout(1):
            while session.adapter.configuration.mtu != 512:
                await asyncio.sleep(0)
        assert not task.done() and session.config.mtu == CONFIG.mtu
        session.send_lock.release()
        await task
        assert session.config.mtu == 512
        assert session.awaiting_idr and session.recovery_revision == 4
    finally:
        if session.send_lock.locked():
            session.send_lock.release()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()
        receiver.close()


@pytest.mark.asyncio
async def test_stop_during_encoder_reconfiguration_never_commits_retired_selection():
    entered, release = asyncio.Event(), asyncio.Event()

    class Worker:
        async def reconfigure(self, _configuration):
            entered.set()
            await release.wait()

        async def close(self):
            pass

    adapter = AccessUnitAdapter(CONFIG)
    adapter._worker, adapter._mode = Worker(), "adapt"
    task = asyncio.create_task(adapter.reconfigure(replace(CONFIG, max_bit_rate=132)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.close()
        release.set()
        with pytest.raises(VideoTransformError, match="retired"):
            await task
        assert adapter.configuration == CONFIG and adapter.configuration_generation == 1
        assert adapter._worker is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.close()


@pytest.mark.asyncio
async def test_report_loop_uses_new_interval_after_current_sleep_without_reset(monkeypatch):
    manager, controller, identity, prepared_video, receiver = await prepared()
    session = manager._sessions[identity]
    session.state, session.config, session.handle = "active", CONFIG, controller.handle
    session.adapter = AccessUnitAdapter(CONFIG)
    now, sleeps = [100.0], []
    manager._clock = lambda: now[0]
    session.last_feedback = now[0]
    observe_video_clock(manager, session)
    session.timeline.sent("video", capture_epoch=session.capture_epoch, source_pts=0)
    session.last_timestamp = session.output_base
    crypto = native_crypto(prepared_video.ssrc)

    async def sleep(interval):
        sleeps.append(interval)
        if len(sleeps) == 3:
            raise asyncio.CancelledError
        if len(sleeps) == 1:
            await manager.reconfigure(
                controller.boot, controller.scope, identity, replace(CONFIG, rtcp_interval=1.0)
            )
        now[0] += interval

    try:
        # Replace only this module's scheduler, keeping socket/event-loop I/O real.
        with monkeypatch.context() as patch:
            patch.setattr(
                homekit_live,
                "asyncio",
                SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError),
            )
            with pytest.raises(asyncio.CancelledError):
                await manager._reports(session)
        assert sleeps == [0.5, 1.0, 1.0]
        reports = []
        async with asyncio.timeout(1):
            for _ in range(2):
                packet = await asyncio.get_running_loop().sock_recv(receiver, 2048)
                reports.append(RtcpPacket.parse(crypto.unprotect_rtcp(packet))[0].sender_info)
        assert reports[1].ntp_timestamp - reports[0].ntp_timestamp == 1 << 32
        assert (reports[1].rtp_timestamp - reports[0].rtp_timestamp) & 0xFFFFFFFF == 90000
        assert session.adapter.configuration_generation == 1 and session.recovery_revision == 0
        assert session.awaiting_idr  # Existing startup wait is preserved too.
    finally:
        await manager.close()
        receiver.close()
