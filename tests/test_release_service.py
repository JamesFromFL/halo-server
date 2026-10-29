"""Local-only release health and escaped single user unit, without installation."""

import copy
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from halo_server.paths import ServerPaths
from halo_server.release_activation import ActivationError, ReleaseReference
from halo_server.release_guard import ReleaseGuard
from halo_server.release_operator import _drop_operator_guard, _run, main, parser
from halo_server.release_service import local_release_healthy, render_user_unit

REFERENCE = ReleaseReference("a" * 64, "1" * 64)


def status(mode="homekit"):
    return {
        "release": REFERENCE.as_dict() | {"minimum_reader": 14},
        "service": "running",
        "shutdown_complete": False,
        "configuration": {"mode": mode},
        "account": {"authenticated": False},
        "events": {"connection": "offline"},
        "cameras": [],
        "homekit": dict.fromkeys(
            (
                "transport_ready",
                "live_video_ready",
                "live_audio_ready",
                "snapshots_ready",
                "recording_samples_ready",
            ),
            True,
        )
        | {"recording_delivery_ready": False, "reason": "recording_disabled"},
        "integrations": {"api": {"enabled": True, "port": 18443}},
        "media": {"gateway": {"enabled": True, "port": 18554}},
    }


@pytest.mark.parametrize("mode", ["homekit", "dual", "compatibility", "ring-only"])
def test_local_health_requires_exact_release_and_mode_resources_but_not_cloud(mode):
    observed = status(mode)
    assert local_release_healthy(observed, REFERENCE)
    wrong = copy.deepcopy(observed)
    wrong["release"]["release_id"] = "b" * 64
    assert not local_release_healthy(wrong, REFERENCE)
    if mode in {"homekit", "dual"}:
        wrong = copy.deepcopy(observed)
        wrong["homekit"]["transport_ready"] = False
        assert not local_release_healthy(wrong, REFERENCE)
    if mode in {"compatibility", "dual"}:
        wrong = copy.deepcopy(observed)
        wrong["media"]["gateway"]["port"] = None
        assert not local_release_healthy(wrong, REFERENCE)
    observed["service"] = "stopped"
    assert not local_release_healthy(observed, REFERENCE)


def test_unit_quotes_spaces_and_literal_environment_and_specifier_characters(tmp_path):
    prefix = tmp_path / 'prefix space $dollar %percent "quote'
    unit = render_user_unit(prefix)
    assert (
        '"'
        + str(prefix).replace('"', '\\"').replace("$", "$$").replace("%", "%%")
        + '/bin/halo-server" daemon'
        in unit
    )
    assert "KillMode=control-group" in unit and "UMask=0077" in unit
    assert "ExecStop=" not in unit and "sudo" not in unit and "npm" not in unit
    with pytest.raises(ActivationError):
        render_user_unit(Path("/tmp/line\nbreak"))


