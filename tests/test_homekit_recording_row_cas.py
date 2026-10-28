"""Recording HAP CAS is per camera; owner opt-in stays whole-store CAS."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from test_homekit_recording_settings import configuration
from test_homekit_registry import PROFILE, inventory

from halo_server.homekit.recording_settings import (
    RecordingPreferences,
    RecordingSettingsConflict,
    RecordingSettingsError,
    RecordingSettingsStore,
)
from halo_server.homekit.registry import RegistryStore


def enroll(tmp_path):
    observed = inventory(("one", "two"))
    registry = RegistryStore(tmp_path / "registry.json").reconcile(observed)
    store = RecordingSettingsStore(tmp_path / "recording.json")
    return store, store.reconcile(registry, observed)


def put(store, row, preferences, revision=None):
    return store.set_hap_preferences(
        row.profile_id,
        row.camera_id,
        row.accessory_id,
        preferences,
        expected_preferences_revision=row.preferences_revision if revision is None else revision,
    )


def test_different_camera_concurrent_changes_preserve_both_rows(tmp_path):
    store, first = enroll(tmp_path)
    barrier = Barrier(2)
    preferences = RecordingPreferences(True, True, configuration())

    def change(row):
        barrier.wait(2)
        return put(RecordingSettingsStore(store.path), row, preferences)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(change, first.entries))
    saved = store.load()
    assert saved.revision == first.revision + 2
    assert all(
        row.preferences == preferences and row.preferences_revision == 1 for row in saved.entries
    )
    assert all(not row.owner_warm for row in saved.entries)


def test_same_camera_aba_rejects_old_intent_and_noop_does_not_advance(tmp_path):
    store, first = enroll(tmp_path)
    row = first.entries[0]
    enabled = RecordingPreferences(True, False, configuration())
    second = put(store, row, enabled)
    assert put(store, second.entries[0], enabled) == second
    third = put(store, second.entries[0], row.preferences)
    assert third.entries[0].preferences == row.preferences
    assert third.entries[0].preferences_revision == 2
    with pytest.raises(RecordingSettingsConflict):
        put(store, row, enabled)
    assert store.load() == third
    owner = store.set_owner_warm(
        PROFILE, row.camera_id, row.accessory_id, True, expected_revision=third.revision
    )
    assert owner.entries[0].preferences_revision == 2
    with pytest.raises(RecordingSettingsConflict):
        store.set_owner_warm(
            PROFILE, row.camera_id, row.accessory_id, False, expected_revision=third.revision
        )


def test_v1_migration_preserves_tokens_privacy_and_identity_atomically(tmp_path):
    store, first = enroll(tmp_path)
    value = first.as_dict()
    value["version"], value["revision"] = 1, 17
    for row in value["entries"]:
        row.pop("preferences_revision")
    store.path.write_text(json.dumps(value))
    migrated = RecordingSettingsStore(store.path).load()
    assert migrated.version == 2 and migrated.revision == 18
    assert all(row.preferences_revision == 17 for row in migrated.entries)
    assert [
        (row.key, row.accessory_id, row.owner_warm, row.preferences) for row in migrated.entries
    ] == [(row.key, row.accessory_id, row.owner_warm, row.preferences) for row in first.entries]
    assert store.load() == migrated
    assert json.loads(store.path.read_text())["version"] == 2


def test_whole_store_save_cannot_rewind_or_skip_camera_revision(tmp_path):
    store, first = enroll(tmp_path)
    row = first.entries[0]
    with pytest.raises(RecordingSettingsError, match="exactly once"):
        store.save(
            replace(
                first,
                entries=(replace(row, preferences=RecordingPreferences(True)), first.entries[1]),
            ),
            expected_revision=first.revision,
        )
    assert store.load() == first
