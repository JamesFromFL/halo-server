"""Actual isolated recording mux, codec compatibility and retirement contracts."""

import asyncio
import contextlib
import math
import signal
import struct
import sys
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import av
import pytest
from test_homekit_audio_codec import source_audio
from test_media_h264 import encode_source

from halo_server.media import recording as recording_module
from halo_server.media.h264 import VideoConfiguration, split_annexb
from halo_server.media.mp4 import Mp4Error, inspect_fragment, inspect_init
from halo_server.media.recording import (
    RecordingAudioConfiguration,
    RecordingConfiguration,
    RecordingError,
    RecordingWorker,
    recording_capabilities,
    retire_recording_workers,
)


def sample(payload, timestamp, sequence, *, kind="video", epoch=1):
    return SimpleNamespace(
        kind=kind,
        payload=payload,
        rtp_timestamp=timestamp,
        received_at=10 + timestamp / (90000 if kind == "video" else 48000),
        sequence=sequence,
        capture_epoch=epoch,
        config_epoch=1,
    )


def parameter_sets(payload):
    nals = split_annexb(payload)
    return next(nal for nal in nals if nal[0] & 31 == 7), next(
        nal for nal in nals if nal[0] & 31 == 8
    )


@pytest.mark.asyncio
async def test_actual_video_only_copy_fragments_are_independently_decodable():
    source = encode_source(width=1280, height=720, fps=15, profile="high", level="4.0", frames=90)
    sps, pps = parameter_sets(source[0][0])
    worker = RecordingWorker(
        RecordingConfiguration(VideoConfiguration(1280, 720, 15, "high", "4.0", 2000)),
        video_sps=sps,
        video_pps=pps,
        capture_epoch=1,
        config_epoch=1,
    )
    output = []
    try:
        for index, (payload, timestamp) in enumerate(source):
            output.extend(await worker.feed(sample(payload, timestamp, index)))
        output.extend(await worker.finish())
    finally:
        await worker.close()
    assert output[0].kind == "init"
    assert len(output) == 4
    for packet in output[1:]:
        assert packet.kind == "fragment" and 0 < packet.duration_seconds <= 4
        movie = av.open(BytesIO(output[0].payload + packet.payload), options={"threads": "1"})
        assert len(movie.streams) == 1
        assert len(list(movie.decode(video=0))) == 30
    assert worker.pid is None


def native_video(width, height, fps, *, profile="high", seconds=6, bframes=0):
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = width, height, "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 90000), Fraction(fps)
    encoder.thread_count, encoder.max_b_frames, encoder.gop_size = 1, bframes, fps * 2
    encoder.options = {
        "profile": profile,
        "level": "4.0",
        "preset": "veryfast",
        "x264-params": f"bframes={bframes}:scenecut=0:repeat-headers=1:threads=1",
    }
    if not bframes:
        encoder.options["tune"] = "zerolatency"
    encoder.open()
    packets = []
    for index in range(fps * seconds):
        frame = av.VideoFrame(width, height, "yuv420p")
        for channel, plane in enumerate(frame.planes):
            plane.update(bytes([40 + index % 180 if channel == 0 else 128]) * plane.buffer_size)
        frame.pts, frame.time_base = index * 90000 // fps, Fraction(1, 90000)
        packets.extend(encoder.encode(frame))
    packets.extend(encoder.encode(None))
    return [(bytes(packet), round(packet.pts * packet.time_base * 90000)) for packet in packets]


def avcc_nals(payload):
    result, offset = [], 0
    while offset < len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        offset += 4
        assert 0 < length <= len(payload) - offset
        result.append(payload[offset : offset + length])
        offset += length
    return result


