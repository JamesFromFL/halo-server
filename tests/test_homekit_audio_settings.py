"""Durable audio preferences fence the camera before any disk await."""

import asyncio
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_homekit_broker import request
from test_homekit_registry import OTHER_PROFILE, PROFILE, inventory
from test_homekit_selection import selected_bridge

from halo_server.homekit.ipc import validate_reply
from halo_server.homekit.models import CameraCapabilities, CameraPolicy, HomeKitOperationError
from halo_server.homekit.registry import RegistryStore
from halo_server.homekit.settings import (
    AudioPreferences,
    AudioSettings,
    AudioSettingsConflict,
    AudioSettingsError,
    AudioSettingsStore,
)


def source(*, profile=PROFILE, microphone=True, speaker=True, complete=True):
    original = inventory(profile=profile, complete=complete)
    caps = CameraCapabilities(
        video=microphone,
        talkback=speaker,
        evidence=tuple(
            name
            for name, enabled in (("sdk:video", microphone), ("halo:talkback", speaker))
            if enabled
        ),
    )
    return replace(original, cameras=(replace(original.cameras[0], capabilities=caps),))


def enrolled(tmp_path):
    current = source()
    registry = RegistryStore(tmp_path / "registry.json").reconcile(current)
    store = AudioSettingsStore(tmp_path / "audio.json")
    return store, store.reconcile(registry, current), registry


def save(store, current, preferences):
    row = current.entries[0]
    return store.set_preferences(
        row.profile_id,
        row.camera_id,
        row.accessory_id,
        preferences,
        expected_revision=current.revision,
    )


def test_audio_graph_and_saved_mutes_survive_rename_outage_and_capability_drift(tmp_path):
    store, initial, registry = enrolled(tmp_path)
    prefs = AudioPreferences(True, True, 37, 12)
    muted = save(store, initial, prefs)
    assert muted.revision == initial.revision + 1
    previous = store.path.read_bytes()
    for observed in (
        source(microphone=False, speaker=False),
        source(complete=False),
        inventory(()),
        replace(source(), cameras=()),
    ):
        assert AudioSettingsStore(store.path).reconcile(registry, observed) == muted
        assert store.path.read_bytes() == previous
    assert muted.entries[0].microphone and muted.entries[0].speaker
    assert muted.entries[0].preferences == prefs
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert save(store, muted, prefs) == muted
    assert store.path.read_bytes() == previous


def test_new_profile_same_camera_never_inherits_audio_identity_or_mute(tmp_path):
    store, initial, registry = enrolled(tmp_path)
    muted = save(store, initial, AudioPreferences(True, True))
    other = source(profile=OTHER_PROFILE)
    registry_store = RegistryStore(tmp_path / "registry.json")
    registry = registry_store.reconcile(other)
    result = store.reconcile(registry, other)
    assert result.camera(PROFILE, "camera-1") == muted.entries[0]
    new = result.camera(OTHER_PROFILE, "camera-1")
    assert new.accessory_id != muted.entries[0].accessory_id
    assert new.preferences == AudioPreferences()


def test_partial_discovery_never_allocates_audio_graph(tmp_path):
    store, _, registry = enrolled(tmp_path)
    empty = AudioSettingsStore(tmp_path / "empty.json")
    assert empty.reconcile(registry, source(complete=False)) == AudioSettings()
    assert not empty.path.exists()
    assert store.load().revision == 1


def test_lost_enrolled_preferences_cannot_recreate_default_unmuted_graph(tmp_path):
    store, initial, registry = enrolled(tmp_path)
    save(store, initial, AudioPreferences(True, True))
    store.path.unlink()
    restarted = AudioSettingsStore(store.path)
    with pytest.raises(AudioSettingsError, match="missing"):
        restarted.reconcile(registry, source())
    assert not store.path.exists()


def test_initial_marker_failure_can_finish_from_durable_preferences(tmp_path, monkeypatch):
    observed = source()
    registry = RegistryStore(tmp_path / "registry.json").reconcile(observed)
    store = AudioSettingsStore(tmp_path / "audio.json")
    real = store._enrollment.update

    def failed(*args, **kwargs):
        raise AudioSettingsError("Injected marker failure")

    monkeypatch.setattr(store._enrollment, "update", failed)
    with pytest.raises(AudioSettingsError):
        store.reconcile(registry, observed)
    assert store.path.is_file()
    monkeypatch.setattr(store._enrollment, "update", real)
    result = store.reconcile(registry, observed)
    assert result.revision == 1
    assert store._enrollment.load().initialized


def test_parallel_audio_writes_have_one_durable_cas_winner(tmp_path):
    store, initial, _ = enrolled(tmp_path)
    barrier = threading.Barrier(2)

    def update(volume):
        barrier.wait(2)
        try:
            return save(
                AudioSettingsStore(store.path), initial, AudioPreferences(microphone_volume=volume)
            )
        except AudioSettingsConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, (11, 22)))
    assert sum(item is not None for item in outcomes) == 1
    assert store.load() == next(item for item in outcomes if item is not None)


