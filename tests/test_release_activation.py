"""Disposable code switches retain current privacy, pairing and deny state."""

import asyncio
import json
import os
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from halo_server.configuration import ConfigurationStore, ServerConfiguration, ServerMode
from halo_server.release_activation import (
    ActivationConflict,
    ActivationError,
    PolicyStore,
    ReleaseActivator,
    ReleaseReference,
    SelectionStore,
)
from halo_server.release_guard import ReleaseGuard, ReleaseGuardError
from halo_server.release_service import UserServiceController
from halo_server.tui.client import AdminClientError

A = ReleaseReference("a" * 64, "1" * 64)
B = ReleaseReference("b" * 64, "2" * 64)


class Service:
    def __init__(self, healthy=True):
        self.events = []
        self.health = healthy
        self.gate = None

    async def stop(self):
        self.events.append("stop")
        if self.gate:
            self.gate.close()
            self.gate = None

    async def start(self, reference):
        self.events.append(("start", reference))

    async def healthy(self, reference):
        self.events.append(("health", reference))
        return (
            self.health.get(reference.release_id, True)
            if isinstance(self.health, dict)
            else self.health
        )

    async def disable(self):
        self.events.append("disable")

    async def prepare(self, prefix):
        self.events.append(("prepare", prefix))


def setup(tmp_path, *, service=None, readers=None):
    prefix, state = tmp_path / "prefix with spaces", tmp_path / "state with spaces"
    service = service or Service()
    readers = readers or {A.release_id: 14, B.release_id: 14}

    def verify(root, digest, floor):
        assert root.parent == prefix / "releases"
        reference = A if root.name == A.release_id else B
        assert digest == reference.manifest_sha256
        reader = readers[root.name]
        if reader < floor:
            raise ActivationError(code="reader_too_old")
        return SimpleNamespace(reader_version=reader)

    return ReleaseActivator(
        prefix, state, service, verify=verify, install=lambda verified: None, initial_mode="homekit"
    ), service


@pytest.mark.asyncio
async def test_activate_rollback_and_uninstall_never_restore_mutable_snapshots(tmp_path):
    activation, service = setup(tmp_path)
    state = activation.state_directory
    state.mkdir(mode=0o700)
    sensitive = {}
    for name in (
        "credentials.json",
        "configuration.json",
        "registry.json",
        "settings.json",
        ".settings.json.enrolled",
        "pairing-maintenance.json",
        "AccessoryInfo.json",
        "write-intent-deny.json",
    ):
        path = state / name
        path.write_text(
            json.dumps(ServerConfiguration(mode=ServerMode.HOMEKIT).as_dict())
            if name == "configuration.json"
            else "latest-authoritative-" + name
        )
        path.chmod(0o600)
        sensitive[path] = path.read_bytes()
    first = await activation.activate(A, expected_revision=0)
    assert first.status == "active" and first.selection.active == A
    assert first.policy.revision >= 1 and first.policy.minimum_reader == 14
    second = await activation.activate(B, expected_revision=first.selection.revision)
    assert second.selection.active == B and second.selection.previous == A
    for path in sensitive:
        if path.name == "configuration.json":
            store = ConfigurationStore(path)
            store.save(replace(store.load(), mode=ServerMode.RING_ONLY))
        else:
            path.write_bytes(path.read_bytes() + b"-revoked-newer")
        sensitive[path] = path.read_bytes()
    rollback = await activation.rollback(expected_revision=second.selection.revision)
    assert rollback.selection.active == A and rollback.selection.previous == B
    assert all(path.read_bytes() == data for path, data in sensitive.items())
    result = await activation.uninstall(expected_revision=rollback.selection.revision)
    assert result["mutable_state_preserved"] and "disable" in service.events
    assert not activation.selection.path.exists() and activation.policy.path.exists()
    assert all(path.read_bytes() == data for path, data in sensitive.items())


