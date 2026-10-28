import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from halo_server import private_json
from halo_server.configuration import ConfigurationStore, ServerConfiguration
from halo_server.homekit import registry as module
from halo_server.homekit.models import CameraCapabilities, MediaScope, NativeCamera, NativeInventory
from halo_server.homekit.registry import (
    MAX_REGISTRY_BYTES,
    CameraRegistry,
    RegisteredCamera,
    RegistryConflictError,
    RegistryError,
    RegistryStore,
)

PROFILE = "11111111-1111-4111-8111-111111111111"
OTHER_PROFILE = "22222222-2222-4222-8222-222222222222"


def inventory(
    camera_ids=("camera-1",),
    *,
    profile=PROFILE,
    generation=1,
    device_generation=1,
    complete=True,
    name="Camera",
    doorbell=False,
):
    cameras = tuple(
        NativeCamera(
            MediaScope(profile, camera_id, generation, device_generation, 1),
            name,
            "camera",
            "observed-kind",
            "observed-model",
            CameraCapabilities(doorbell=doorbell, evidence=("sdk:doorbell",) if doorbell else ()),
        )
        for camera_id in camera_ids
    )
    return NativeInventory(1, cameras, complete, profile, generation)


def stored(tmp_path, source=None):
    store = RegistryStore(tmp_path / "registry.json")
    return store, store.reconcile(source or inventory())


