import asyncio
import json
import os
import stat
import sys
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from halo_server import configuration as module
from halo_server.configuration import (
    MAX_CONFIGURATION_BYTES,
    MAX_REVISION,
    ConfigurationConflictError,
    ConfigurationError,
    ConfigurationStore,
    ServerConfiguration,
    ServerMode,
)


def _write(path, value):
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    path.chmod(0o600)


def test_missing_configuration_preserves_compatibility_without_rewriting_file(tmp_path):
    path = tmp_path / "private" / "configuration.json"
    loaded = ConfigurationStore(path).load()
    assert loaded == ServerConfiguration()
    assert loaded.revision == 0
    assert loaded.compatibility_enabled and not loaded.homekit_enabled
    assert not path.exists()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(FrozenInstanceError):
        loaded.mode = ServerMode.HOMEKIT


@pytest.mark.parametrize(
    ("mode", "compatibility", "homekit"),
    [
        ("compatibility", True, False),
        ("homekit", False, True),
        ("dual", True, True),
        ("ring-only", False, False),
    ],
)
def test_modes_are_validated_immutable_enum_snapshots(mode, compatibility, homekit):
    config = ServerConfiguration(mode=mode)
    assert isinstance(config.mode, ServerMode)
    assert config.compatibility_enabled is compatibility
    assert config.homekit_enabled is homekit


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"revision": True},
        {"revision": -1},
        {"revision": MAX_REVISION + 1},
        {"mode": "direct"},
        {"mode": None},
        {"plugin_port": True},
        {"plugin_port": -1},
        {"rtsp_port": 65536},
        {"rtsp_port": 1.0},
        {"plugin_host": "0.0.0.0"},
        {"rtsp_host": "::"},
        {"plugin_host": "remote.example"},
        {"plugin_host": "::1%lo"},
        {"plugin_host": "localhost\n"},
    ],
)
def test_invalid_configuration_cannot_be_constructed(changes):
    with pytest.raises(ConfigurationError):
        ServerConfiguration(**changes)


def test_private_atomic_roundtrip_and_stale_compare_and_swap(tmp_path):
    path = tmp_path / "configuration.json"
    first, second = ConfigurationStore(path), ConfigurationStore(path)
    initial = first.load()
    stale = second.load()
    saved = first.save(replace(initial, mode=ServerMode.HOMEKIT, plugin_port=0, rtsp_host="::1"))
    assert saved.revision == 1
    assert second.load() == saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_name(".configuration.json.lock").stat().st_mode) == 0o600
    with pytest.raises(ConfigurationConflictError):
        second.save(replace(stale, mode=ServerMode.DUAL))
    assert first.load() == saved
    explicit = second.save(replace(initial, mode=ServerMode.RING_ONLY), expected_revision=1)
    assert explicit.revision == 2
    assert first.load() == explicit
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON secret-text",
        b"\xff",
        b"[]",
        b'{"version":1,"version":1}',
        b'{"version":NaN}',
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_corrupt_configuration_is_visible_and_never_overwritten(tmp_path, body):
    path = tmp_path / "configuration.json"
    _write(path, body)
    store = ConfigurationStore(path)
    with pytest.raises(ConfigurationError):
        store.load()
    with pytest.raises(ConfigurationError):
        store.save(ServerConfiguration())
    assert path.read_bytes() == body


def test_unknown_fields_and_version_fail_without_discarding_saved_content(tmp_path):
    path = tmp_path / "configuration.json"
    value = ServerConfiguration().as_dict()
    value["future-private-field"] = "preserve"
    _write(path, value)
    with pytest.raises(ConfigurationError, match="format"):
        ConfigurationStore(path).load()
    assert json.loads(path.read_text()) == value
    value.pop("future-private-field")
    value["version"] = 2
    _write(path, value)
    with pytest.raises(ConfigurationError, match="version"):
        ConfigurationStore(path).load()


def test_oversized_configuration_rejected_before_body_is_opened(tmp_path, monkeypatch):
    path = tmp_path / "configuration.json"
    _write(path, b"x" * (MAX_CONFIGURATION_BYTES + 1))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("oversized body was read")

    monkeypatch.setattr(module.os, "fdopen", forbidden)
    with pytest.raises(ConfigurationError, match="size limit"):
        ConfigurationStore(path).load()


def test_read_is_bounded_even_if_file_grows_after_size_check(tmp_path, monkeypatch):
    path = tmp_path / "configuration.json"
    _write(path, b"x" * (MAX_CONFIGURATION_BYTES + 100))
    inode = path.stat().st_ino
    real_fstat = os.fstat

    def changed_size(descriptor):
        value = real_fstat(descriptor)
        if value.st_ino == inode:
            return SimpleNamespace(
                st_mode=value.st_mode, st_uid=value.st_uid, st_nlink=value.st_nlink, st_size=0
            )
        return value

    monkeypatch.setattr(module.os, "fstat", changed_size)
    with pytest.raises(ConfigurationError, match="size limit"):
        ConfigurationStore(path).load()