@pytest.mark.parametrize("mutation", ["missing", "null", "volume", "mute", "graph", "identity"])
def test_corrupt_preferences_fail_closed_without_default_unmute(tmp_path, mutation):
    store, initial, _ = enrolled(tmp_path)
    body = initial.as_dict()
    row = body["entries"][0]
    if mutation == "missing":
        del row["preferences"]["microphone_muted"]
    elif mutation == "null":
        row["preferences"] = None
    elif mutation == "volume":
        row["preferences"]["speaker_volume"] = True
    elif mutation == "mute":
        row["preferences"]["speaker_muted"] = 0
    elif mutation == "graph":
        row["microphone"] = False
    else:
        row["accessory_id"] = "invalid"
    store.path.write_text(json.dumps(body))
    with pytest.raises(AudioSettingsError):
        store.load()


@pytest.mark.parametrize("change", ["remove", "identity", "microphone", "speaker"])
def test_audio_birth_cannot_change_implicitly(tmp_path, change):
    store, initial, _ = enrolled(tmp_path)
    row = initial.entries[0]
    changes = {
        "remove": (),
        "identity": (replace(row, accessory_id=str(uuid4())),),
        "microphone": (replace(row, microphone=False, speaker=False),),
        "speaker": (replace(row, speaker=False),),
    }
    with pytest.raises(AudioSettingsError):
        store.save(replace(initial, entries=changes[change]))
    assert store.load() == initial


@pytest.mark.asyncio
async def test_cancelled_mute_fences_only_target_before_disk_and_joins_commit(
    tmp_path, monkeypatch
):
    bridge, boot, scope, service, client, _ = await selected_bridge(tmp_path, count=2)
    other = service.homekit.inventory().cameras[1].scope
    before = bridge._audio_settings
    entered, release = threading.Event(), threading.Event()
    real = bridge._audio_store.set_preferences

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return real(*args, **kwargs)

    monkeypatch.setattr(bridge._audio_store, "set_preferences", blocked)
    task = asyncio.create_task(
        bridge.set_audio_settings(
            boot, scope, before.revision, AudioPreferences(microphone_muted=True)
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(HomeKitOperationError):
            service.homekit.authorize(boot, scope, "streaming")
        service.homekit.validate_selected_scope(boot, other)
        assert bridge._audio_store.load() == before
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert bridge._audio_settings == bridge._audio_store.load()
        assert bridge._audio_settings.camera(
            scope.profile_id, scope.camera_id
        ).preferences.microphone_muted
        assert bridge._updates.is_set() and client.submissions == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_uncertain_audio_save_reloads_durable_truth_and_keeps_fence(tmp_path, monkeypatch):
    bridge, boot, scope, service, _, _ = await selected_bridge(tmp_path)
    before = bridge._audio_settings
    real = bridge._audio_store.set_preferences

    def saved_then_failed(*args, **kwargs):
        real(*args, **kwargs)
        raise AudioSettingsError("Injected fsync failure", possibly_saved=True)

    monkeypatch.setattr(bridge._audio_store, "set_preferences", saved_then_failed)
    try:
        with pytest.raises(AudioSettingsError) as raised:
            await bridge.set_audio_settings(
                boot, scope, before.revision, AudioPreferences(speaker_muted=True)
            )
        assert raised.value.possibly_saved
        assert bridge._audio_settings == bridge._audio_store.load()
        assert bridge._audio_settings.revision == before.revision + 1
        assert bridge._updates.is_set()
        with pytest.raises(HomeKitOperationError):
            service.homekit.validate_selected_scope(boot, scope)
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_settings_rpc_scope_and_revision_are_correlated_without_config_ack_deadlock(tmp_path):
    bridge, boot, scope, service, _, _ = await selected_bridge(tmp_path)
    before = bridge._audio_settings
    envelope = request(
        boot,
        "audio_settings_update",
        {
            "scope": asdict(scope),
            "expected_revision": before.revision,
            "settings": asdict(AudioPreferences(microphone_muted=True)),
        },
    )
    try:
        result = await asyncio.wait_for(bridge._request(envelope), 2)
        validate_reply(envelope, envelope | {"kind": "audio_settings_result", "payload": result})
        assert result["accepted"] and result["revision"] == before.revision + 1
        assert bridge._updates.is_set()
        fresh_scope = service.homekit.inventory().cameras[0].scope
        conflict = envelope | {"payload": envelope["payload"] | {"scope": asdict(fresh_scope)}}
        result = await bridge._request(conflict)
        assert not result["accepted"] and result["code"] == "conflict"
        policy = CameraPolicy(synchronized=True, streaming=True, live_audio=True)
        denied = await bridge._broker.request(
            request(
                boot,
                "policy_update",
                {
                    "scope": asdict(fresh_scope),
                    "policy": asdict(policy),
                },
            )
        )
        assert not denied["accepted"] and denied["code"] == "policy-denied"
    finally:
        await bridge._broker.retired(boot)
