"""The non-camera bridge cannot take ownership of or rewrite camera state."""

import stat

import pytest
from test_release_activation import A, B, setup

from halo_server.devices.bridge_registry import BridgeRegistryError, BridgeRegistryStore
from halo_server.devices.models import DeviceIdentity, DeviceRecord
from halo_server.homekit.storage_owner import PairingDirectoryOwner, PairingStorageError
from halo_server.paths import PathSecurityError, ServerPaths


def camera_sentinels(paths):
    paths.prepare_homekit()
    result = {}
    for index, path in enumerate(
        (
            paths.homekit_registry_file,
            paths.homekit_configuration_file,
            paths.homekit_audio_settings_file,
            paths.homekit_recording_settings_file,
            paths.homekit_pairing_maintenance_file,
            paths.homekit_pairing_directory / "AccessoryInfo.020000000001.json",
            paths.homekit_pairing_directory / "IdentifierCache.020000000001.json",
            paths.homekit_pairing_directory / "ControllerStorage.020000000001.json",
        )
    ):
        # Deliberately opaque bytes: no device operation may parse or migrate
        # these camera-owned files, even if their format is newer or damaged.
        body = f"camera-owned-{index}\n".encode() + bytes((0, 255, index))
        path.write_bytes(body)
        path.chmod(0o600)
        result[path] = (body, stat.S_IMODE(path.stat().st_mode))
    return result


def assert_preserved(sentinels):
    for path, (body, mode) in sentinels.items():
        assert path.read_bytes() == body
        assert stat.S_IMODE(path.stat().st_mode) == mode


def contact(profile):
    return DeviceRecord(
        DeviceIdentity(profile, "ring-hub", "location", "asset", "sensor"),
        1,
        1,
        "Contact",
        "hub",
        "contact-sensor",
        capabilities=("contact",),
        capability_evidence=("ring-hub-device-schema",),
        supported=True,
    )


