"""Source daemons retain canonical release admission until real cleanup completes."""

import asyncio
import os
import sys

import pytest
from test_service import FakeListener, FakeMediaSession, FakeSessions

from halo_server.configuration import ServerConfiguration, ServerMode
from halo_server.paths import ServerPaths
from halo_server.release_activation import PolicyStore
from halo_server.release_guard import (
    GUARD_FD_ENV,
    GUARD_STATE_ENV,
    ReleaseGuard,
    ReleaseGuardError,
    acquire_source_guard,
    inherited_guard_environment,
    inherited_guard_fds,
)
from halo_server.service import HaloService


def service(tmp_path):
    return HaloService(
        ServerPaths(tmp_path / "state", tmp_path / "runtime"),
        configuration=ServerConfiguration(mode=ServerMode.RING_ONLY),
        session_manager=FakeSessions(),
        listener_factory=FakeListener,
        media_session_factory=FakeMediaSession,
    )


def test_same_state_source_references_and_child_environment_do_not_mutate_process_env(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(GUARD_FD_ENV, raising=False)
    monkeypatch.delenv(GUARD_STATE_ENV, raising=False)
    state = tmp_path / "state"
    first = acquire_source_guard(state)
    second = acquire_source_guard(state)
    try:
        (descriptor,) = inherited_guard_fds(state_directory=state)
        assert inherited_guard_environment() == {
            GUARD_FD_ENV: str(descriptor),
            GUARD_STATE_ENV: str(state),
        }
        assert GUARD_FD_ENV not in os.environ and GUARD_STATE_ENV not in os.environ
        first.close()
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(state, exclusive=True, nonblocking=True)
        with pytest.raises(ReleaseGuardError, match="Another canonical"):
            acquire_source_guard(tmp_path / "other")
        assert inherited_guard_fds() == (descriptor,)
    finally:
        first.close()
        second.close()
    assert inherited_guard_fds() == ()
    ReleaseGuard.acquire(state, exclusive=True, nonblocking=True).close()


@pytest.mark.asyncio
async def test_source_service_start_and_clean_stop_own_canonical_guard(tmp_path):
    owner = service(tmp_path)
    try:
        await owner.start()
        lease = owner._source_guard_lease
        await owner.start()
        assert owner._source_guard_lease is lease
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True)
        assert inherited_guard_environment()[GUARD_STATE_ENV] == str(owner.paths.state_directory)
    finally:
        await owner.stop()
    assert owner._source_guard_lease is None and owner._shutdown_complete
    ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True).close()


@pytest.mark.asyncio
async def test_failed_shutdown_retains_source_guard_until_successful_retry(tmp_path, monkeypatch):
    owner = service(tmp_path)
    await owner.start()
    original = owner._owner_admin.close

    async def fail():
        raise RuntimeError("synthetic retained owner")

    monkeypatch.setattr(owner._owner_admin, "close", fail)
    try:
        with pytest.raises(ExceptionGroup):
            await owner.stop()
        assert owner._source_guard_lease is not None and not owner._shutdown_complete
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True)
        with pytest.raises(RuntimeError, match="Previous source release"):
            await owner.start()
    finally:
        monkeypatch.setattr(owner._owner_admin, "close", original)
        await owner.stop()
    ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True).close()


@pytest.mark.asyncio
async def test_failed_startup_retains_guard_only_when_compensation_is_incomplete(
    tmp_path, monkeypatch
):
    owner = service(tmp_path)
    original_stop = owner._admin.stop

    async def start_failure():
        raise RuntimeError("synthetic startup")

    async def cleanup_failure():
        raise RuntimeError("synthetic cleanup")

    monkeypatch.setattr(owner._admin, "start", start_failure)
    monkeypatch.setattr(owner._admin, "stop", cleanup_failure)
    try:
        with pytest.raises(RuntimeError, match="synthetic startup"):
            await owner.start()
        assert owner._source_guard_lease is not None
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True)
    finally:
        monkeypatch.setattr(owner._admin, "stop", original_stop)
        await owner.stop()
    owner = service(tmp_path / "clean")
    monkeypatch.setattr(owner._admin, "start", start_failure)
    with pytest.raises(RuntimeError):
        await owner.start()
    assert owner._source_guard_lease is None
    ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True).close()


@pytest.mark.asyncio
async def test_source_reader_floor_is_checked_under_guard_before_admin_start(tmp_path):
    owner = service(tmp_path)
    PolicyStore(owner.paths.state_directory).advance(15)
    with pytest.raises(ReleaseGuardError, match="cannot read"):
        await owner.start()
    assert owner._source_guard_lease is None and not owner.paths.admin_socket.exists()
    ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True).close()


@pytest.mark.asyncio
async def test_source_parent_stop_cannot_release_actual_inherited_child_claim(tmp_path):
    owner = service(tmp_path)
    await owner.start()
    (descriptor,) = inherited_guard_fds()
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        'import sys;print("ready",flush=True);sys.stdin.buffer.read()',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        pass_fds=(descriptor,),
        env=inherited_guard_environment(),
    )
    await child.stdout.readline()
    try:
        await owner.stop()
        assert inherited_guard_fds() == ()
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True)
    finally:
        child.stdin.close()
        await child.wait()
        await owner.stop()
    ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True).close()


@pytest.mark.asyncio
async def test_cancelled_stop_keeps_source_lifetime_claim_until_joined_retry(tmp_path, monkeypatch):
    owner = service(tmp_path)
    await owner.start()
    entered, release = asyncio.Event(), asyncio.Event()
    original = owner._owner_admin.close

    async def paused():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(owner._owner_admin, "close", paused)
    stopping = asyncio.create_task(owner.stop())
    try:
        await entered.wait()
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert owner._source_guard_lease is not None
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(owner.paths.state_directory, exclusive=True, nonblocking=True)
    finally:
        release.set()
        monkeypatch.setattr(owner._owner_admin, "close", original)
        await owner.stop()
    assert owner._source_guard_lease is None
