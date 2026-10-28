"""Native worker output, compressed-copy evidence and bounded process ownership."""

import asyncio
import signal
from dataclasses import replace

import av
import pytest
from test_media_h264 import encode_source, source_sps

from halo_server.media import transcode
from halo_server.media.h264 import VIDEO_CATALOG, VideoConfiguration, annexb, split_annexb
from halo_server.media.transcode import AccessUnitAdapter, VideoTransformError, VideoWorker


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", VIDEO_CATALOG)
async def test_every_advertised_format_is_actually_encoded_sps_proven_and_decoded(selected):
    worker = VideoWorker(selected)
    try:
        frames = await worker.probe()
        assert len(frames) == 1
        _, payload = frames[0]
        info = source_sps(payload)
        assert (info.width, info.height, info.profile, info.level) == (
            selected.width,
            selected.height,
            selected.profile,
            selected.level,
        )
        assert info.max_reorder_frames == 0
        decoder = av.CodecContext.create("h264", "r")
        decoder.thread_count = 1
        decoded = decoder.decode(av.Packet(payload))
        assert len(decoded) == 1
        assert (decoded[0].width, decoded[0].height) == (selected.width, selected.height)
        assert abs(bytes(decoded[0].planes[0])[0] - 16) <= 2
    finally:
        await worker.close()
    assert worker.pid is None


@pytest.mark.asyncio
async def test_native_copy_preserves_every_access_unit_byte_and_waits_for_fresh_idr_on_pli():
    source = encode_source(frames=4, force=(3,))
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "baseline", "3.1", 2000))
    try:
        for payload, timestamp in source[:2]:
            output = await adapter.feed(payload, timestamp, capture_epoch=1)
            assert len(output) == 1 and output[0].payload == payload
            assert output[0].mode == "copy" and output[0].timestamp == timestamp
        adapter.request_keyframe()
        assert await adapter.feed(*source[2], capture_epoch=1) == ()
        output = await adapter.feed(*source[3], capture_epoch=1)
        assert output[0].is_keyframe and output[0].payload == source[3][0]
        assert adapter._worker is None
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_separate_parameter_packets_are_repeated_before_initial_copied_idr():
    payload, timestamp = encode_source(frames=1)[0]
    nals = split_annexb(payload)
    parameters = tuple(nal for nal in nals if nal[0] & 31 in {7, 8})
    image = tuple(nal for nal in nals if nal[0] & 31 not in {7, 8})
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "baseline", "3.1", 2000))
    try:
        assert await adapter.feed(annexb(parameters), timestamp, capture_epoch=1) == ()
        output = await adapter.feed(annexb(image), timestamp, capture_epoch=1)
        assert split_annexb(output[0].payload) == parameters + image
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_incompatible_source_is_scaled_letterboxed_profile_converted_and_rate_limited():
    source = encode_source(width=640, height=360, fps=30, profile="high", level="5.0", frames=5)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "baseline", "3.1", 500))
    result = []
    try:
        for payload, timestamp in source:
            result.extend(await adapter.feed(payload, timestamp, capture_epoch=1))
        assert [frame.timestamp for frame in result] == [0, 6000, 12000]
        assert all(frame.mode == "adapt" and frame.output.profile == "baseline" for frame in result)
        decoder = av.CodecContext.create("h264", "r")
        frames = [frame for unit in result for frame in decoder.decode(av.Packet(unit.payload))]
        assert len(frames) == 3
        plane = frames[0].planes[0]
        pixels = bytes(plane)
        assert abs(pixels[100] - 16) <= 2  # Actual black letterbox, not aspect distortion.
        assert abs(pixels[100 * plane.line_size + 100] - 80) <= 4
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_bframe_source_pts_are_decoded_in_presentation_order_without_invented_dts():
    source = encode_source(profile="high", frames=10, bframes=2)
    timestamps = [timestamp for _, timestamp in source]
    assert timestamps != sorted(timestamps)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "high", "3.1", 1000))
    result = []
    try:
        for payload, timestamp in source:
            result.extend(await adapter.feed(payload, timestamp, capture_epoch=1))
        assert len(result) >= 6
        assert [item.timestamp for item in result] == sorted(item.timestamp for item in result)
        assert all(item.mode == "adapt" for item in result)
        decoder = av.CodecContext.create("h264", "r")
        decoded = [frame for item in result for frame in decoder.decode(av.Packet(item.payload))]
        assert len(decoded) == len(result)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_capture_reset_does_not_combine_previous_parameters_with_new_delta_frames():
    source = encode_source(frames=2)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000))
    try:
        assert await adapter.feed(*source[0], capture_epoch=1)
        assert await adapter.feed(*source[1], capture_epoch=2) == ()
        assert adapter.mode == "waiting"
        assert await adapter.feed(*source[0], capture_epoch=2)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_transport_can_request_real_adaptation_and_retry_the_same_current_idr():
    source = encode_source(frames=1)[0]
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000))
    try:
        assert (await adapter.feed(*source, capture_epoch=1))[0].mode == "copy"
        adapter.force_adaptation()
        output = await adapter.feed(*source, capture_epoch=1)
        assert output and output[0].mode == "adapt" and output[0].is_keyframe
        assert output[0].output.profile == "baseline"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_reconfiguration_keeps_source_domain_but_requires_a_fresh_source_idr():
    source = encode_source(frames=3, force=(2,))
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000))
    try:
        await adapter.feed(*source[0], capture_epoch=1)
        await adapter.reconfigure(replace(adapter.configuration, width=640, height=480))
        assert await adapter.feed(*source[1], capture_epoch=1) == ()
        result = await adapter.feed(*source[2], capture_epoch=1)
        assert result[0].mode == "adapt" and result[0].output.width == 640
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_encoder_concurrency_is_bounded_and_reservation_is_returned(monkeypatch):
    monkeypatch.setattr(transcode, "MAX_ENCODERS", 2)
    initial = transcode._ENCODERS
    workers = [
        VideoWorker(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000)) for _ in range(3)
    ]
    try:
        await workers[0].start()
        await workers[1].start()
        with pytest.raises(VideoTransformError, match="capacity"):
            await workers[2].start()
    finally:
        await asyncio.gather(*(worker.close() for worker in workers))
    assert initial == transcode._ENCODERS