@pytest.mark.parametrize("name", ["prefix with spaces", 'prefix $dollar %percent "quote'])
def test_unit_real_offline_systemd_parser_accepts_prefix_with_spaces(tmp_path, name):
    prefix = tmp_path / name
    launcher = prefix / "bin" / "halo-server"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    unit = tmp_path / "halo-server.service"
    state = tmp_path / 'state space $dollar %percent "quote'
    unit.write_text(render_user_unit(prefix, state_directory=state))
    result = subprocess.run(
        ["/usr/bin/systemd-analyze", "--user", "--generators=no", "--man=no", "verify", str(unit)],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_operator_only_guard_drop_releases_its_own_fd_before_loop_resources(tmp_path):
    # The operator is a fresh-process entry. Other tests may already have
    # started process-lifetime native workers, just as the daemon would.
    state = tmp_path / "state"
    script = """
import os
import sys
import threading
from pathlib import Path
from halo_server.paths import ServerPaths
from halo_server.release_guard import ReleaseGuard
from halo_server.release_operator import _drop_operator_guard

state = Path(sys.argv[1])
assert threading.active_count() == 1
guard = ReleaseGuard.acquire(state, create=True)
descriptor = os.dup(guard.fileno())
os.environ.update(guard.environment() | {
    "HALO_RELEASE_GUARD_FD": str(descriptor),
    "HALO_RELEASE_OPERATOR": "1",
})
guard.close()
_drop_operator_guard(ServerPaths(state, state.parent / "runtime"))
assert "HALO_RELEASE_GUARD_FD" not in os.environ
try:
    os.fstat(descriptor)
except OSError:
    pass
else:
    raise AssertionError("Operator retained its inherited guard descriptor")
exclusive = ReleaseGuard.acquire(state, exclusive=True, nonblocking=True)
exclusive.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(state)], capture_output=True, timeout=5
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    exclusive = ReleaseGuard.acquire(state, exclusive=True, nonblocking=True)
    exclusive.close()


@pytest.mark.asyncio
async def test_operator_refuses_to_drop_guard_inside_daemon_event_loop(tmp_path, monkeypatch):
    state = tmp_path / "state"
    guard = ReleaseGuard.acquire(state, create=True)
    for key, value in guard.environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HALO_RELEASE_OPERATOR", "1")
    try:
        with pytest.raises(ActivationError) as error:
            _drop_operator_guard(ServerPaths(state, tmp_path / "runtime"))
        assert error.value.code == "unsafe_operator"
        assert os.fstat(guard.fileno())
    finally:
        guard.close()


def test_unit_command_prints_only_and_does_not_prepare_paths(tmp_path, capsys):
    prefix = tmp_path / "not installed"
    assert main(["--prefix", str(prefix), "unit"]) == 0
    assert "ExecStart=" in capsys.readouterr().out and not prefix.exists()


def test_unit_command_binds_explicit_state_without_creating_it(tmp_path, capsys):
    prefix, state = tmp_path / "prefix", tmp_path / "state with spaces"
    assert main(["--prefix", str(prefix), "--state-directory", str(state), "unit"]) == 0
    assert f'--state-directory "{state}" daemon' in capsys.readouterr().out
    assert not prefix.exists() and not state.exists()
    with pytest.raises(ActivationError):
        render_user_unit(prefix, state_directory=Path("relative"))


@pytest.mark.asyncio
async def test_operator_dispatch_is_explicit_and_uses_canonical_state(tmp_path):
    called = []

    class Operation:
        def __init__(self, prefix, state, service, *, prepare_unit=False, initial_mode=None):
            assert initial_mode == "homekit"
            called.append((prefix, state, service))

        async def activate(self, reference, *, expected_revision):
            called.append((reference, expected_revision))
            return SimpleNamespace(as_dict=lambda: {"status": "active"})

    arguments = parser().parse_args(
        [
            "--prefix",
            str(tmp_path / "prefix"),
            "activate",
            "--initial-mode",
            "homekit",
            "--release-id",
            REFERENCE.release_id,
            "--manifest-sha256",
            REFERENCE.manifest_sha256,
            "--expected-revision",
            "0",
        ]
    )
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    service = object()
    assert await _run(arguments, paths, factory=Operation, service=service) == {"status": "active"}
    assert called == [(tmp_path / "prefix", paths.state_directory, service), (REFERENCE, 0)]


def test_audited_firstinstall_operator_unit_entry_uses_only_stdlib(tmp_path):
    installer = Path(__file__).parents[1] / "tools" / "install_release.py"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(installer),
            "--prefix",
            str(tmp_path / "prefix spaces"),
            "unit",
        ],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"ExecStart=" in result.stdout and not (tmp_path / "prefix spaces").exists()


@pytest.mark.asyncio
async def test_service_command_delayed_spawn_cancel_reaps_actual_overflow_child(monkeypatch):
    import asyncio

    import halo_server.release_service as module

    original = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        process = await original(
            sys.executable,
            "-I",
            "-c",
            'import sys,time; sys.stdout.write("x"*5000); sys.stdout.flush(); time.sleep(300)',
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    operation = asyncio.create_task(module._systemctl("stop"))
    try:
        await entered.wait()
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert processes and processes[0].returncode is not None
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_service_command_timeout_reaps_actual_child(monkeypatch):
    import asyncio

    import halo_server.release_service as module

    original = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(
            sys.executable, "-I", "-c", "import time; time.sleep(300)", **kwargs
        )
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "COMMAND_SECONDS", 0.05)
    with pytest.raises(TimeoutError):
        await module._systemctl("stop")
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_explicit_unit_prepare_accepts_exact_file_refuses_overwrite_preserves_shared_modes(
    tmp_path,
):
    import stat

    from halo_server.release_service import UserServiceController

    config = tmp_path / "config"
    shared = config / "systemd" / "user"
    shared.mkdir(parents=True, mode=0o755)
    shared.chmod(0o755)
    commands = []

    async def command(*args):
        commands.append(args)

    state = tmp_path / 'state with spaces $literal %literal "quote'
    service = UserServiceController(
        object(), command=command, config_directory=config, state_directory=state
    )
    prefix = tmp_path / "prefix spaces"
    await service.prepare(prefix)
    path = shared / "halo-server.service"
    original = path.read_bytes()
    assert original == render_user_unit(prefix, state_directory=state).encode()
    assert (
        stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(shared.stat().st_mode) == 0o755
    )
    await service.prepare(prefix)
    assert path.read_bytes() == original and commands == [("daemon-reload",), ("daemon-reload",)]
    with pytest.raises(ActivationError) as error:
        await service.prepare(tmp_path / "different prefix")
    assert error.value.code == "unit_conflict" and path.read_bytes() == original
    assert commands == [("daemon-reload",), ("daemon-reload",)]


@pytest.mark.asyncio
async def test_unit_prepare_refuses_existing_symlink_without_touching_target(tmp_path):
    from halo_server.release_service import UserServiceController

    config = tmp_path / "config"
    shared = config / "systemd" / "user"
    shared.mkdir(parents=True)
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    (shared / "halo-server.service").symlink_to(target)

    async def command(*args):
        raise AssertionError("must not reload unsafe unit")

    service = UserServiceController(object(), command=command, config_directory=config)
    with pytest.raises(ActivationError):
        await service.prepare(tmp_path / "prefix")
    assert target.read_text() == "unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_service_stop_captures_before_command_and_closes_proof_on_failure(monkeypatch, fail):
    import halo_server.release_service as module

    calls = []
    client = object()

    class Owner:
        def close(self):
            calls.append("closed")

    owner = Owner()

    async def capture(current):
        assert current is client
        calls.append("captured")
        return owner

    async def verify(current, proof):
        assert current is client and proof is owner
        calls.append("verified")

    async def command(*args):
        assert args == ("stop",)
        calls.append("stopped")
        if fail:
            raise ActivationError(code="service_failed")

    monkeypatch.setattr(module, "capture_owner", capture)
    monkeypatch.setattr(module, "verify_stopped", verify)
    service = module.UserServiceController(client, command=command)
    if fail:
        with pytest.raises(ActivationError):
            await service.stop()
        assert calls == ["captured", "stopped", "closed"]
    else:
        await service.stop()
        assert calls == ["captured", "stopped", "verified", "closed"]