def inspect_recording(output, *, audio, expected_width, expected_height):
    assert output[0].kind == "init"
    init = output[0].payload
    metadata = inspect_init(init, audio=audio)
    vcl, counts = [], {"video": 0, "audio": 0}
    for item in output[1:]:
        inspected = inspect_fragment(item.payload, metadata)
        assert 0 < inspected.duration_seconds <= 4
        container = av.open(BytesIO(init + item.payload), options={"threads": "1"})
        assert [stream.type for stream in container.streams] == (
            ["video", "audio"] if audio else ["video"]
        )
        local = {"video": 0, "audio": 0}
        raw_audio = None
        raw_audio_frames = 0
        if audio:
            raw_audio = av.CodecContext.create("aac", "r")
            raw_audio.thread_count = 1
            raw_audio.extradata = container.streams.audio[0].codec_context.extradata
        for packet in container.demux():
            if packet.size and packet.stream.type == "audio":
                raw_audio_frames += len(raw_audio.decode(av.Packet(bytes(packet))))
            if packet.size and packet.stream.type == "video":
                vcl.extend(nal for nal in avcc_nals(bytes(packet)) if nal[0] & 31 in {1, 5})
            for frame in packet.decode():
                assert not frame.is_corrupt
                if packet.stream.type == "video":
                    assert (frame.width, frame.height) == (expected_width, expected_height)
                else:
                    assert (
                        frame.sample_rate in {16000, 24000, 48000}
                        and len(frame.layout.channels) == 1
                    )
                local[packet.stream.type] += 1
                counts[packet.stream.type] += 1
        assert local["video"] > 0
        if audio:
            raw_audio_frames += len(raw_audio.decode(None))
            assert raw_audio_frames > 0
    if audio:
        # AAC edit-list priming belongs to the complete movie, not each late
        # fragment opened again with the global initialization segment.
        full = av.open(
            BytesIO(init + b"".join(item.payload for item in output[1:])), options={"threads": "1"}
        )
        counts["audio"] = sum(1 for _ in full.decode(audio=0))
    return vcl, counts


async def convert(source, selected, *, audio=False, rate=24000, mode="vbr", receipt_by_index=False):
    sps, pps = parameter_sets(source[0][0])
    configuration = RecordingConfiguration(
        selected, RecordingAudioConfiguration(rate) if audio else None
    )
    worker = RecordingWorker(
        configuration, video_sps=sps, video_pps=pps, capture_epoch=1, config_epoch=1
    )
    inputs = []
    source_fps = round(
        90000 / (sorted(stamp for _, stamp in source)[1] - sorted(stamp for _, stamp in source)[0])
    )
    for index, (payload, stamp) in enumerate(source):
        item = sample(payload, stamp, index)
        if receipt_by_index:
            item.received_at = 10 + index / source_fps
        inputs.append(item)
    if audio:
        seconds = (max(stamp for _, stamp in source) + 90000 // source_fps) / 90000
        packets = source_audio(channels=2, mode=mode, count=math.ceil(seconds * 50))
        inputs.extend(
            sample(payload, index * 960, index, kind="audio")
            for index, payload in enumerate(packets)
        )
    output = []
    try:
        for item in sorted(inputs, key=lambda item: (item.received_at, item.kind)):
            output.extend(await worker.feed(item))
        output.extend(await worker.finish())
    finally:
        await worker.close()
    assert worker.pid is None and not worker.pending_resources
    return output


@pytest.mark.asyncio
@pytest.mark.parametrize("dimensions", [(1280, 720), (1920, 1080)])
@pytest.mark.parametrize("fps", [15, 24, 30])
@pytest.mark.parametrize("audio", [False, True])
async def test_required_geometry_and_rate_copy_AAC_matrix(dimensions, fps, audio):
    width, height = dimensions
    source = native_video(width, height, fps)
    output = await convert(
        source, VideoConfiguration(width, height, fps, "high", "4.0", 2000), audio=audio
    )
    vcl, counts = inspect_recording(
        output, audio=audio, expected_width=width, expected_height=height
    )
    expected = [
        nal for payload, _ in source for nal in split_annexb(payload) if nal[0] & 31 in {1, 5}
    ]
    assert vcl == expected
    assert counts["video"] == fps * 6
    assert len(output) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [16000, 24000, 48000])