HUNG = r"""
import signal,struct,sys,time
signal.signal(signal.SIGTERM,lambda *_:None)
def read(n):
 b=b''
 while len(b)<n:b+=sys.stdin.buffer.read(n-len(b))
 return b
n=struct.unpack('!I',read(4))[0];read(n)
sys.stdout.buffer.write(struct.pack('!I',1)+b'S');sys.stdout.buffer.flush()
read(14)
time.sleep(100)
"""


@pytest.mark.asyncio
async def test_hung_native_work_is_killed_reaped_and_releases_global_capacity(
    tmp_path, monkeypatch
):
    script = tmp_path / "hung.py"
    script.write_text(HUNG)
    real_spawn = asyncio.create_subprocess_exec
    spawned = []

    async def spawn(*args, **kwargs):
        child = await real_spawn(args[0], args[1], str(script), **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(transcode.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(transcode, "FRAME_SECONDS", 0.03)
    monkeypatch.setattr(transcode, "STOP_SECONDS", 0.03)
    initial = transcode._ENCODERS
    worker = VideoWorker(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000))
    try:
        with pytest.raises(TimeoutError):
            await worker.feed(b"encoded-fixture", 0)
        assert spawned[0].returncode == -signal.SIGKILL
        assert worker.pid is None and initial == transcode._ENCODERS
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_close_joins_a_late_process_spawn_instead_of_releasing_its_reservation(monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    spawned = []

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        child = await real_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(transcode.asyncio, "create_subprocess_exec", spawn)
    initial = transcode._ENCODERS
    worker = VideoWorker(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000))
    starting = asyncio.create_task(worker.start())
    try:
        await entered.wait()
        closing = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        assert not closing.done() and initial + 1 == transcode._ENCODERS
        release.set()
        await asyncio.wait_for(closing, 2)
        outcome = await asyncio.gather(starting, return_exceptions=True)
        assert isinstance(outcome[0], Exception)
        assert spawned[0].returncode is not None and worker.pid is None
        assert initial == transcode._ENCODERS
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await worker.close()


@pytest.mark.asyncio
async def test_observed_faster_timestamps_force_adaptation_even_if_sps_claims_target_rate():
    source = encode_source(frames=3, force=(2,))
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000))
    try:
        assert (await adapter.feed(source[0][0], 0, capture_epoch=1))[0].mode == "copy"
        assert await adapter.feed(source[1][0], 3000, capture_epoch=1) == ()
        result = await adapter.feed(source[2][0], 6000, capture_epoch=1)
        assert result[0].mode == "adapt" and result[0].is_keyframe
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_bad_input_is_rejected_before_allocating_an_encoder():
    initial = transcode._ENCODERS
    worker = VideoWorker(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000))
    try:
        with pytest.raises(VideoTransformError):
            await worker.feed(b"x" * (8 * 1024 * 1024 + 1), 0)
        assert worker.pid is None and initial == transcode._ENCODERS
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_failed_worker_reaping_keeps_capacity_until_cleanup_retry(monkeypatch):
    monkeypatch.setattr(transcode, "MAX_ENCODERS", 1)
    monkeypatch.setattr(transcode, "STOP_SECONDS", 0.01)
    worker = VideoWorker(VideoConfiguration(320, 240, 15, "baseline", "3.1", 1000))
    blocked = VideoWorker(worker.configuration)
    initial = transcode._ENCODERS
    await worker.start()
    process = worker._process
    real_wait = process.wait

    async def unfinished_wait():
        await asyncio.Event().wait()

    monkeypatch.setattr(process, "wait", unfinished_wait)
    try:
        with pytest.raises(VideoTransformError, match="retirement is incomplete"):
            await worker.close()
        assert worker._reserved and initial + 1 == transcode._ENCODERS
        assert worker in transcode._RETIRING_WORKERS
        with pytest.raises(VideoTransformError, match="capacity"):
            await blocked.start()
        monkeypatch.setattr(process, "wait", real_wait)
        await transcode.retire_video_workers()
        assert not worker._reserved and initial == transcode._ENCODERS
        assert worker not in transcode._RETIRING_WORKERS
    finally:
        monkeypatch.setattr(process, "wait", real_wait)
        await worker.close()
        await blocked.close()


@pytest.mark.asyncio
async def test_adapted_pli_forces_next_actual_idr_with_repeated_parameters():
    source = encode_source(profile="high", level="5.0", frames=2)
    adapter = AccessUnitAdapter(VideoConfiguration(320, 240, 15, "main", "3.1", 1000))
    try:
        assert (await adapter.feed(*source[0], capture_epoch=1))[0].mode == "adapt"
        assert not any(nal[0] & 31 == 5 for nal in split_annexb(source[1][0]))
        adapter.request_keyframe()
        output = (await adapter.feed(*source[1], capture_epoch=1))[0]
        assert output.is_keyframe
        assert {7, 8, 5} <= {nal[0] & 31 for nal in split_annexb(output.payload)}
        decoder = av.CodecContext.create("h264", "r")
        decoded = decoder.decode(av.Packet(output.payload))
        assert len(decoded) == 1 and (decoded[0].width, decoded[0].height) == (320, 240)
    finally:
        await adapter.close()
