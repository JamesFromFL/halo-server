"""Manifest authority, unsafe paths and pre-import state-floor enforcement."""

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from halo_server.release import (
    ENTRIES,
    ReleaseError,
    canonical,
    host_qualification,
    launch_selected,
    selected_release,
    verify_release,
)
from halo_server.release_guard import ReleaseGuard


@pytest.fixture(autouse=True)
def writable_fixture_directories(tmp_path):
    """Unseal only this test's owned directories for pytest's later removal."""
    yield
    for directory, children, _files in os.walk(tmp_path, followlinks=False):
        path = Path(directory)
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
            path.chmod(0o700)
        children[:] = [name for name in children if not (path / name).is_symlink()]


def make_release(prefix, *, reader=14, qualification=None, extra_files=None):
    files = {
        "node/bin/node": b"not executed by verification",
        "adapter/dist/src/main.js": b"// adapter fixture",
        "adapter/dist/src/pairing-reset.js": b"// reset fixture",
        "python/site-packages/local_fixture.py": b"QUALIFIED = True\n",
        "bootstrap.py": (
            b'import json,sys\nprint(json.dumps({"isolated":sys.flags.isolated,'
            b'"no_site":sys.flags.no_site,"bytecode":sys.dont_write_bytecode}))\n'
        ),
    }
    files.update(extra_files or {})
    interpreter = Path(os.path.realpath(sys.executable))
    payload = {
        "reader_version": reader,
        "launcher_version": 1,
        "qualification": qualification or host_qualification(),
        "interpreter": {
            "path": str(interpreter),
            "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        },
        "external_files": [],
        "entries": ENTRIES,
        "provenance": {"kind": "synthetic-only"},
        "files": [
            {
                "path": path,
                "size": len(raw),
                "mode": 0o555 if path == ENTRIES["node"] else 0o444,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for path, raw in sorted(files.items())
        ],
    }
    identity = hashlib.sha256(canonical(payload)).hexdigest()
    root = prefix / "releases" / identity
    root.mkdir(parents=True)
    for name, raw in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o555 if name == ENTRIES["node"] else 0o444)
    manifest = canonical({"schema": 1, "release_id": identity, "payload": payload})
    (root / "manifest.json").write_bytes(manifest)
    (root / "manifest.json").chmod(0o444)
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        directory.chmod(0o555)
    root.chmod(0o555)
    return root, hashlib.sha256(manifest).hexdigest()


def selection(prefix, root, digest, state, *, floor=14):
    state.mkdir(mode=0o700)
    (prefix / "selection.json").write_bytes(
        canonical(
            {
                "version": 1,
                "revision": 1,
                "active": {"release_id": root.name, "manifest_sha256": digest},
                "previous": None,
            }
        )
    )
    (prefix / "selection.json").chmod(0o600)
    (state / "release-policy.json").write_bytes(
        canonical({"version": 1, "revision": 1, "minimum_reader": floor})
    )
    (state / "release-policy.json").chmod(0o600)
    ReleaseGuard.acquire(state, create=True).close()


def test_complete_release_verifies_in_prefix_with_spaces_and_changed_CWD(tmp_path, monkeypatch):
    root, digest = make_release(tmp_path / "prefix with spaces")
    monkeypatch.chdir("/")
    result = verify_release(root, digest, minimum_reader=14)
    assert result.node.is_absolute() and result.entry.is_absolute()
    assert result.build_id == root.name and result.site_packages == root / "python/site-packages"


@pytest.mark.parametrize(
    "damage", ["changed", "missing", "symlink", "hardlink", "writable", "fifo", "unlisted"]
)
def test_release_rejects_damaged_or_unlisted_files_before_execution(tmp_path, damage):
    root, digest = make_release(tmp_path)
    path = root / "adapter/dist/src/main.js"
    path.parent.chmod(0o755)
    if damage == "changed":
        path.chmod(0o644)
        path.write_bytes(b"changed")
        path.chmod(0o444)
    elif damage == "missing":
        path.unlink()
    elif damage == "symlink":
        path.unlink()
        path.symlink_to("/etc/passwd")
    elif damage == "hardlink":
        os.link(path, path.with_name("extra-link"))
    elif damage == "writable":
        path.chmod(0o666)
    elif damage == "fifo":
        path.unlink()
        os.mkfifo(path, 0o400)
    else:
        path.with_name("extra.js").write_bytes(b"not in manifest")
    path.parent.chmod(0o555)
    with pytest.raises(ReleaseError):
        verify_release(root, digest)


def test_bad_platform_and_expected_manifest_digest_fail_closed(tmp_path):
    qualification = host_qualification() | {"machine": "unsupported"}
    root, digest = make_release(tmp_path, qualification=qualification)
    with pytest.raises(ReleaseError) as error:
        verify_release(root, digest)
    assert error.value.code == "host_mismatch"
    with pytest.raises(ReleaseError) as error:
        verify_release(root, "0" * 64)
    assert error.value.code == "manifest_mismatch"


def test_missing_policy_and_old_reader_never_execute_selected_bootstrap(tmp_path, monkeypatch):
    root, digest = make_release(tmp_path / "prefix", reader=13)
    state = tmp_path / "state"
    selection(tmp_path / "prefix", root, digest, state)
    called = []
    monkeypatch.setattr(os, "execve", lambda *args: called.append(args))
    with pytest.raises(ReleaseError) as error:
        launch_selected(tmp_path / "prefix", state, ["daemon"])
    assert error.value.code == "reader_too_old" and not called
    (state / "release-policy.json").unlink()
    with pytest.raises(ReleaseError):
        selected_release(tmp_path / "prefix", state)
    assert not called


def test_reader_floor_below_first_managed_generation_is_invalid(tmp_path):
    root, digest = make_release(tmp_path / "prefix")
    state = tmp_path / "state"
    selection(tmp_path / "prefix", root, digest, state, floor=13)
    with pytest.raises(ReleaseError, match="invalid_reader_floor"):
        selected_release(tmp_path / "prefix", state)


def test_selected_launcher_executes_isolated_no_site_no_bytecode_with_empty_PATH(tmp_path):
    prefix, state = tmp_path / "prefix with spaces", tmp_path / "state"
    root, digest = make_release(prefix)
    selection(prefix, root, digest, state)
    source = Path(__file__).parents[1] / "src"
    script = (
        "import sys;sys.path.insert(0,sys.argv[1]);from pathlib import Path;"
        "from halo_server.release import launch_selected;"
        'launch_selected(Path(sys.argv[2]),Path(sys.argv[3]),["--version"])'
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(source), str(prefix), str(state)],
        env={"PATH": "", "PYTHONPATH": "/untrusted", "NODE_OPTIONS": "--require /untrusted"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"isolated": 1, "no_site": 1, "bytecode": True}
    assert not list(root.rglob("__pycache__"))
    assert stat.S_IMODE((state / "release-activation.lock").stat().st_mode) == 0o600


def test_managed_context_checks_guard_and_environment_without_rehashing(tmp_path, monkeypatch):
    from halo_server import release as module

    root, digest = make_release(tmp_path / "prefix")
    state = tmp_path / "state"
    selection(tmp_path / "prefix", root, digest, state)
    verified = verify_release(root, digest, minimum_reader=14)
    guard = ReleaseGuard.acquire(state)
    try:
        monkeypatch.setattr(module, "_CONTEXT", None)
        for key, value in guard.environment().items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("HALO_RELEASE_ROOT", str(root))
        monkeypatch.setenv("HALO_RELEASE_MANIFEST_SHA256", digest)
        monkeypatch.setenv("HALO_RELEASE_MINIMUM_READER", "14")
        monkeypatch.setenv("HALO_RELEASE_ID", root.name)
        module.configure_verified_release(verified)

        def no_hash(*args, **kwargs):
            raise AssertionError("worker admission rehashed complete release")

        monkeypatch.setattr(module, "verify_release", no_hash)
        assert module.managed_release() is verified
        monkeypatch.setenv("HALO_RELEASE_ID", "0" * 64)
        with pytest.raises(ReleaseError, match="changed_context"):
            module.managed_release()
    finally:
        guard.close()