async def test_every_advertised_AAC_rate_is_actual_LC_mono(rate):
    source = native_video(1280, 720, 15, seconds=4)
    output = await convert(
        source, VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), audio=True, rate=rate
    )
    _, counts = inspect_recording(output, audio=True, expected_width=1280, expected_height=720)
    assert counts["audio"] > 0
    info = inspect_init(output[0].payload, audio=True)
    assert next(track for track in info.tracks if track.kind == "audio").sample_rate == rate


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["baseline", "main", "high"])
async def test_actual_adaptation_matches_selected_profile_and_geometry(profile):
    source = native_video(640, 360, 15, seconds=4)
    output = await convert(source, VideoConfiguration(1280, 720, 15, profile, "4.0", 2000))
    vcl, counts = inspect_recording(output, audio=False, expected_width=1280, expected_height=720)
    assert counts["video"] == 60
    assert vcl != [
        nal for payload, _ in source for nal in split_annexb(payload) if nal[0] & 31 in {1, 5}
    ]


@pytest.mark.asyncio
async def test_actual_B_frame_source_uses_native_no_B_adaptation_with_correct_PTS():
    source = native_video(1280, 720, 15, seconds=4, bframes=2)
    assert [stamp for _, stamp in source] != sorted(stamp for _, stamp in source)
    output = await convert(
        source, VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), receipt_by_index=True
    )
    _, counts = inspect_recording(output, audio=False, expected_width=1280, expected_height=720)
    assert counts["video"] == 60


@pytest.mark.asyncio
async def test_actual_rate_conversion_duplicates_only_pictures_and_has_selected_cadence():
    source = native_video(1280, 720, 15, seconds=4)
    output = await convert(source, VideoConfiguration(1280, 720, 30, "high", "4.0", 2000))
    _, counts = inspect_recording(output, audio=False, expected_width=1280, expected_height=720)
    assert counts["video"] == 120


