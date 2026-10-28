import stat

import pytest

from halo_server.paths import PathSecurityError, ServerPaths, ensure_private_directory


def test_server_paths_use_distinct_private_state_and_runtime_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime-home"))

    paths = ServerPaths.from_environment()
    paths.prepare()

    assert paths.credentials_file == tmp_path / "state-home/halo-server/credentials.json"
    assert paths.admin_socket == tmp_path / "runtime-home/halo-server/admin.sock"
    assert stat.S_IMODE(paths.state_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.runtime_directory.stat().st_mode) == 0o700


def test_private_directory_repairs_owner_permissions(tmp_path):
    path = tmp_path / "private"
    path.mkdir(mode=0o755)
    path.chmod(0o755)

    ensure_private_directory(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_private_directory_rejects_symlink_without_chmodding_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    path = tmp_path / "private"
    path.symlink_to(target, target_is_directory=True)

    with pytest.raises(PathSecurityError, match="not a directory"):
        ensure_private_directory(path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_homekit_directories_are_opt_in_without_state_file_initialization(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    paths.prepare()
    assert paths.configuration_file == tmp_path / "state/configuration.json"
    assert not paths.homekit_directory.exists()
    assert not paths.homekit_runtime_directory.exists()

    paths.prepare_homekit()

    for path in (
        paths.homekit_directory,
        paths.homekit_pairing_directory,
        paths.homekit_runtime_directory,
    ):
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert paths.homekit_configuration_file == tmp_path / "state/homekit/config.json"
    assert paths.homekit_registry_file == tmp_path / "state/homekit/registry.json"
    assert not paths.homekit_configuration_file.exists()
    assert not paths.homekit_registry_file.exists()
    assert not list(paths.homekit_pairing_directory.iterdir())


def test_private_directory_rejects_symlink_in_ancestor_before_creating_or_chmodding(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(PathSecurityError):
        ensure_private_directory(alias / "new" / "private")
    assert not (target / "new").exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_homekit_prepare_rejects_linked_pairing_store_without_reading_it(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    paths.prepare()
    paths.homekit_directory.mkdir(mode=0o700)
    target = tmp_path / "external-pairings"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    paths.homekit_pairing_directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(PathSecurityError):
        paths.prepare_homekit()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