def write(path, payload):
    path.write_bytes(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    path.chmod(0o600)


def test_first_discovery_is_private_durable_and_never_selects_a_camera(tmp_path):
    path = tmp_path / "private" / "registry.json"
    store = RegistryStore(path)
    empty = store.load()
    assert empty == CameraRegistry() and empty.installation_id is None
    assert not path.exists()
    saved = store.reconcile(inventory(doorbell=True))
    assert saved.revision == 1 and saved.installation_id is not None
    camera = saved.entries[0]
    assert camera.doorbell and not camera.selected
    assert UUID(camera.accessory_uuid).version == 4
    assert int(camera.username[:2], 16) & 3 == 2
    assert saved.resolve(inventory()) == ()
    assert RegistryStore(path).load() == saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((path.parent / ".registry.json.lock").stat().st_mode) == 0o600
    with pytest.raises(FrozenInstanceError):
        camera.username = "02:00:00:00:00:00"
    payload = json.loads(path.read_text())
    assert set(payload["entries"][0]) == {
        "profile_id",
        "camera_id",
        "accessory_uuid",
        "username",
        "doorbell",
        "selected",
    }


def test_rename_order_outage_restart_and_reappearance_preserve_selected_identity(tmp_path):
    first_inventory = inventory(("one", "two"), name="Original", doorbell=True)
    store, first = stored(tmp_path, first_inventory)
    first = store.set_selected(PROFILE, ("one",), expected_revision=first.revision)
    identity = next(item for item in first.entries if item.camera_id == "one")
    path, previous_bytes = store.path, store.path.read_bytes()
    for source in (
        inventory(("two", "one"), name="Renamed", generation=2, doorbell=False),
        inventory(("one",), complete=False),
        NativeInventory(0, (), False),
        inventory(()),
        inventory(("one", "two"), generation=3, device_generation=4),
    ):
        assert RegistryStore(path).reconcile(source) == first
        assert path.read_bytes() == previous_bytes
    resolved = first.resolve(inventory(("one",), generation=7, device_generation=9))
    assert len(resolved) == 1 and resolved[0].identity == identity
    assert resolved[0].identity.doorbell
    assert resolved[0].camera.scope.account_generation == 7
    assert resolved[0].camera.scope.device_generation == 9
    assert first.resolve(inventory(())) == ()
    assert first.resolve(NativeInventory(1, (), False)) == ()


def test_same_api_id_in_another_profile_never_reuses_accessory_or_selection(tmp_path):
    store, first = stored(tmp_path)
    first = store.set_selected(PROFILE, ("camera-1",), expected_revision=first.revision)
    second = store.reconcile(inventory(profile=OTHER_PROFILE), expected_revision=first.revision)
    old, new = (
        second.selected(PROFILE)[0],
        next(row for row in second.entries if row.profile_id == OTHER_PROFILE),
    )
    assert old.accessory_uuid != new.accessory_uuid and old.username != new.username
    assert not new.selected
    assert second.resolve(inventory(profile=OTHER_PROFILE)) == ()
    selected = store.set_selected(OTHER_PROFILE, ("camera-1",), expected_revision=second.revision)
    assert selected.selected(PROFILE) == (old,)
    assert selected.resolve(inventory(profile=OTHER_PROFILE))[0].identity.key == new.key


def test_partial_inventory_never_allocates_or_overwrites_an_identity(tmp_path):
    store, first = stored(tmp_path)
    before = store.path.read_bytes()
    partial = inventory(("camera-1", "unknown"), complete=False)
    assert store.reconcile(partial) == first
    assert store.path.read_bytes() == before
    source = inventory(("unknown",), complete=False)
    assert RegistryStore(tmp_path / "new.json").reconcile(source) == CameraRegistry()
    assert not (tmp_path / "new.json").exists()


def test_selection_updates_are_revision_checked_and_preserve_offline_tombstones(tmp_path):
    store, first = stored(tmp_path, inventory(("one", "two")))
    second = store.set_selected(PROFILE, ("one", "two"), expected_revision=first.revision)
    with pytest.raises(RegistryConflictError):
        store.set_selected(PROFILE, (), expected_revision=first.revision)
    with pytest.raises(RegistryConflictError):
        store.reconcile(inventory(), expected_revision=first.revision)
    assert store.reconcile(inventory(())) == second
    third = store.set_selected(PROFILE, ("two",), expected_revision=second.revision)
    assert len(third.entries) == 2
    assert [entry.camera_id for entry in third.selected(PROFILE)] == ["two"]
    assert store.set_selected(PROFILE, ("two",), expected_revision=third.revision) == third
    assert store.load() == third


@pytest.mark.parametrize(
    "selected",
    [None, "camera-1", b"camera-1", ["missing"], ["camera-1", "camera-1"], [None], ["bad\n"]],
)
def test_invalid_or_unknown_selection_never_modifies_identity_file(tmp_path, selected):
    store, initial = stored(tmp_path)
    before = store.path.read_bytes()
    with pytest.raises(RegistryError):
        store.set_selected(PROFILE, selected, expected_revision=initial.revision)
    assert store.path.read_bytes() == before


def test_selection_iterator_and_tombstone_capacity_are_bounded(tmp_path):
    store, initial = stored(tmp_path)
    consumed = []

    def too_many():
        for index in range(1000):
            consumed.append(index)
            yield str(index)

    with pytest.raises(RegistryError, match="limit"):
        store.set_selected(PROFILE, too_many(), expected_revision=initial.revision)
    assert len(consumed) == 257
    ids = tuple(str(index) for index in range(256))
    full_store, full = stored(tmp_path / "full", inventory(ids))
    full = full_store.set_selected(PROFILE, ("0",), expected_revision=full.revision)
    assert full_store.reconcile(inventory(())) == full
    before = full_store.path.read_bytes()
    with pytest.raises(RegistryError, match="capacity"):
        full_store.reconcile(inventory(("new",)))
    assert full_store.path.read_bytes() == before
    assert full_store.reconcile(inventory(("0",))) == full


def test_maximum_supported_utf8_api_ids_fit_the_bounded_store(tmp_path):
    ids = tuple(chr(0x1F300 + index) * 128 for index in range(256))
    store, saved = stored(tmp_path, inventory(ids))
    assert len(saved.entries) == 256 and store.path.stat().st_size < MAX_REGISTRY_BYTES
    assert store.load() == saved


@pytest.mark.parametrize(
    "change",
    [
        {"profile_id": "email@example.com"},
        {"camera_id": ""},
        {"camera_id": "a" * 129},
        {"camera_id": "camera\u200b"},
        {"camera_id": "\ud800"},
        {"accessory_uuid": "missing"},
        {"accessory_uuid": "00000000-0000-0000-0000-000000000000"},
        {"username": "AA:BB:CC:DD:EE:FF\n"},
        {"username": "02:aa:bb:cc:dd:ee"},
        {"username": "03:00:00:00:00:00"},
        {"username": "00:00:00:00:00:00"},
        {"doorbell": 1},
        {"selected": "yes"},
    ],
)
def test_malformed_identity_never_becomes_an_accessory(tmp_path, change):
    _, current = stored(tmp_path)
    with pytest.raises(RegistryError):
        replace(current.entries[0], **change)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"revision": True},
        {"revision": -1},
        {"revision": 2**53},
        {"installation_id": "not-a-uuid"},
        {"entries": []},
    ],
)
def test_registry_snapshot_rejects_invalid_revision_or_schema(changes):
    with pytest.raises(RegistryError):
        CameraRegistry(**changes)