@pytest.mark.asyncio
async def test_initial_actual_cadence_faster_than_SPS_forces_adaptation():
    source = [(payload, stamp // 2) for payload, stamp in native_video(1280, 720, 15, seconds=4)]
    output = await convert(source, VideoConfiguration(1280, 720, 15, "high", "4.0", 2000))
    _, counts = inspect_recording(output, audio=False, expected_width=1280, expected_height=720)
    assert counts["video"] == 30


@pytest.mark.asyncio
async def test_actual_native_preflight_and_two_worker_capacity():
    assert (await recording_capabilities()).ready
    source = native_video(320, 240, 15, seconds=1)
    sps, pps = parameter_sets(source[0][0])

    def make():
        return RecordingWorker(
            RecordingConfiguration(VideoConfiguration(320, 240, 15, "high", "4.0", 2000)),
            video_sps=sps,
            video_pps=pps,
            capture_epoch=1,
            config_epoch=1,
        )

    workers = [make(), make(), make()]
    baseline = recording_module._WORKERS
    try:
        await workers[0].start()
        await workers[1].start()
        with pytest.raises(RecordingError, match="unavailable"):
            await workers[2].start()
        assert baseline + 2 == recording_module._WORKERS
    finally:
        for worker in workers:
            await worker.close()
    assert baseline == recording_module._WORKERS


@pytest.fixture(scope="module")
def container_fixture():
    async def render():
        return await convert(
            native_video(1280, 720, 15, seconds=4),
            VideoConfiguration(1280, 720, 15, "main", "4.0", 2000),
            audio=True,
        )

    return asyncio.run(render())


@pytest.mark.parametrize("which", ["init", "fragment"])
def test_every_truncated_prefix_and_bounded_mutation_is_a_typed_parser_result(
    container_fixture, which
):
    import random

    init = container_fixture[0].payload
    movie = inspect_init(init, audio=True)
    payload = init if which == "init" else container_fixture[1].payload
    inspect = (
        (lambda value: inspect_init(value, audio=True))
        if which == "init"
        else (lambda value: inspect_fragment(value, movie))
    )
    for count in range(len(payload)):
        with pytest.raises(Mp4Error):
            inspect(payload[:count])
    rng = random.Random(1711)
    for _ in range(500):
        changed = bytearray(payload)
        changed[rng.randrange(len(changed))] ^= rng.randrange(1, 256)
        with contextlib.suppress(Mp4Error):
            inspect(bytes(changed))


@pytest.mark.parametrize(
    "mutation", ["zero-size", "oversized", "tfdt-year", "offset", "non-sync", "esds"]
)
def test_malformed_atom_metadata_rejected_without_payload_reallocation(container_fixture, mutation):
    init, fragment = container_fixture[0].payload, container_fixture[1].payload
    movie = inspect_init(init, audio=True)
    changed = bytearray(init if mutation == "esds" else fragment)
    if mutation == "zero-size":
        changed[:4] = bytes(4)
    elif mutation == "oversized":
        changed[:4] = (8 * 1024 * 1024 + 1).to_bytes(4, "big")
    elif mutation == "tfdt-year":
        pos = changed.index(b"tfdt") + 8
        changed[pos : pos + 8] = (2**64 - 1).to_bytes(8, "big")
    elif mutation == "offset":
        pos = changed.index(b"trun") + 12
        changed[pos : pos + 4] = (2**31 - 1).to_bytes(4, "big")
    elif mutation == "non-sync":
        pos = changed.index(b"trun") + 16
        changed[pos : pos + 4] = (0x01010000).to_bytes(4, "big")
    else:
        pos = changed.index(b"esds")
        size = int.from_bytes(changed[pos - 4 : pos], "big")
        changed[pos + 4 : pos - 4 + size] = bytes(size - 8)
    with pytest.raises(Mp4Error):
        if mutation == "esds":
            inspect_init(bytes(changed), audio=True)
        else:
            inspect_fragment(bytes(changed), movie)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["profile", "fps", "level", "AAC-rate"])
async def test_faulty_child_init_cannot_relabel_output_configuration(
    container_fixture, monkeypatch, mismatch
):
    selected = VideoConfiguration(1280, 720, 15, "main", "4.0", 2000)
    rate = 24000
    if mismatch == "profile":
        selected = VideoConfiguration(1280, 720, 15, "high", "4.0", 2000)
    elif mismatch == "fps":
        selected = VideoConfiguration(1280, 720, 30, "main", "4.0", 2000)
    elif mismatch == "level":
        selected = VideoConfiguration(1280, 720, 15, "main", "3.1", 2000)
    else:
        rate = 16000
    track = next(
        t
        for t in inspect_init(container_fixture[0].payload, audio=True).tracks
        if t.kind == "video"
    )
    worker = RecordingWorker(
        RecordingConfiguration(selected, RecordingAudioConfiguration(rate)),
        video_sps=track.sps,
        video_pps=track.pps,
        capture_epoch=1,
        config_epoch=1,
    )

    class Input:
        def write(self, _):
            pass

        async def drain(self):
            pass

    async def start():
        worker._process = SimpleNamespace(stdin=Input())

    async def read():
        data = container_fixture[0].payload
        return b"O\1I" + struct.pack("!I", len(data)) + data

    async def close():
        worker._closed = True

    monkeypatch.setattr(worker, "start", start)
    monkeypatch.setattr(worker, "_read", read)
    monkeypatch.setattr(worker, "close", close)
    with pytest.raises(RecordingError) as error:
        await worker._request(b"P", b"", 0, 0)
    assert error.value.code == "invalid_output" and worker._closed


@pytest.mark.asyncio
async def test_tiny_final_GOP_preserves_raw_AAC_and_full_movie_priming_and_tail():
    source = native_video(1280, 720, 15, seconds=4)
    source.append(native_video(1280, 720, 15, seconds=1)[0])
    source[-1] = (source[-1][0], 360000)
    output = await convert(
        source, VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), audio=True
    )
    _, counts = inspect_recording(output, audio=True, expected_width=1280, expected_height=720)
    assert counts["video"] == 61 and output[-1].duration_seconds < Fraction(1, 5)
    # No last fragment or short AAC payload is discarded to make an isolated
    # container apply the whole-movie priming edit for a second time.
    clip = av.open(
        BytesIO(output[0].payload + b"".join(p.payload for p in output[1:])),
        options={"threads": "1"},
    )
    packets = [packet for packet in clip.demux(audio=0) if packet.size]
    assert packets[0].pts == -1024
    assert packets[-1].duration > 0


