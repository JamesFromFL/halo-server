"""Installed stable entry points work with no source checkout or useful PATH."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_release import make_release, selection, writable_fixture_directories  # noqa: F401

from halo_server.release import ReleaseError
from halo_server.release_launcher import install_launchers


@pytest.mark.parametrize("mask", [0, 0o777])
def test_stable_installed_entry_uses_absolute_isolated_host_python(tmp_path, mask):
    prefix, state = tmp_path / "prefix with spaces", tmp_path / "state"
    root, digest = make_release(prefix)
    prefix.chmod(0o700)
    selection(prefix, root, digest, state)
    old = os.umask(mask)
    try:
        install_launchers(prefix, interpreter=Path(sys.executable))
    finally:
        os.umask(old)
    result = subprocess.run(
        [str(prefix / "bin/halo-server"), "--state-directory", str(state), "--version"],
        cwd="/",
        env={"PATH": "", "PYTHONPATH": "/untrusted", "NODE_OPTIONS": "--require /untrusted"},
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"isolated": 1, "no_site": 1, "bytecode": True}
    assert not list(root.rglob("__pycache__"))


def test_existing_different_stable_launcher_is_never_overwritten(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    install_launchers(prefix, interpreter=Path(sys.executable))
    path = prefix / "bin/halo-server"
    path.chmod(0o755)
    path.write_text("changed")
    path.chmod(0o555)
    with pytest.raises(ReleaseError):
        install_launchers(prefix, interpreter=Path(sys.executable))
    assert path.read_text() == "changed"


def test_modified_libexec_verifier_is_rejected_before_import(tmp_path):
    prefix, state = tmp_path / "prefix", tmp_path / "state"
    root, digest = make_release(prefix)
    prefix.chmod(0o700)
    selection(prefix, root, digest, state)
    install_launchers(prefix, interpreter=Path(sys.executable))
    path = prefix / "libexec/halo_release_runtime.py"
    path.chmod(0o644)
    path.write_text('raise SystemExit("unverified code ran")\n')
    path.chmod(0o444)
    result = subprocess.run(
        [str(prefix / "bin/halo-server"), "--state-directory", str(state), "--version"],
        env={"PATH": ""},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1 and not result.stdout
    assert "Changed stable launcher module" in result.stderr
    assert "unverified code ran" not in result.stderr


def test_stable_launcher_refuses_old_reader_before_selected_python_runs(tmp_path):
    prefix, state = tmp_path / "prefix", tmp_path / "state"
    root, digest = make_release(prefix, reader=13)
    prefix.chmod(0o700)
    selection(prefix, root, digest, state)
    install_launchers(prefix, interpreter=Path(sys.executable))
    result = subprocess.run(
        [str(prefix / "bin/halo-server"), "--state-directory", str(state), "--version"],
        env={"PATH": ""},
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1 and not result.stdout
    assert "reader_too_old" in result.stderr


def test_interrupted_launcher_write_never_publishes_partial_file_and_retry_succeeds(
    tmp_path, monkeypatch
):
    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    write = os.write
    calls = 0

    def interrupted(descriptor, body):
        nonlocal calls
        calls += 1
        if calls == 1:
            return write(descriptor, body[:7])
        raise OSError("injected interrupted write")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", interrupted)
        with pytest.raises(OSError, match="injected"):
            install_launchers(prefix, interpreter=Path(sys.executable))
    assert not (prefix / "libexec/halo_release_runtime.py").exists()
    assert not list(prefix.rglob("*.tmp"))
    install_launchers(prefix, interpreter=Path(sys.executable))
    assert (prefix / "bin/halo-server").is_file()
    assert not list(prefix.rglob("*.tmp"))