@pytest.mark.parametrize("duplicate", ["key", "accessory_uuid", "username"])
def test_duplicate_durable_identity_is_rejected(tmp_path, duplicate):
    _, current = stored(tmp_path, inventory(("one", "two")))
    first, second = current.entries
    if duplicate == "key":
        second = replace(second, camera_id=first.camera_id)
    else:
        second = replace(second, **{duplicate: getattr(first, duplicate)})
    with pytest.raises(RegistryError, match="duplicate"):
        replace(current, entries=(first, second))


@pytest.mark.parametrize("replacement", ["drop", "installation", "uuid", "username", "doorbell"])
def test_saved_identity_cannot_be_implicitly_deleted_reassociated_or_regenerated(
    tmp_path, replacement
):
    store, current = stored(tmp_path)
    changed = current
    if replacement == "drop":
        changed = replace(current, entries=())
    elif replacement == "installation":
        changed = replace(current, installation_id=str(uuid4()))
    else:
        value = {
            "uuid": ("accessory_uuid", str(uuid4())),
            "username": ("username", "02:01:02:03:04:05"),
            "doorbell": ("doorbell", True),
        }[replacement]
        changed = replace(current, entries=(replace(current.entries[0], **{value[0]: value[1]}),))
    before = store.path.read_bytes()
    with pytest.raises(RegistryError, match="implicitly"):
        store.save(changed)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "body",
    [
        b"invalid secret-file-content",
        b"\xff",
        b"[]",
        b'{"version":1,"version":1}',
        b'{"version":NaN}',
        b'{"version":Infinity}',
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_corrupt_registry_never_silently_creates_new_pairing_identity(tmp_path, body):
    path = tmp_path / "registry.json"
    write(path, body)
    store = RegistryStore(path)
    for action in (
        store.load,
        lambda: store.reconcile(inventory()),
        lambda: store.save(CameraRegistry()),
    ):
        with pytest.raises(RegistryError) as raised:
            action()
        assert "secret-file-content" not in str(raised.value)
    assert path.read_bytes() == body


@pytest.mark.parametrize("change", ["unknown", "entry_unknown", "missing", "entry_not_object"])
def test_registry_strict_fields_do_not_hide_corruption(tmp_path, change):
    store, current = stored(tmp_path)
    payload = current.as_dict()
    if change == "unknown":
        payload["future"] = "preserve"
    elif change == "entry_unknown":
        payload["entries"][0]["future"] = "preserve"
    elif change == "missing":
        payload.pop("installation_id")
    else:
        payload["entries"][0] = "camera"
    write(store.path, payload)
    before = store.path.read_bytes()
    with pytest.raises(RegistryError):
        store.reconcile(inventory())
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "kind", ["file_symlink", "parent_symlink", "hardlink", "fifo", "lock_symlink"]
)
def test_registry_rejects_unsafe_paths_without_touching_their_targets(tmp_path, kind):
    target = tmp_path / "target.json"
    write(target, CameraRegistry().as_dict())
    path = tmp_path / "registry.json"
    if kind == "file_symlink":
        path.symlink_to(target)
    elif kind == "parent_symlink":
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        path = alias / "nested" / "registry.json"
    elif kind == "hardlink":
        path.hardlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, mode=0o600)
    else:
        (tmp_path / ".registry.json.lock").symlink_to(target)
    before = target.read_bytes()
    with pytest.raises(RegistryError):
        RegistryStore(path).load()
    assert target.read_bytes() == before
    if kind == "parent_symlink":
        assert not (tmp_path / "real" / "nested").exists()


