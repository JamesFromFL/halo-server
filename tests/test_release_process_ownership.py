"""Actual Node/native processes retain the release gate until physical retirement."""

import os
from pathlib import Path

import pytest
from test_homekit_recording_codec import parameter_sets
from test_homekit_runtime_dispatch import NODE, _child
from test_media_h264 import encode_source

from halo_server.homekit.runtime import HomeKitRuntime
from halo_server.media.h264 import VideoConfiguration
from halo_server.media.homekit_audio_codec import AudioWorker
from halo_server.media.opus import AudioConfiguration
from halo_server.media.recording import RecordingConfiguration, RecordingWorker
from halo_server.media.snapshot_codec import SnapshotWorker
from halo_server.media.transcode import VideoWorker
from halo_server.release_guard import ReleaseGuard, ReleaseGuardError


def bind(guard, monkeypatch):
    for key, value in guard.environment().items():
        monkeypatch.setenv(key, value)


def blocked(state):
    with pytest.raises(ReleaseGuardError):
        ReleaseGuard.acquire(state, exclusive=True, nonblocking=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["video", "audio", "recording", "snapshot"])
async def test_native_worker_retains_gate_after_parent_descriptor_close(
    tmp_path, monkeypatch, kind
):
    state = tmp_path / "state"
    guard = ReleaseGuard.acquire(state, create=True)
    bind(guard, monkeypatch)
    identity = os.fstat(guard.fileno())
    fd = guard.fileno()
    if kind == "video":
        worker = VideoWorker(VideoConfiguration(320, 240, 15, "baseline", "3.1", 500))
    elif kind == "audio":
        worker = AudioWorker(AudioConfiguration(24000, 20, "cbr", 24))
    elif kind == "snapshot":
        worker = SnapshotWorker()
    else:
        sps, pps = parameter_sets(encode_source(frames=1)[0][0])
        worker = RecordingWorker(
            RecordingConfiguration(VideoConfiguration(320, 240, 15, "baseline", "3.1", 500)),
            video_sps=sps,
            video_pps=pps,
            capture_epoch=1,
            config_epoch=1,
        )
    try:
        await (worker._start() if kind == "snapshot" else worker.start())
        inherited = Path(f"/proc/{worker.pid}/fd/{fd}").stat()
        assert (identity.st_dev, identity.st_ino) == (inherited.st_dev, inherited.st_ino)
        guard.close()
        blocked(state)
    finally:
        await worker.close()
        guard.close()
    with ReleaseGuard.acquire(state, exclusive=True, nonblocking=True):
        pass


@pytest.mark.asyncio
async def test_hap_child_retains_gate_until_runtime_reaped(tmp_path, monkeypatch):
    state = tmp_path / "state"
    guard = ReleaseGuard.acquire(state, create=True)
    bind(guard, monkeypatch)
    fd = guard.fileno()
    identity = os.fstat(fd)

    async def retired(_boot):
        pass

    runtime = HomeKitRuntime(
        Path(NODE),
        _child(tmp_path),
        build_id="release-retirement",
        max_restarts=0,
        on_retired=retired,
    )
    try:
        await runtime.start()
        inherited = Path(f"/proc/{runtime._process.pid}/fd/{fd}").stat()
        assert inherited.st_ino == identity.st_ino and inherited.st_dev == identity.st_dev
        guard.close()
        blocked(state)
    finally:
        await runtime.stop()
        guard.close()
    with ReleaseGuard.acquire(state, exclusive=True, nonblocking=True):
        pass
