"""New code refuses unsupported mutable floors and reports storage recovery truthfully."""

import json
from pathlib import Path

import pytest
from test_native_service import service_fixture

from halo_server.diagnostics import local_doctor
from halo_server.homekit.bridge import AdapterResources
from halo_server.paths import ServerPaths, ensure_private_directory
from halo_server.release_activation import ActivationError, PolicyStore
from halo_server.release_execution import validate_runtime_state
from halo_server.release_guard import ReleaseGuardError


def test_source_runtime_does_not_create_unenrolled_state(tmp_path):
    state = tmp_path / "absent"
    validate_runtime_state(state)
    assert not state.exists()


def test_new_code_refuses_future_floor_and_enrolled_floor_loss(tmp_path):
    store = PolicyStore(tmp_path)
    store.advance(14)
    validate_runtime_state(tmp_path)
    store.advance(16)
    with pytest.raises(ReleaseGuardError):
        validate_runtime_state(tmp_path)
    store.path.unlink()
    with pytest.raises(ActivationError, match="missing"):
        validate_runtime_state(tmp_path)


@pytest.mark.asyncio
async def test_service_refuses_future_floor_before_starting_owner_resources(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    PolicyStore(service.paths.state_directory).advance(16)
    with pytest.raises(ReleaseGuardError):
        await service.start()
    assert service._started_at is None and service._main_loop is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "symlink"])
async def test_doctor_reports_intent_without_reading_or_removing_it(tmp_path, kind):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    ensure_private_directory(paths.homekit_pairing_directory)
    secret = tmp_path / "secret"
    secret.write_text("private-canary-intent-content")
    marker = paths.homekit_pairing_directory / ".hap-storage-intent"
    if kind == "file":
        marker.write_text("private-canary-intent-content")
        marker.chmod(0o600)
    else:
        marker.symlink_to(secret)
    import sys

    entry = tmp_path / "main.js"
    entry.write_text("// no publication")
    result = await local_doctor(paths, resources=AdapterResources(Path(sys.executable), entry))
    assert {
        "name": "pairing-storage",
        "status": "fail",
        "code": "storage-recovery-required",
    } in result["checks"]
    assert result["status"] == "degraded"
    assert "canary" not in json.dumps(result)
    assert marker.lstat() and secret.read_text() == "private-canary-intent-content"


def test_partial_managed_environment_cannot_fall_back_to_source_worker(tmp_path, monkeypatch):
    from halo_server.release_execution import python_worker_command

    monkeypatch.setenv("HALO_RELEASE_ID", "a" * 64)
    for key in ("HALO_RELEASE_ROOT", "HALO_RELEASE_MANIFEST_SHA256", "HALO_RELEASE_MINIMUM_READER"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ReleaseGuardError, match="incomplete"):
        python_worker_command(tmp_path / "transcode_worker.py")


def test_managed_state_directory_override_is_bound_to_actual_inherited_gate(tmp_path, monkeypatch):
    from halo_server.release_guard import ReleaseGuard

    state = tmp_path / "state with spaces"
    with ReleaseGuard.acquire(state, create=True) as guard:
        for key, value in guard.environment().items():
            monkeypatch.setenv(key, value)
        assert ServerPaths.from_environment().state_directory == state
        monkeypatch.setenv("HALO_RELEASE_STATE_DIRECTORY", str(tmp_path / "other"))
        with pytest.raises(ReleaseGuardError):
            ServerPaths.from_environment()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "symlink"])
async def test_doctor_reports_device_pairing_recovery_without_reading_secret(tmp_path, kind):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    ensure_private_directory(paths.homekit_device_pairing_directory)
    marker = paths.homekit_device_pairing_directory / ".hap-storage-intent"
    secret = tmp_path / "private"
    secret.write_text("device-pairing-secret-canary")
    if kind == "file":
        marker.write_text("device-pairing-secret-canary")
    else:
        marker.symlink_to(secret)
    entry = tmp_path / "main.js"
    entry.write_text("// camera")
    (tmp_path / "device-main.js").write_text("// bridge")
    import sys

    result = await local_doctor(paths, resources=AdapterResources(Path(sys.executable), entry))
    checks = {item["name"]: item for item in result["checks"]}
    assert checks["device-pairing-storage"]["code"] == "storage-recovery-required"
    assert checks["device-adapter-resources"]["status"] == "pass"
    assert "secret-canary" not in json.dumps(result)
    assert marker.lstat() and secret.read_text() == "device-pairing-secret-canary"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "symlink", "empty"])
async def test_doctor_invalid_device_child_is_not_ready(tmp_path, kind):
    import sys

    entry = tmp_path / "main.js"
    entry.write_text("// camera")
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    if kind == "symlink":
        (tmp_path / "device-main.js").symlink_to(entry)
    elif kind == "empty":
        (tmp_path / "device-main.js").touch()
    result = await local_doctor(paths, resources=AdapterResources(Path(sys.executable), entry))
    assert {
        "name": "device-adapter-resources",
        "status": "fail",
        "code": "unavailable",
    } in result["checks"]
