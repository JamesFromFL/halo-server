"""Local diagnostics are read-only and explicit worker probes retain ownership."""

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from halo_server import diagnostics
from halo_server.homekit.bridge import AdapterResources
from halo_server.paths import ServerPaths


def resources(tmp_path):
    entry = tmp_path / "adapter.cjs"
    entry.write_text("// metadata only")
    return AdapterResources(Path(sys.executable), entry)


@pytest.mark.asyncio
async def test_doctor_does_not_create_state_read_secrets_or_spawn_by_default(tmp_path, monkeypatch):
    paths = ServerPaths(tmp_path / "absent-state", tmp_path / "absent-runtime")

    async def forbidden(*args, **kwargs):
        pytest.fail("default doctor spawned a worker")

    monkeypatch.setattr(diagnostics, "_probe", forbidden)
    result = await diagnostics.local_doctor(paths, resources=resources(tmp_path))
    assert not paths.state_directory.exists() and not paths.runtime_directory.exists()
    assert result["probe"] is False
    assert result["status"] == "degraded"
    assert result["capabilities"]["real_ring_apple_qualified"] is False
    assert str(tmp_path) not in json.dumps(result)
    assert any(c["code"] == "not-created" for c in result["checks"])


@pytest.mark.asyncio
async def test_doctor_rejects_symlink_and_world_readable_directory_without_repair(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    paths = ServerPaths(insecure, link)
    result = await diagnostics.local_doctor(paths, resources=resources(tmp_path))
    assert {c["name"] for c in result["checks"] if c["code"] == "unsafe-path"} == {
        "state-directory",
        "runtime-directory",
    }
    assert insecure.stat().st_mode & 0o777 == 0o755
    assert link.is_symlink()


@pytest.mark.asyncio
async def test_doctor_only_exposes_expected_probe_metadata(tmp_path, monkeypatch):
    calls = []

    async def probe(command):
        calls.append(command)
        if command[-1] == "--version":
            return b"v26.12.1\n"
        return b'{"codecs":{"libx264":true,"aac":true,"libopus":true,"mjpeg":true}}'

    monkeypatch.setattr(diagnostics, "_probe", probe)
    result = await diagnostics.local_doctor(
        ServerPaths(tmp_path, tmp_path), probe=True, resources=resources(tmp_path)
    )
    assert len(calls) == 2
    assert {c["code"] for c in result["checks"] if c["name"].endswith("-probe")} == {
        "qualified",
        "available",
    }
    assert result["capabilities"]["live_camera_probed"] is False
    assert "codecs" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "overflow", "cancel"])
async def test_probe_timeout_output_limit_and_cancellation_reap_real_child(
    tmp_path, monkeypatch, mode
):
    pidfile = tmp_path / "pid"
    script = tmp_path / "probe.py"
    script.write_text(
        "import os,time,sys\n"
        + f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
        + (
            "sys.stdout.write('x'*8192);sys.stdout.flush();time.sleep(60)\n"
            if mode == "overflow"
            else "time.sleep(60)\n"
        )
    )
    monkeypatch.setattr(diagnostics, "PROBE_TIMEOUT", 0.5)
    task = asyncio.create_task(diagnostics._probe([sys.executable, "-I", str(script)]))
    async with asyncio.timeout(3):
        while not pidfile.exists():
            await asyncio.sleep(0.01)
    pid = int(pidfile.read_text())
    if mode == "cancel":
        task.cancel()
    with pytest.raises(
        {"timeout": TimeoutError, "overflow": ValueError, "cancel": asyncio.CancelledError}[mode]
    ):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_probe_rejects_nonzero_without_returning_stderr():
    with pytest.raises(ValueError, match="worker failed"):
        await diagnostics._probe(
            [sys.executable, "-I", "-c", "import sys;print('secret',file=sys.stderr);sys.exit(2)"]
        )


@pytest.mark.asyncio
async def test_cancellation_during_probe_spawn_joins_and_reaps_late_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    children = []

    async def delayed_spawn(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        entered.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(
        diagnostics._probe([sys.executable, "-I", "-c", "import time;time.sleep(60)"])
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(children) == 1 and children[0].returncode is not None


@pytest.mark.asyncio
async def test_probe_checks_entire_fragmented_output_before_success():
    code = "import sys,time;print('prefix',flush=True);time.sleep(.03);print('x'*8192,flush=True)"
    with pytest.raises(ValueError, match="output limit"):
        await diagnostics._probe([sys.executable, "-I", "-c", code])


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,descriptor", [("stdout", 1), ("stderr", 2)])
@pytest.mark.parametrize("cancel_during_spawn", [False, True])
async def test_probe_reaps_with_full_pipe_before_readers_start(
    monkeypatch, stream, descriptor, cancel_during_spawn
):
    original = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    children = []

    async def delayed_spawn(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        # Let pipe transport collect more than twice its limit before the
        # probe readers can consume anything, as on a busy CI event loop.
        await asyncio.sleep(0.1)
        entered.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    code = f"import os,time;os.write({descriptor},b'x'*12288);time.sleep(60)"
    task = asyncio.create_task(diagnostics._probe([sys.executable, "-I", "-c", code]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        if cancel_during_spawn:
            task.cancel()
        release.set()
        _done, pending = await asyncio.wait({task}, timeout=2)
        if pending:
            # Rescue the old deadlock so this regression fails rather than
            # hanging the suite or abandoning an owned child process.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(children[0].pid, signal.SIGKILL)
            await asyncio.wait_for(getattr(children[0], stream).read(), timeout=2)
        expected = asyncio.CancelledError if cancel_during_spawn else ValueError
        with pytest.raises(expected):
            await asyncio.wait_for(task, timeout=2)
        assert not pending, "Probe reaping waited for a paused pipe to be drained externally"
        assert children[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(children[0].pid, 0)
    finally:
        release.set()
        for child in children:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