@pytest.mark.asyncio
async def test_local_failure_rolls_back_only_code_compatible_with_current_floor(tmp_path):
    activation, _ = setup(tmp_path, service=Service({B.release_id: False}))
    first = await activation.activate(A, expected_revision=0)
    result = await activation.activate(B, expected_revision=first.selection.revision)
    assert result.status == "rolled_back" and result.selection.active == A
    newer, service = setup(
        tmp_path / "newer",
        service=Service({B.release_id: False}),
        readers={A.release_id: 14, B.release_id: 15},
    )
    first = await newer.activate(A, expected_revision=0)
    result = await newer.activate(B, expected_revision=first.selection.revision)
    assert result.status == "stopped" and result.code == "rollback_incompatible"
    assert newer.policy.load().minimum_reader == 15 and newer.selection.load().active == B
    assert service.events[-1] == "stop"
    with pytest.raises(ActivationError) as error:
        await newer.activate(A, expected_revision=result.selection.revision)
    assert error.value.code == "operator_reader_too_old"


def test_policy_floor_is_monotonic_and_missing_enrolled_policy_fails_closed(tmp_path):
    store = PolicyStore(tmp_path / "state")
    first = store.advance(14)
    assert store.advance(14) == first
    second = store.advance(15)
    with pytest.raises(ActivationError, match="cannot decrease"):
        store.save(replace(second, minimum_reader=14), expected_revision=second.revision)
    store.path.unlink()
    with pytest.raises(ActivationError) as error:
        PolicyStore(store.path.parent).advance(14)
    assert error.value.code == "policy_missing"


@pytest.mark.asyncio
async def test_orphan_child_lifetime_lock_blocks_selection_and_floor_mutation(tmp_path):
    activation, _ = setup(tmp_path)
    activation.policy.advance(14)
    selected = activation.selection.activate(A, expected_revision=0)
    shared = ReleaseGuard.acquire(activation.state_directory, create=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        'import sys; print("ready",flush=True); sys.stdin.buffer.read()',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        pass_fds=(shared.fileno(),),
    )
    await process.stdout.readline()
    shared.close()  # Parent disappears; the actual child still owns SH.
    before = activation.selection.path.read_bytes(), activation.policy.path.read_bytes()
    try:
        with pytest.raises(ActivationError) as error:
            await activation.activate(B, expected_revision=selected.revision)
        assert error.value.code == "busy"
        assert (
            activation.selection.path.read_bytes(),
            activation.policy.path.read_bytes(),
        ) == before
    finally:
        process.stdin.close()
        await process.wait()
    assert (await activation.activate(B, expected_revision=selected.revision)).status == "active"


@pytest.mark.asyncio
async def test_cancelled_activation_joins_floor_and_selection_transaction(tmp_path, monkeypatch):
    activation, _ = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = activation.policy.advance

    def blocked(reader):
        entered.set()
        assert release.wait(3)
        return original(reader)

    monkeypatch.setattr(activation.policy, "advance", blocked)
    task = asyncio.create_task(activation.activate(A, expected_revision=0))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not activation.selection.path.exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            activation.selection.load().active == A
            and activation.policy.load().minimum_reader == 14
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_uncertain_selection_save_reports_failure_and_reloads_actual_selection(
    tmp_path, monkeypatch
):
    activation, service = setup(tmp_path)
    original = activation.selection.activate

    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise ActivationError("Injected directory fsync error", possibly_saved=True)

    monkeypatch.setattr(activation.selection, "activate", uncertain)
    with pytest.raises(ActivationError) as error:
        await activation.activate(A, expected_revision=0)
    assert error.value.possibly_saved
    assert activation.selection.load().active == A
    assert service.events == ["stop"]


@pytest.mark.asyncio
async def test_stale_selection_cas_does_not_stop_existing_service(tmp_path):
    activation, service = setup(tmp_path)
    activation.selection.activate(A, expected_revision=0)
    with pytest.raises(ActivationConflict):
        await activation.activate(B, expected_revision=0)
    assert service.events == []