@pytest.mark.parametrize("kind", ["file", "lock", "directory"])
def test_registry_does_not_silently_repair_insecure_permissions(tmp_path, kind):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    store, _ = stored(private)
    unsafe = {
        "file": store.path,
        "lock": private / ".registry.json.lock",
        "directory": private,
    }[kind]
    unsafe.chmod(0o755 if kind == "directory" else 0o640)
    before = unsafe.stat().st_mode
    with pytest.raises(RegistryError):
        store.load()
    assert unsafe.stat().st_mode == before


def test_registry_rejects_oversize_before_read_and_caps_racing_file_growth(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    write(path, b"x" * (MAX_REGISTRY_BYTES + 100))
    store = RegistryStore(path)
    inode, real_fstat = path.stat().st_ino, os.fstat
    with pytest.raises(RegistryError, match="size limit"):
        store.load()

    def changed_size(descriptor):
        value = real_fstat(descriptor)
        if value.st_ino == inode:
            return SimpleNamespace(
                st_mode=value.st_mode, st_uid=value.st_uid, st_nlink=value.st_nlink, st_size=0
            )
        return value

    monkeypatch.setattr(private_json.os, "fstat", changed_size)
    with pytest.raises(RegistryError, match="size limit"):
        store.load()


@pytest.mark.parametrize("failure", ["file_sync", "replace", "directory_sync"])
def test_registry_write_failure_preserves_old_identity_or_reports_possible_save(
    tmp_path, monkeypatch, failure
):
    store, current = stored(tmp_path)
    old_bytes = store.path.read_bytes()
    real_sync = os.fsync

    def sync(descriptor):
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if (failure == "directory_sync" and is_directory) or (
            failure == "file_sync" and not is_directory
        ):
            raise OSError("injected failure")
        real_sync(descriptor)

    def fail_replace(*_args, **_kwargs):
        raise OSError("injected failure")

    monkeypatch.setattr(private_json.os, "fsync", sync)
    if failure == "replace":
        monkeypatch.setattr(private_json.os, "replace", fail_replace)
    with pytest.raises(RegistryError) as raised:
        store.set_selected(PROFILE, ("camera-1",), expected_revision=current.revision)
    assert raised.value.possibly_saved is (failure == "directory_sync")
    if failure == "directory_sync":
        loaded = store.load()
        assert loaded.revision == current.revision + 1
        assert replace(loaded.entries[0], selected=False) == current.entries[0]
    else:
        assert store.path.read_bytes() == old_bytes
    assert not list(tmp_path.glob(".*.tmp"))


def test_concurrent_discovery_uses_one_read_modify_write_transaction(tmp_path):
    path = tmp_path / "registry.json"
    sources = [inventory((str(index),), profile=str(uuid4())) for index in range(16)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda source: RegistryStore(path).reconcile(source), sources))
    loaded = RegistryStore(path).load()
    assert loaded.revision == 16 and len(loaded.entries) == 16
    assert len({entry.accessory_uuid for entry in loaded.entries}) == 16
    assert len({entry.username for entry in loaded.entries}) == 16


def test_random_identity_collision_is_retried_and_failure_does_not_partially_enroll(
    tmp_path, monkeypatch
):
    store, current = stored(tmp_path)
    before = store.path.read_bytes()
    monkeypatch.setattr(module, "uuid4", lambda: UUID(current.entries[0].accessory_uuid))
    with pytest.raises(RegistryError, match="allocate"):
        store.reconcile(inventory(("one-more",)))
    assert store.path.read_bytes() == before