@pytest.mark.parametrize("kind", ["file-symlink", "parent-symlink", "hardlink", "fifo"])
def test_unsafe_file_and_parent_paths_are_rejected_without_touching_targets(tmp_path, kind):
    target = tmp_path / "target.json"
    _write(target, ServerConfiguration().as_dict())
    before = target.read_bytes()
    path = tmp_path / "configuration.json"
    if kind == "file-symlink":
        path.symlink_to(target)
    elif kind == "parent-symlink":
        folder = tmp_path / "real"
        folder.mkdir(mode=0o700)
        alias = tmp_path / "alias"
        alias.symlink_to(folder, target_is_directory=True)
        path = alias / "nested" / "configuration.json"
    elif kind == "hardlink":
        path.hardlink_to(target)
    else:
        os.mkfifo(path, mode=0o600)
    with pytest.raises(ConfigurationError):
        ConfigurationStore(path).load()
    assert target.read_bytes() == before
    if kind == "parent-symlink":
        assert not (tmp_path / "real" / "nested").exists()


def test_private_file_directory_and_lock_permissions_are_not_silently_repaired(tmp_path):
    path = tmp_path / "configuration.json"
    _write(path, ServerConfiguration().as_dict())
    path.chmod(0o640)
    with pytest.raises(ConfigurationError, match="0600"):
        ConfigurationStore(path).load()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    path.chmod(0o600)
    lock = tmp_path / ".configuration.json.lock"
    lock.chmod(0o644)
    with pytest.raises(ConfigurationError, match="0600"):
        ConfigurationStore(path).load()
    lock.unlink()
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(ConfigurationError, match="unsafe"):
        ConfigurationStore(public / "configuration.json").load()
    assert stat.S_IMODE(public.stat().st_mode) == 0o755


def test_lock_symlink_is_not_followed_or_chmodded(tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    target.chmod(0o644)
    (tmp_path / ".configuration.json.lock").symlink_to(target)
    with pytest.raises(ConfigurationError):
        ConfigurationStore(tmp_path / "configuration.json").save(ServerConfiguration())
    assert target.read_text() == "unchanged"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_configuration_descriptor_must_belong_to_current_user(tmp_path, monkeypatch):
    path = tmp_path / "configuration.json"
    _write(path, ServerConfiguration().as_dict())
    inode, real_fstat = path.stat().st_ino, os.fstat

    def other_owner(descriptor):
        value = real_fstat(descriptor)
        if value.st_ino == inode:
            return SimpleNamespace(
                st_mode=value.st_mode,
                st_uid=value.st_uid + 1,
                st_nlink=value.st_nlink,
                st_size=value.st_size,
            )
        return value

    monkeypatch.setattr(module.os, "fstat", other_owner)
    with pytest.raises(ConfigurationError, match="current user"):
        ConfigurationStore(path).load()


def test_save_fsyncs_file_before_atomic_replace_and_directory(tmp_path, monkeypatch):
    calls = []
    real_sync, real_replace = os.fsync, os.replace

    def sync(descriptor):
        calls.append(
            "directory-sync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file-sync"
        )
        real_sync(descriptor)

    def replace_file(*args, **kwargs):
        calls.append("replace")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(module.os, "fsync", sync)
    monkeypatch.setattr(module.os, "replace", replace_file)
    ConfigurationStore(tmp_path / "configuration.json").save(ServerConfiguration())
    assert calls == ["file-sync", "replace", "directory-sync"]


def test_new_files_have_exact_private_mode_even_when_umask_removes_owner_bits(tmp_path):
    store = ConfigurationStore(tmp_path / "configuration.json")
    old_mask = os.umask(0o777)
    try:
        saved = store.save(ServerConfiguration())
    finally:
        os.umask(old_mask)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.load() == saved


def test_failed_replace_preserves_previous_revision_and_removes_temporary_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "configuration.json"
    store = ConfigurationStore(path)
    old = store.save(ServerConfiguration())

    def fail(*_args, **_kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(ConfigurationError) as raised:
        store.save(replace(old, mode=ServerMode.HOMEKIT))
    assert not raised.value.possibly_saved
    assert store.load() == old
    assert not list(tmp_path.glob("*.tmp"))


def test_directory_sync_failure_reports_possible_save_and_requires_reload(tmp_path, monkeypatch):
    path = tmp_path / "configuration.json"
    store = ConfigurationStore(path)
    real_sync = os.fsync

    def sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected directory sync failure")
        real_sync(descriptor)

    monkeypatch.setattr(module.os, "fsync", sync)
    with pytest.raises(ConfigurationError) as raised:
        store.save(ServerConfiguration(mode=ServerMode.HOMEKIT))
    assert raised.value.possibly_saved
    assert "reload" in str(raised.value)
    assert store.load().revision == 1
    assert store.load().mode is ServerMode.HOMEKIT
    with pytest.raises(ConfigurationConflictError):
        store.save(ServerConfiguration())


@pytest.mark.asyncio
async def test_exclusive_flock_serializes_another_process(tmp_path):
    path = tmp_path / "configuration.json"
    store = ConfigurationStore(path)
    script = (
        "from halo_server.configuration import ConfigurationStore; import sys; "
        "print('started',flush=True); ConfigurationStore(sys.argv[1]).load(); "
        "print('loaded',flush=True)"
    )
    process = None
    try:
        with store._transaction():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert await asyncio.wait_for(process.stdout.readline(), 3) == b"started\n"
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(process.stdout.readline(), 0.05)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0, stderr.decode()
        assert stdout == b"loaded\n"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
