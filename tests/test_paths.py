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
