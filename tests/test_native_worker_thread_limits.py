"""Actual native work stays on one CPU/thread without changing the service."""

import asyncio
import os
from pathlib import Path

import pytest
from test_homekit_audio_codec import source_audio
from test_homekit_recording_codec import parameter_sets, sample
from test_homekit_snapshot_codec import jpeg_source
from test_media_h264 import encode_source

from halo_server.media import worker_limits
from halo_server.media.h264 import VideoConfiguration
from halo_server.media.homekit_audio_codec import AudioWorker
from halo_server.media.jpeg import SnapshotConfiguration
from halo_server.media.opus import AudioConfiguration
from halo_server.media.recording import (
    RecordingAudioConfiguration,
    RecordingConfiguration,
    RecordingWorker,
)
from halo_server.media.snapshot_codec import SnapshotWorker
from halo_server.media.transcode import VideoWorker


def observe(pid):
    status = dict(
        line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines()
    )
    assert int(status["Threads"]) == 1
    assert len(os.sched_getaffinity(pid)) == 1


def test_worker_cpu_selection_is_round_robin_over_current_allowed_cpus(monkeypatch):
    monkeypatch.setattr(worker_limits, "_NEXT", 0)
    monkeypatch.setattr(worker_limits.os, "sched_getaffinity", lambda pid: {3, 7})
    assert [worker_limits.worker_environment()["HALO_NATIVE_WORKER_CPU"] for _ in range(3)] == [
        "3",
        "7",
        "3",
    ]
    monkeypatch.setattr(worker_limits.os, "sched_getaffinity", lambda pid: {7})
    assert worker_limits.worker_environment()["HALO_NATIVE_WORKER_CPU"] == "7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rate,mode", [(8000, "encode"), (16000, "encode"), (24000, "encode"), (24000, "decode")]
)
async def test_real_stereo_resampler_graph_cannot_spawn_automatic_cpu_pool(rate, mode):
    parent = os.sched_getaffinity(0)
    worker = AudioWorker(AudioConfiguration(rate, 20, "cbr", 24), mode=mode)
    try:
        for index, payload in enumerate(source_audio(channels=2, count=12)):
            await worker.feed(payload, index * 960)
        observe(worker.pid)
        assert os.sched_getaffinity(0) == parent
    finally:
        await worker.close()
    assert worker.pid is None


@pytest.mark.asyncio
async def test_real_video_worker_cannot_expand_parent_cpu_affinity():
    parent = os.sched_getaffinity(0)
    worker = VideoWorker(VideoConfiguration(1920, 1080, 30, "high", "4.0", 2000))
    try:
        assert await worker.probe()
        observe(worker.pid)
        assert os.sched_getaffinity(0) == parent
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_real_jpeg_decode_resize_encode_child_has_one_thread():
    parent = os.sched_getaffinity(0)
    worker = SnapshotWorker()
    source = jpeg_source(2000, 1000, noise=True)
    task = asyncio.create_task(worker.convert(source, SnapshotConfiguration(1279, 719)))
    observations = 0
    try:
        while not task.done():
            if worker.pid is not None:
                try:
                    if len(os.sched_getaffinity(worker.pid)) != 1:
                        await asyncio.sleep(0.001)
                        continue  # Child has not reached its fixed bootstrap yet.
                    observe(worker.pid)
                except (ProcessLookupError, FileNotFoundError):
                    pass
                else:
                    observations += 1
            await asyncio.sleep(0.001)
        assert await task
        assert observations and os.sched_getaffinity(0) == parent
    finally:
        await worker.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_recording_audio_graph_and_mux_have_one_thread():
    parent = os.sched_getaffinity(0)
    video = encode_source(width=1280, height=720, fps=15, profile="high", level="4.0", frames=61)
    audio = source_audio(channels=2, count=201)
    sps, pps = parameter_sets(video[0][0])
    worker = RecordingWorker(
        RecordingConfiguration(
            VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), RecordingAudioConfiguration()
        ),
        video_sps=sps,
        video_pps=pps,
        capture_epoch=1,
        config_epoch=1,
    )
    inputs = [
        sample(payload, stamp, index, kind="video") for index, (payload, stamp) in enumerate(video)
    ]
    inputs.extend(
        sample(payload, index * 960, index, kind="audio") for index, payload in enumerate(audio)
    )
    try:
        for item in sorted(inputs, key=lambda item: (item.received_at, item.kind)):
            await worker.feed(item)
        observe(worker.pid)
        assert os.sched_getaffinity(0) == parent
        assert await worker.finish()
    finally:
        await worker.close()
    assert worker.pid is None
