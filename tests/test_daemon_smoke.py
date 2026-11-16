import asyncio
import contextlib
import json
import os
import signal
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_daemon_status_and_shutdown_commands_end_to_end(tmp_path):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    command = [
        sys.executable,
        "-m",
        "halo_server",
        "daemon",
        "--state-directory",
        str(state),
        "--runtime-directory",
        str(runtime),
        "--log-level",
        "WARNING",
        "--plugin-port",
        "0",
        "--rtsp-port",
        "0",
    ]
    environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
    log_path = tmp_path / "daemon.log"
    with log_path.open("wb") as log:
        daemon = await asyncio.create_subprocess_exec(
            *command,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
        )
        try:
            socket_path = runtime / "admin.sock"
            await _wait_for_daemon_socket(daemon, socket_path, log_path)

            status = await _admin_command("status", state, runtime, environment)
            assert status.returncode == 0, status.stderr.decode()
            payload = json.loads(status.stdout)
            assert payload["service"] == "running"
            assert payload["account"]["authenticated"] is False
            assert payload["events"]["connection"] == "offline"

            shutdown = await _admin_command("shutdown", state, runtime, environment)
            assert shutdown.returncode == 0, shutdown.stderr.decode()
            assert json.loads(shutdown.stdout) == {"stopping": True}
            assert await asyncio.wait_for(daemon.wait(), timeout=5) == 0
            assert not socket_path.exists()
        finally:
            await _stop_process(daemon)


async def _wait_for_daemon_socket(daemon, socket_path, log_path, *, timeout=15):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while daemon.returncode is None:
        if socket_path.exists():
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(0.05, remaining))
    # Reading a live process's pipe here would wait for EOF instead of failing.
    output = log_path.read_text(errors="replace")
    pytest.fail(f"Daemon did not become ready (exit={daemon.returncode}):\n{output}")


async def _stop_process(process, *, timeout=5):
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.wait(), timeout=timeout)


async def _admin_command(command, state, runtime, environment, *, timeout=15):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "halo_server",
        command,
        "--state-directory",
        str(state),
        "--runtime-directory",
        str(runtime),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        pytest.fail(f"Admin command {command!r} did not finish within {timeout} seconds")
    finally:
        await _stop_process(process)
    return SimpleNamespace(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.mark.asyncio
async def test_daemon_startup_wait_accepts_delayed_readiness(tmp_path):
    socket_path = tmp_path / "ready"
    log_path = tmp_path / "delayed.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import pathlib,sys,time; time.sleep(2.1); "
            "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)",
            str(socket_path),
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await _wait_for_daemon_socket(process, socket_path, log_path)
            assert process.returncode is None
            assert socket_path.exists()
        finally:
            await _stop_process(process)
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_daemon_startup_timeout_reports_log_and_kills_unresponsive_process(tmp_path):
    ready_path = tmp_path / "child-ready"
    log_path = tmp_path / "unresponsive.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import pathlib,signal,sys,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('startup fixture stalled', flush=True); "
            "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)",
            str(ready_path),
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await _wait_for_daemon_socket(process, ready_path, log_path)
            with pytest.raises(pytest.fail.Exception, match="startup fixture stalled"):
                await asyncio.wait_for(
                    _wait_for_daemon_socket(
                        process, tmp_path / "missing.sock", log_path, timeout=0.05
                    ),
                    timeout=2,
                )
        finally:
            await _stop_process(process, timeout=0.2)
    assert process.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_admin_command_timeout_reaps_its_process(tmp_path, monkeypatch):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    try:
        with pytest.raises(pytest.fail.Exception, match="Admin command 'status' did not finish"):
            await asyncio.wait_for(
                _admin_command("status", tmp_path, tmp_path, {}, timeout=0.05), timeout=2
            )
    finally:
        await _stop_process(process)
    assert process.returncode is not None
