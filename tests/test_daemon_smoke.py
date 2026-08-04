import asyncio
import json
import os
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
    daemon = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        socket_path = runtime / "admin.sock"
        for _attempt in range(100):
            if socket_path.exists():
                break
            if daemon.returncode is not None:
                break
            await asyncio.sleep(0.02)
        assert socket_path.exists(), (await daemon.stderr.read()).decode()

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
        if daemon.returncode is None:
            daemon.terminate()
            await daemon.wait()


async def _admin_command(command, state, runtime, environment):
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
    stdout, stderr = await process.communicate()
    return SimpleNamespace(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )
