"""Managed release switches wait for actual process ownership, including orphans."""

import os
import subprocess
import sys

import pytest

from halo_server.release_guard import (
    GUARD_FD_ENV,
    GUARD_NAME,
    GUARD_STATE_ENV,
    ReleaseGuard,
    ReleaseGuardError,
    inherited_guard_environment,
    inherited_guard_fds,
)


def test_source_execution_has_no_implicit_release_gate(monkeypatch):
    monkeypatch.delenv(GUARD_FD_ENV, raising=False)
    monkeypatch.delenv(GUARD_STATE_ENV, raising=False)
    assert inherited_guard_fds() == () and inherited_guard_environment() == {}


def test_shared_gate_survives_parent_close_until_real_child_exit(tmp_path):
    state = tmp_path / "state with spaces"
    guard = ReleaseGuard.acquire(state, create=True)
    fd = guard.fileno()
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"], pass_fds=(fd,)
    )
    guard.close()
    try:
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(state, exclusive=True, nonblocking=True)
    finally:
        child.kill()
        child.wait(timeout=5)
    with ReleaseGuard.acquire(state, exclusive=True, nonblocking=True) as exclusive:
        assert exclusive.fileno() >= 3


def test_gate_checks_exact_inherited_inode_and_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    with ReleaseGuard.acquire(state, create=True) as guard:
        for key, value in guard.environment().items():
            monkeypatch.setenv(key, value)
        assert inherited_guard_fds(state_directory=state) == (guard.fileno(),)
        with pytest.raises(ReleaseGuardError):
            inherited_guard_fds(state_directory=tmp_path / "other")
        (state / GUARD_NAME).unlink()
        (state / GUARD_NAME).touch(mode=0o600)
        with pytest.raises(ReleaseGuardError):
            inherited_guard_fds()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "permissions", "content"])
def test_unsafe_gate_is_preserved_and_refused(tmp_path, kind):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = state / GUARD_NAME
    other = tmp_path / "other"
    other.touch(mode=0o600)
    if kind == "symlink":
        target.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, target)
    else:
        target.touch(mode=0o600)
        if kind == "permissions":
            target.chmod(0o644)
        else:
            target.write_text("unexpected")
    before = target.lstat()
    with pytest.raises(ReleaseGuardError):
        ReleaseGuard.acquire(state, create=True, exclusive=True, nonblocking=True)
    assert target.lstat() == before


def test_shared_and_exclusive_gate_mutual_exclusion(tmp_path):
    with (
        ReleaseGuard.acquire(tmp_path, exclusive=True, create=True),
        pytest.raises(ReleaseGuardError),
    ):
        ReleaseGuard.acquire(tmp_path, nonblocking=True)
    with (
        ReleaseGuard.acquire(tmp_path),
        ReleaseGuard.acquire(tmp_path),
        pytest.raises(ReleaseGuardError),
    ):
        ReleaseGuard.acquire(tmp_path, exclusive=True, nonblocking=True)


@pytest.mark.parametrize("value", ["-1", "0", "1", "2", "3x", "65536", "٣"])
def test_malformed_inherited_fd_fails_closed(tmp_path, monkeypatch, value):
    monkeypatch.setenv(GUARD_FD_ENV, value)
    monkeypatch.setenv(GUARD_STATE_ENV, str(tmp_path))
    with pytest.raises(ReleaseGuardError):
        inherited_guard_fds()


def test_new_gate_uses_exact_private_mode_under_restrictive_umask(tmp_path):
    previous = os.umask(0o777)
    try:
        with ReleaseGuard.acquire(tmp_path, create=True) as guard:
            assert os.fstat(guard.fileno()).st_mode & 0o777 == 0o600
    finally:
        os.umask(previous)
