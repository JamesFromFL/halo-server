"""Managed startup initializes only explicit fresh HomeKit settings under EX."""

import concurrent.futures
import os
import stat
from dataclasses import replace

import pytest
from test_release_activation import A, setup

from halo_server.configuration import ConfigurationStore, ServerConfiguration, ServerMode
from halo_server.release_activation import ActivationError
from halo_server.release_guard import ReleaseGuard, ReleaseGuardError
from halo_server.release_modes import ManagedModeStore


def test_missing_mode_requires_explicit_initialization_and_durable_private_save(tmp_path):
    store = ManagedModeStore(tmp_path / "configuration.json")
    with pytest.raises(ActivationError) as error:
        store.prepare()
    assert error.value.code == "initial_mode_required" and not store.path.exists()
    proposed = store.preflight("homekit")
    assert proposed.mode is ServerMode.HOMEKIT and proposed.revision == 0
    assert not store.path.exists()
    saved = store.prepare("homekit")
    assert saved.mode is ServerMode.HOMEKIT and saved.revision == 1
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    before = store.path.read_bytes()
    assert store.prepare("homekit") == saved and store.path.read_bytes() == before


@pytest.mark.parametrize("mode", list(ServerMode))
def test_saved_mode_and_preferences_are_never_overridden(tmp_path, mode):
    store = ManagedModeStore(tmp_path / "configuration.json")
    saved = store.save(ServerConfiguration(mode=mode, plugin_port=0, rtsp_port=0))
    before = store.path.read_bytes()
    if saved.compatibility_enabled:
        with pytest.raises(ActivationError) as error:
            store.prepare("homekit")
        assert error.value.code == "managed_mode_unsupported"
    else:
        assert store.prepare("homekit") == saved
    assert store.path.read_bytes() == before


def test_parallel_initializers_preserve_single_cas_revision(tmp_path):
    path = tmp_path / "configuration.json"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        saved = list(executor.map(lambda _: ManagedModeStore(path).prepare("homekit"), range(2)))
    assert saved[0] == saved[1] and saved[0].revision == 1


@pytest.mark.parametrize("kind", ["malformed", "symlink"])
def test_invalid_or_unsafe_existing_configuration_refuses_without_replacement(tmp_path, kind):
    store = ManagedModeStore(tmp_path / "configuration.json")
    target = tmp_path / "unchanged"
    target.write_text("not configuration")
    if kind == "malformed":
        store.path.write_bytes(target.read_bytes())
        store.path.chmod(0o600)
    else:
        store.path.symlink_to(target)
    with pytest.raises(ActivationError) as error:
        store.prepare("homekit")
    assert error.value.code == "configuration_unavailable"
    assert target.read_text() == "not configuration"
    assert (
        store.path.is_symlink()
        if kind == "symlink"
        else store.path.read_bytes() == target.read_bytes()
    )


@pytest.mark.asyncio
async def test_activation_refuses_missing_optin_before_selector_floor_or_launchers(tmp_path):
    activation, service = setup(tmp_path)
    activation.initial_mode = None
    with pytest.raises(ActivationError) as error:
        await activation.activate(A, expected_revision=0)
    assert error.value.code == "initial_mode_required" and service.events == []
    assert not activation.policy.path.exists() and not activation.selection.path.exists()
    assert not (activation.state_directory / "configuration.json").exists()


@pytest.mark.asyncio
async def test_saved_unsupported_mode_refuses_before_stopping_working_service(tmp_path):
    activation, service = setup(tmp_path)
    store = ConfigurationStore(activation.state_directory / "configuration.json")
    store.save(ServerConfiguration(mode=ServerMode.DUAL))
    before = store.path.read_bytes()
    with pytest.raises(ActivationError) as error:
        await activation.activate(A, expected_revision=0)
    assert error.value.code == "managed_mode_unsupported" and service.events == []
    assert not activation.policy.path.exists() and not activation.selection.path.exists()
    assert store.path.read_bytes() == before


@pytest.mark.asyncio
async def test_preflight_does_not_replace_configuration_changed_before_ex(tmp_path):
    activation, service = setup(tmp_path)

    async def stop():
        service.events.append("stop")
        ConfigurationStore(activation.state_directory / "configuration.json").save(
            ServerConfiguration(mode=ServerMode.COMPATIBILITY)
        )

    service.stop = stop
    with pytest.raises(ActivationError) as error:
        await activation.activate(A, expected_revision=0)
    assert error.value.code == "managed_mode_unsupported" and service.events == ["stop"]
    assert not activation.policy.path.exists() and not activation.selection.path.exists()
    assert (
        ConfigurationStore(activation.state_directory / "configuration.json").load().mode
        is ServerMode.COMPATIBILITY
    )


@pytest.mark.asyncio
async def test_mode_initialization_has_actual_ex_and_uncertain_save_never_starts(
    tmp_path, monkeypatch
):
    activation, service = setup(tmp_path)
    original_write = ConfigurationStore._write_locked
    original_fsync = os.fsync

    def write(store, directory, snapshot):
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(activation.state_directory, nonblocking=True)

        def fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("synthetic configuration directory fsync")
            original_fsync(descriptor)

        with monkeypatch.context() as context:
            context.setattr(os, "fsync", fsync)
            return original_write(store, directory, snapshot)

    monkeypatch.setattr(ConfigurationStore, "_write_locked", write)
    with pytest.raises(ActivationError) as error:
        await activation.activate(A, expected_revision=0)
    assert error.value.possibly_saved and error.value.code == "configuration_unavailable"
    assert service.events == ["stop"]
    assert not activation.policy.path.exists() and not activation.selection.path.exists()
    current = ConfigurationStore(activation.state_directory / "configuration.json").load()
    assert current.mode is ServerMode.HOMEKIT and current.revision == 1
    monkeypatch.setattr(ConfigurationStore, "_write_locked", original_write)
    assert (await activation.activate(A, expected_revision=0)).status == "active"
    assert ConfigurationStore(activation.state_directory / "configuration.json").load() == current


def test_initialization_does_not_revert_later_owner_configuration(tmp_path):
    store = ManagedModeStore(tmp_path / "configuration.json")
    store.prepare("homekit")
    current = store.load()
    saved = store.save(
        replace(current, mode=ServerMode.RING_ONLY), expected_revision=current.revision
    )
    assert store.prepare("homekit") == saved


@pytest.mark.asyncio
async def test_old_source_operator_refuses_floor_before_any_configuration_read(
    tmp_path, monkeypatch
):
    activation, service = setup(tmp_path)
    activation.policy.advance(16)
    configuration = activation.state_directory / "configuration.json"
    configuration.write_text("future configuration must never be decoded by this reader")
    configuration.chmod(0o600)
    before = configuration.read_bytes(), activation.policy.path.read_bytes()

    def forbidden(*args):
        raise AssertionError("Old operator passed the reader-floor barrier")

    monkeypatch.setattr(ManagedModeStore, "preflight", forbidden)
    activation.verify = forbidden
    with pytest.raises(ActivationError) as error:
        await activation.activate(A, expected_revision=0)
    assert error.value.code == "operator_reader_too_old" and service.events == []
    assert before == (configuration.read_bytes(), activation.policy.path.read_bytes())
    assert not activation.selection.path.exists()