def test_camera_setup_does_not_initialize_bridge_storage(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    paths.prepare_homekit()
    assert not paths.homekit_device_directory.exists()
    assert not paths.homekit_device_runtime_directory.exists()


def test_bridge_storage_is_separate_private_and_does_not_initialize_camera_pairings(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    paths.prepare_homekit_devices()
    assert paths.homekit_device_directory == paths.state_directory / "homekit/devices"
    assert paths.homekit_device_registry_file == paths.homekit_device_directory / "registry.json"
    assert paths.homekit_device_pairing_directory == paths.homekit_device_directory / "pairings"
    assert (
        paths.homekit_device_pairing_maintenance_file
        == paths.homekit_device_directory / "pairing-maintenance.json"
    )
    assert paths.homekit_device_runtime_directory == paths.runtime_directory / "homekit/devices"
    for directory in (
        paths.homekit_device_directory,
        paths.homekit_device_pairing_directory,
        paths.homekit_device_runtime_directory,
    ):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert not paths.homekit_device_registry_file.exists()
    assert not paths.homekit_device_pairing_maintenance_file.exists()
    assert not paths.homekit_pairing_directory.exists()
    assert not paths.homekit_registry_file.exists()
    assert not tuple(paths.homekit_device_pairing_directory.iterdir())


def test_camera_and_bridge_pairing_locks_are_independent_and_exclusive(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    sentinels = camera_sentinels(paths)
    paths.prepare_homekit_devices()
    camera = PairingDirectoryOwner.acquire(paths.homekit_pairing_directory)
    bridge = None
    try:
        bridge = PairingDirectoryOwner.acquire(paths.homekit_device_pairing_directory)
        for directory in (paths.homekit_pairing_directory, paths.homekit_device_pairing_directory):
            with pytest.raises(PairingStorageError):
                PairingDirectoryOwner.acquire(directory)
        assert (paths.homekit_directory / ".pairings.owner.lock").is_file()
        assert (paths.homekit_device_directory / ".pairings.owner.lock").is_file()
        bridge.close()
        bridge = None
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(paths.homekit_pairing_directory)
        bridge = PairingDirectoryOwner.acquire(paths.homekit_device_pairing_directory)
    finally:
        if bridge is not None:
            bridge.close()
        camera.close()
    assert_preserved(sentinels)


@pytest.mark.parametrize("linked_component", ["devices", "pairings"])
def test_bridge_preparation_rejects_aliases_without_touching_camera_files(
    tmp_path, linked_component
):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    sentinels = camera_sentinels(paths)
    target = tmp_path / "external"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    if linked_component == "devices":
        paths.homekit_device_directory.symlink_to(target, target_is_directory=True)
    else:
        paths.homekit_device_directory.mkdir(mode=0o700)
        paths.homekit_device_pairing_directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(PathSecurityError):
        paths.prepare_homekit_devices()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not tuple(target.iterdir())
    assert_preserved(sentinels)


def test_bridge_enrollment_filters_account_switch_and_corruption_never_rewrite_cameras(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    sentinels = camera_sentinels(paths)
    store = BridgeRegistryStore(paths.homekit_device_registry_file)
    first_profile = "11111111-1111-4111-8111-111111111111"
    next_profile = "22222222-2222-4222-8222-222222222222"
    first_record, next_record = contact(first_profile), contact(next_profile)
    first = store.configure(
        first_profile,
        records=(first_record,),
        expected_revision=0,
        enabled=True,
        auto_import=True,
    )
    assert len(first.entries) == 1 and first.entries[0].selected
    excluded = store.configure(
        first_profile,
        records=(first_record,),
        expected_revision=first.revision,
        enabled=True,
        auto_import=True,
        excluded_devices=(first_record.key,),
    )
    assert not excluded.entries[0].selected
    assert excluded.entries[0].accessory_uuid == first.entries[0].accessory_uuid
    other = store.configure(
        next_profile,
        records=(next_record,),
        expected_revision=excluded.revision,
        enabled=True,
        auto_import=True,
    )
    assert len({profile.accessory_uuid for profile in other.profiles}) == 2
    assert len({profile.username for profile in other.profiles}) == 2
    assert len({entry.accessory_uuid for entry in other.entries}) == 2
    retained = next(entry for entry in other.entries if entry.profile_id == first_profile)
    assert retained == excluded.entries[0]
    assert not paths.homekit_device_pairing_directory.exists()
    assert not paths.homekit_device_pairing_maintenance_file.exists()
    assert_preserved(sentinels)
    saved_body = store.path.read_bytes()
    marker_body = store.enrollment_path.read_bytes()
    store.path.unlink()
    with pytest.raises(BridgeRegistryError):
        store.load()
    with pytest.raises(BridgeRegistryError):
        store.configure(
            next_profile,
            records=(next_record,),
            expected_revision=0,
            enabled=True,
            auto_import=True,
        )
    assert not store.path.exists()
    assert store.enrollment_path.read_bytes() == marker_body
    assert_preserved(sentinels)
    # Restoring the authoritative registry bytes retains the same identities;
    # no failed enrollment may have allocated a substitute bridge.
    store.path.write_bytes(saved_body)
    store.path.chmod(0o600)
    assert store.load() == other
    store.path.write_bytes(b"{ damaged-bridge-registry")
    with pytest.raises(BridgeRegistryError):
        store.reconcile(next_profile, (next_record,))
    assert store.path.read_bytes() == b"{ damaged-bridge-registry"
    assert_preserved(sentinels)


@pytest.mark.asyncio
async def test_release_rollback_and_uninstall_preserve_nested_bridge_and_camera_state(tmp_path):
    activation, _ = setup(tmp_path)
    paths = ServerPaths(activation.state_directory, tmp_path / "runtime")
    sentinels = camera_sentinels(paths)
    paths.prepare_homekit_devices()
    for index, path in enumerate(
        (
            paths.homekit_device_registry_file,
            paths.homekit_device_pairing_maintenance_file,
            paths.homekit_device_pairing_directory / "AccessoryInfo.020000000002.json",
            paths.homekit_device_pairing_directory / "IdentifierCache.020000000002.json",
        )
    ):
        body = f"newer-bridge-state-{index}\n".encode()
        path.write_bytes(body)
        path.chmod(0o600)
        sentinels[path] = (body, 0o600)
    first = await activation.activate(A, expected_revision=0)
    second = await activation.activate(B, expected_revision=first.selection.revision)
    # Policy changes made under the newer release must survive a code rollback.
    registry = paths.homekit_device_registry_file
    registry.write_bytes(b"later-explicit-exclusions-and-identity\n")
    sentinels[registry] = (registry.read_bytes(), 0o600)
    rollback = await activation.rollback(expected_revision=second.selection.revision)
    assert rollback.selection.active == A
    assert_preserved(sentinels)
    result = await activation.uninstall(expected_revision=rollback.selection.revision)
    assert result["mutable_state_preserved"]
    assert_preserved(sentinels)