def test_selection_and_policy_reject_symlink_public_or_noncanonical_inputs(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    target = tmp_path / "outside"
    target.write_text("do-not-touch")
    (prefix / "selection.json").symlink_to(target)
    with pytest.raises(ActivationError):
        SelectionStore(prefix).activate(A, expected_revision=0)
    assert target.read_text() == "do-not-touch"
    with pytest.raises(ActivationError):
        ReleaseReference("../outside", "1" * 64)
    os.unlink(prefix / "selection.json")


@pytest.mark.asyncio
async def test_crossprefix_floor_change_after_verification_is_rechecked_under_global_gate(tmp_path):
    activation, service = setup(tmp_path)
    activation.policy.advance(14)
    original_stop = service.stop

    async def stop():
        await original_stop()
        # A separate prefix won EX between initial verification and this stop.
        activation.policy.advance(15)

    service.stop = stop
    with pytest.raises(ActivationError) as error:
        await activation.activate(A, expected_revision=0)
    assert error.value.code == "operator_reader_too_old"
    assert activation.policy.load().minimum_reader == 15
    assert not activation.selection.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [False, True, -1, 2**53])
async def test_invalid_uninstall_revision_does_not_stop_or_disable_unit(tmp_path, revision):
    activation, service = setup(tmp_path)
    with pytest.raises(ActivationConflict):
        await activation.uninstall(expected_revision=revision)
    assert service.events == []


@pytest.mark.asyncio
async def test_different_prefix_operators_cannot_stop_each_other_during_health_check(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedHealth(Service):
        async def healthy(self, reference):
            entered.set()
            await release.wait()
            return True

    first, first_service = setup(tmp_path / "first", service=PausedHealth())
    second_service = Service()
    second = ReleaseActivator(
        tmp_path / "second prefix",
        first.state_directory,
        second_service,
        verify=lambda *args: SimpleNamespace(reader_version=14),
        install=lambda verified: None,
    )
    active = asyncio.create_task(first.activate(A, expected_revision=0))
    try:
        await entered.wait()
        with pytest.raises(ActivationError) as error:
            await second.activate(B, expected_revision=0)
        assert error.value.code == "busy"
        assert second_service.events == [] and not second.selection.path.exists()
        release.set()
        assert (await active).status == "active"
        assert (await second.activate(B, expected_revision=0)).status == "active"
    finally:
        release.set()
        await asyncio.gather(active, return_exceptions=True)


@pytest.mark.asyncio
async def test_firstinstall_hooks_run_under_gate_before_policy_and_selection(tmp_path):
    activation, service = setup(tmp_path)
    activation.prepare_unit = True
    calls = []

    def install(verified):
        assert service.events == [("prepare", activation.prefix), "stop"]
        assert not activation.selection.path.exists() and not activation.policy.path.exists()
        assert (
            ConfigurationStore(activation.state_directory / "configuration.json").load().mode
            is ServerMode.HOMEKIT
        )
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(activation.state_directory, nonblocking=True)
        calls.append(True)

    activation.install = install
    first = await activation.activate(A, expected_revision=0)
    assert calls == [True] and first.status == "active"
    await activation.activate(B, expected_revision=first.selection.revision)
    assert calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["running", "bad_response", "timeout"])
async def test_legacy_stop_uncertainty_prevents_first_selection_and_floor(tmp_path, response):
    calls = []

    class Client:
        async def status(self):
            if response == "running":
                return {"service": "running"}
            if response == "timeout":
                raise TimeoutError
            raise AdminClientError("bad_response", "synthetic invalid reply")

    async def command(*args):
        calls.append(args)

    controller = UserServiceController(Client(), command=command)
    activation, _ = setup(tmp_path, service=controller)
    with pytest.raises(ActivationError) as error:
        await activation.activate(A, expected_revision=0)
    assert error.value.code == ("active_unmanaged" if response == "running" else "stop_unverified")
    assert calls == [("stop",)]
    assert not activation.selection.path.exists() and not activation.policy.path.exists()
