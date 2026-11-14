"""Companion startup exclusively owns writes and retains failed cleanup leases."""

import asyncio
from types import SimpleNamespace

import pytest

from halo_server.paths import ServerPaths
from halo_server.release_guard import ReleaseGuard, ReleaseGuardError
from halo_server.web import server
from halo_server.web.settings import WebSettingsError, configure, web_directory


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    paths = ServerPaths(tmp_path / "state", tmp_path / "run")
    paths.prepare()
    configure(paths)
    calls = []
    flags = {"duplicate": False, "cleanup_failure": False}

    class Console:
        def __init__(self, *args):
            self.started = False

        async def start(self):
            calls.append("journal")
            self.started = True

        async def close(self):
            calls.append("console-close")

    class Runner:
        def __init__(self, console, **kwargs):
            self.console = console

        async def setup(self):
            await self.console.start()

        async def cleanup(self):
            calls.append("runner-cleanup")
            if flags["cleanup_failure"]:
                raise RuntimeError("Synthetic cleanup failure")

    class Health:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            calls.append("claim")
            if flags["duplicate"]:
                raise RuntimeError("Existing companion owns the socket")

        async def stop(self):
            calls.append("health-stop")

    class Site:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            calls.append("listen")

    monkeypatch.setattr(server, "WebConsole", Console)
    monkeypatch.setattr(server, "create_application", lambda console: console)
    monkeypatch.setattr(server, "BoundedRunner", Runner)
    monkeypatch.setattr(server, "AdminServer", Health)
    monkeypatch.setattr(server.web, "TCPSite", Site)
    try:
        yield SimpleNamespace(paths=paths, calls=calls, flags=flags)
    finally:
        retained = server._retained_retirements.pop(paths.state_directory, None)
        if retained:
            if retained[4] is not None:
                retained[4].close()
            retained[0].close()


@pytest.mark.asyncio
async def test_runtime_validation_is_under_guard_and_success_retires_it(runtime, monkeypatch):
    original = server.validate_runtime_state

    def validate(state):
        runtime.calls.append("validate")
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(state, exclusive=True, nonblocking=True)
        original(state)

    monkeypatch.setattr(server, "validate_runtime_state", validate)
    stopped = asyncio.Event()
    stopped.set()
    await server.serve(runtime.paths, stop_event=stopped)
    assert runtime.calls[:4] == ["validate", "claim", "journal", "listen"]
    ReleaseGuard.acquire(runtime.paths.state_directory, exclusive=True, nonblocking=True).close()


@pytest.mark.asyncio
async def test_duplicate_companion_cannot_reconcile_journal_or_open_http(runtime):
    runtime.flags["duplicate"] = True
    with pytest.raises(RuntimeError, match="Existing companion"):
        await server.serve(runtime.paths, stop_event=asyncio.Event())
    assert "journal" not in runtime.calls and "listen" not in runtime.calls
    ReleaseGuard.acquire(runtime.paths.state_directory, exclusive=True, nonblocking=True).close()


@pytest.mark.asyncio
async def test_concurrent_companion_is_fenced_before_journal_even_with_different_sockets(runtime):
    stop = asyncio.Event()
    owner = asyncio.create_task(server.serve(runtime.paths, stop_event=stop))
    try:
        async with asyncio.timeout(3):
            while "listen" not in runtime.calls:
                await asyncio.sleep(0)
        other_paths = ServerPaths(
            runtime.paths.state_directory, runtime.paths.runtime_directory / "other"
        )
        with pytest.raises(WebSettingsError, match="already owns"):
            await server.serve(other_paths, stop_event=asyncio.Event())
        assert runtime.calls.count("journal") == runtime.calls.count("listen") == 1
    finally:
        stop.set()
        await owner
    again = server.CompanionLease(runtime.paths)
    again.close()


def test_companion_lock_rejects_symlink_and_unprivate_files(runtime, tmp_path):
    path = web_directory(runtime.paths) / "companion.lock"
    target = tmp_path / "target"
    target.write_text("untouched")
    path.symlink_to(target)
    with pytest.raises(OSError):
        server.CompanionLease(runtime.paths)
    assert target.read_text() == "untouched"
    path.unlink()
    path.write_bytes(b"")
    path.chmod(0o644)
    with pytest.raises(WebSettingsError, match="unsafe"):
        server.CompanionLease(runtime.paths)


@pytest.mark.asyncio
async def test_failed_cleanup_retains_source_lease_and_refuses_second_start(runtime):
    runtime.flags["cleanup_failure"] = True
    stopped = asyncio.Event()
    stopped.set()
    with pytest.raises(BaseExceptionGroup, match="retirement is incomplete"):
        await server.serve(runtime.paths, stop_event=stopped)
    assert "console-close" in runtime.calls and "health-stop" in runtime.calls
    with pytest.raises(ReleaseGuardError):
        ReleaseGuard.acquire(runtime.paths.state_directory, exclusive=True, nonblocking=True)
    with pytest.raises(RuntimeError, match="Previous web companion"):
        await server.serve(runtime.paths, stop_event=stopped)


@pytest.mark.asyncio
async def test_rejected_reader_floor_starts_no_owner_resources_and_releases_guard(
    runtime, monkeypatch
):
    def reject(_state):
        raise ReleaseGuardError("Current reader cannot read mutable state")

    monkeypatch.setattr(server, "validate_runtime_state", reject)
    with pytest.raises(ReleaseGuardError, match="cannot read"):
        await server.serve(runtime.paths, stop_event=asyncio.Event())
    assert not runtime.calls
    ReleaseGuard.acquire(runtime.paths.state_directory, exclusive=True, nonblocking=True).close()