def test_resolved_camera_cannot_mix_profiles_or_api_id(tmp_path):
    _, current = stored(tmp_path)
    with pytest.raises(RegistryError, match="mismatched"):
        RegisteredCamera(current.entries[0], inventory(profile=OTHER_PROFILE).cameras[0])


def test_common_store_keeps_configuration_keyword_save_api(tmp_path):
    store = ConfigurationStore(tmp_path / "configuration.json")
    saved = store.save(configuration=ServerConfiguration())
    assert store.load() == saved


def test_concurrent_selection_intents_fail_one_stale_revision(tmp_path):
    store, current = stored(tmp_path, inventory(("one", "two")))
    barrier = Barrier(2)

    def select(camera_id):
        barrier.wait(timeout=3)
        try:
            return RegistryStore(store.path).set_selected(
                PROFILE, (camera_id,), expected_revision=current.revision
            )
        except RegistryConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(select, ("one", "two")))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert winner.revision == current.revision + 1
    assert len(store.load().selected(PROFILE)) == 1
    assert store.load() == winner


def test_username_collision_retry_and_bounded_failure_never_overwrite_identity(
    tmp_path, monkeypatch
):
    store, current = stored(tmp_path)
    used = bytes.fromhex(current.entries[0].username.replace(":", ""))
    fresh = bytes([used[0] ^ 0x04]) + used[1:]
    candidates = iter((used, fresh))
    real_bytes = module.secrets.token_bytes
    monkeypatch.setattr(
        module.secrets,
        "token_bytes",
        lambda length: next(candidates) if length == 6 else real_bytes(length),
    )
    next_registry = store.reconcile(inventory(("new",)))
    new = next(entry for entry in next_registry.entries if entry.camera_id == "new")
    assert new.username != current.entries[0].username
    before = store.path.read_bytes()
    monkeypatch.setattr(
        module.secrets, "token_bytes", lambda length: used if length == 6 else real_bytes(length)
    )
    with pytest.raises(RegistryError, match="allocate"):
        store.reconcile(inventory(("another",)))
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["file", "lock"])
def test_registry_requires_current_owner_for_snapshot_and_lock(tmp_path, monkeypatch, kind):
    store, _ = stored(tmp_path)
    target = store.path if kind == "file" else tmp_path / ".registry.json.lock"
    inode, real_fstat = target.stat().st_ino, os.fstat

    def another_owner(descriptor):
        value = real_fstat(descriptor)
        if value.st_ino == inode:
            return SimpleNamespace(
                st_mode=value.st_mode,
                st_uid=value.st_uid + 1,
                st_nlink=value.st_nlink,
                st_size=value.st_size,
            )
        return value

    monkeypatch.setattr(private_json.os, "fstat", another_owner)
    with pytest.raises(RegistryError, match="current user"):
        store.load()


def test_legacy_or_future_registry_schema_is_not_migrated_by_guessing_identity(tmp_path):
    path = tmp_path / "registry.json"
    payload = CameraRegistry().as_dict() | {"version": 2}
    write(path, payload)
    with pytest.raises(RegistryError, match="version"):
        RegistryStore(path).reconcile(inventory())
    assert json.loads(path.read_text()) == payload


def test_registry_revision_exhaustion_rejects_changes_without_wrapping(tmp_path):
    store, initial = stored(tmp_path)
    write(store.path, initial.as_dict() | {"revision": 2**53 - 1})
    with pytest.raises(RegistryError, match="revision limit"):
        store.set_selected(PROFILE, ("camera-1",), expected_revision=2**53 - 1)
    assert store.load().revision == 2**53 - 1
    assert not store.load().entries[0].selected


@pytest.mark.parametrize("expected", [True, -1, 1.0, 2**53])
def test_invalid_selection_revision_is_rejected_before_changes(tmp_path, expected):
    store, current = stored(tmp_path)
    with pytest.raises(RegistryError, match="revision"):
        store.set_selected(PROFILE, ("camera-1",), expected_revision=expected)
    assert store.load() == current