@pytest.mark.asyncio
async def test_later_copy_source_bitrate_burst_is_retired_before_forwarding():
    source = native_video(1280, 720, 15)
    sps, pps = parameter_sets(source[0][0])
    worker = RecordingWorker(
        RecordingConfiguration(VideoConfiguration(1280, 720, 15, "high", "4.0", 2000)),
        video_sps=sps,
        video_pps=pps,
        capture_epoch=1,
        config_epoch=1,
    )
    output = []
    try:
        for i, (payload, stamp) in enumerate(source[:61]):
            output.extend(await worker.feed(sample(payload, stamp, i)))
        assert output and output[0].kind == "init"
        payload, stamp = source[61]
        # A valid ignored SEI body changes source bytes without changing SPS.
        burst = b"\0\0\0\1\x06" + b"\x55" * 300000 + payload
        with pytest.raises(RecordingError):
            await worker.feed(sample(burst, stamp, 61))
        assert worker.pid is None and not worker.pending_resources
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_source_epoch_and_privacy_revocation_cannot_return_more_packets():
    source = native_video(320, 240, 15, seconds=1)
    sps, pps = parameter_sets(source[0][0])
    valid = [True]
    worker = RecordingWorker(
        RecordingConfiguration(VideoConfiguration(320, 240, 15, "high", "4.0", 2000)),
        video_sps=sps,
        video_pps=pps,
        capture_epoch=1,
        config_epoch=1,
        valid=lambda: valid[0],
    )
    try:
        with pytest.raises(RecordingError):
            await worker.feed(sample(source[0][0], 0, 0, epoch=2))
        assert worker.pid is None
        await worker.feed(sample(source[0][0], 0, 0))
        valid[0] = False
        with pytest.raises(RecordingError):
            await worker.feed(sample(source[1][0], 6000, 1))
    finally:
        await worker.close()
    assert worker.pid is None


def small_worker():
    source = native_video(320, 240, 15, seconds=1)
    sps, pps = parameter_sets(source[0][0])
    return RecordingWorker(
        RecordingConfiguration(VideoConfiguration(320, 240, 15, "high", "4.0", 2000)),
        video_sps=sps,
        video_pps=pps,
        capture_epoch=1,
        config_epoch=1,
    ), source


@pytest.mark.asyncio
async def test_timeout_escalates_ignored_termination_and_reaps(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []

    async def spawn(*args, **kwargs):
        process = await original(
            sys.executable,
            "-c",
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)",
            **kwargs,
        )
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(recording_module, "START_SECONDS", 0.1)
    monkeypatch.setattr(recording_module, "STOP_SECONDS", 0.02)
    worker, _ = small_worker()
    baseline = recording_module._WORKERS
    with pytest.raises(TimeoutError):
        await worker.start()
    assert children[0].returncode == -signal.SIGKILL
    assert not Path(f"/proc/{children[0].pid}").exists()
    assert baseline == recording_module._WORKERS and not worker.pending_resources


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [b"\0\0", struct.pack("!I", 64) + b"S", b"\xff" * 4])
async def test_incomplete_or_oversized_native_response_has_finite_retirement(monkeypatch, response):
    original = asyncio.create_subprocess_exec
    children = []

    async def spawn(*args, **kwargs):
        program = (
            "import sys,time;"
            "n=int.from_bytes(sys.stdin.buffer.read(4),'big');"
            "sys.stdin.buffer.read(n);"
            f"sys.stdout.buffer.write({response!r});"
            "sys.stdout.buffer.flush();time.sleep(30)"
        )
        process = await original(sys.executable, "-c", program, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(recording_module, "START_SECONDS", 0.1)
    worker, _ = small_worker()
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises((TimeoutError, RecordingError)):
            await worker.start()
        assert asyncio.get_running_loop().time() - started < 1
        assert children[0].returncode is not None
        assert not Path(f"/proc/{children[0].pid}").exists()
        assert not worker.pending_resources
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_failed_reap_keeps_capacity_and_global_retry_ownership(monkeypatch):
    worker, _ = small_worker()
    await worker.start()
    process = worker._process
    wait, terminate, kill = process.wait, process.terminate, process.kill

    async def stalled_wait():
        await asyncio.sleep(60)

    monkeypatch.setattr(process, "wait", stalled_wait)
    monkeypatch.setattr(process, "terminate", lambda: None)
    monkeypatch.setattr(process, "kill", lambda: None)
    monkeypatch.setattr(recording_module, "STOP_SECONDS", 0.01)
    try:
        with pytest.raises(RecordingError) as error:
            await worker.close()
        assert error.value.code == "cleanup_pending"
        assert worker in recording_module._OWNED and worker.pending_resources
        monkeypatch.setattr(recording_module, "MAX_WORKERS", recording_module._WORKERS)
        other, _ = small_worker()
        with pytest.raises(RecordingError):
            await other.start()
    finally:
        monkeypatch.setattr(process, "wait", wait)
        monkeypatch.setattr(process, "terminate", terminate)
        monkeypatch.setattr(process, "kill", kill)
        await retire_recording_workers()
    assert process.returncode is not None and not worker.pending_resources


@pytest.mark.asyncio
async def test_cancellation_of_stalled_input_joins_worker_before_return(monkeypatch):
    worker, source = small_worker()
    await worker.start()
    pid = worker.pid
    entered = asyncio.Event()

    async def stalled():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker._process.stdin, "drain", stalled)
    task = asyncio.create_task(worker.feed(sample(source[0][0], 0, 0)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.pid is None and not worker.pending_resources
    assert not Path(f"/proc/{pid}").exists()


@pytest.mark.asyncio
async def test_late_spawn_remains_owned_until_closed_and_reaped(monkeypatch):
    original = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    children = []

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        process = await original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    monkeypatch.setattr(recording_module, "STOP_SECONDS", 0.01)
    worker, _ = small_worker()
    start = asyncio.create_task(worker.start())
    await entered.wait()
    try:
        with pytest.raises(RecordingError) as error:
            await worker.close()
        assert error.value.code == "cleanup_pending" and worker in recording_module._OWNED
        release.set()
        with pytest.raises(RecordingError):
            await start
        await worker.close()
    finally:
        release.set()
        if not start.done():
            start.cancel()
            with contextlib.suppress(BaseException):
                await start
        await worker.close()
    assert children[0].returncode is not None and not worker.pending_resources


@pytest.mark.asyncio
async def test_native_startup_qualification_never_retires_an_unrelated_active_job():
    worker, _ = small_worker()
    try:
        await worker.start()
        pid = worker.pid
        assert (await recording_capabilities()).ready
        assert worker.pid == pid and worker.pending_resources
    finally:
        await worker.close()


@pytest.mark.parametrize(
    "changes", [{"bitrate_mode": "cbr"}, {"sample_rate": 8000}, {"channels": 2}, {"bit_rate": True}]
)
def test_unqualified_audio_modes_and_parameters_reject(changes):
    with pytest.raises(RecordingError):
        RecordingAudioConfiguration(**changes)
