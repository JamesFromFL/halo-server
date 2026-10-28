"""Durable recording privacy and owner-only warm demand survive restarts."""

import asyncio
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest
from test_homekit_broker import request
from test_homekit_registry import OTHER_PROFILE, inventory
from test_homekit_selection import selected_bridge
from test_native_service import Camera, service_fixture

from halo_server.api.admin import AdminProtocolError
from halo_server.homekit.ipc import validate_reply
from halo_server.homekit.models import CameraCapabilities, CameraPolicy, HomeKitOperationError
from halo_server.homekit.power import MAINS_ONLY_KINDS, mains_confirmed
from halo_server.homekit.recording_settings import (
    RecordingPreferences,
    RecordingSettings,
    RecordingSettingsConflict,
    RecordingSettingsError,
    RecordingSettingsStore,
    configuration_from_wire,
    configuration_wire,
)
from halo_server.homekit.registry import RegistryStore
from halo_server.media.h264 import VideoConfiguration
from halo_server.media.recording import RecordingAudioConfiguration, RecordingConfiguration


def configuration():
    return RecordingConfiguration(
        VideoConfiguration(1280, 720, 15, "main", "4.0", 2000), RecordingAudioConfiguration()
    )


def enrolled(tmp_path):
    observed = inventory()
    registry = RegistryStore(tmp_path / "registry.json").reconcile(observed)
    store = RecordingSettingsStore(tmp_path / "recording.json")
    return store, store.reconcile(registry, observed), registry


def save(store, current, preferences):
    row = current.entries[0]
    return store.set_preferences(
        row.profile_id,
        row.camera_id,
        row.accessory_id,
        preferences,
        expected_revision=current.revision,
    )


def test_recording_defaults_off_and_saved_configuration_and_optin_are_private_and_durable(tmp_path):
    store, first, registry = enrolled(tmp_path)
    row = first.entries[0]
    assert not row.owner_warm and row.preferences == RecordingPreferences()
    chosen = save(store, first, RecordingPreferences(True, True, configuration()))
    chosen = store.set_owner_warm(
        row.profile_id, row.camera_id, row.accessory_id, True, expected_revision=chosen.revision
    )
    assert RecordingSettingsStore(store.path).load() == chosen
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    for observed in (inventory(complete=False), inventory(()), inventory(name="Renamed")):
        assert store.reconcile(registry, observed) == chosen
    other = inventory(profile=OTHER_PROFILE)
    registry = RegistryStore(tmp_path / "registry.json").reconcile(other)
    updated = store.reconcile(registry, other)
    assert updated.camera(row.profile_id, row.camera_id) == chosen.entries[0]
    assert updated.camera(OTHER_PROFILE, row.camera_id).preferences == RecordingPreferences()
    assert not updated.camera(OTHER_PROFILE, row.camera_id).owner_warm


def test_missing_enrolled_recording_file_cannot_restore_defaults(tmp_path):
    store, saved, registry = enrolled(tmp_path)
    save(store, saved, RecordingPreferences(True, True, configuration()))
    store.path.unlink()
    with pytest.raises(RecordingSettingsError, match="missing"):
        RecordingSettingsStore(store.path).reconcile(registry, inventory())
    assert not store.path.exists()


def test_partial_enrollment_and_configuration_parser_fail_closed(tmp_path):
    registry = RegistryStore(tmp_path / "registry.json").reconcile(inventory())
    store = RecordingSettingsStore(tmp_path / "recording.json")
    assert store.reconcile(registry, inventory(complete=False)) == RecordingSettings()
    assert not store.path.exists()
    selected = configuration_wire(configuration())
    assert configuration_from_wire(selected) == configuration()
    for value in (
        selected | {"i_frame_interval_ms": 0},
        selected | {"video": selected["video"] | {"width": 1279}},
        selected | {"audio": selected["audio"] | {"channels": 2}},
        selected | {"mains_confirmed": True},
    ):
        with pytest.raises(RecordingSettingsError):
            configuration_from_wire(value)


def test_recording_cas_has_one_durable_winner_and_corrupt_state_never_grants_optin(tmp_path):
    store, first, _ = enrolled(tmp_path)
    barrier = threading.Barrier(2)

    def change(active):
        barrier.wait(2)
        try:
            return save(
                RecordingSettingsStore(store.path),
                first,
                RecordingPreferences(active, False, configuration()),
            )
        except RecordingSettingsConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, (True, False)))
    assert sum(value is not None for value in outcomes) == 1
    assert store.load() == next(value for value in outcomes if value is not None)
    body = store.load().as_dict()
    body["entries"][0]["owner_warm"] = 1
    store.path.write_text(json.dumps(body))
    with pytest.raises(RecordingSettingsError):
        store.load()


@pytest.mark.parametrize("kind", sorted(MAINS_ONLY_KINDS))
def test_only_reviewed_mains_hardware_can_qualify(kind):
    camera = inventory().cameras[0]
    camera = replace(
        camera, kind=kind, capabilities=CameraCapabilities(video=True, evidence=("sdk:video",))
    )
    assert mains_confirmed(camera)
    assert not mains_confirmed(
        replace(
            camera,
            capabilities=CameraCapabilities(
                video=True, battery=True, evidence=("sdk:video", "sdk:battery")
            ),
        )
    )


@pytest.mark.parametrize(
    "kind",
    [
        "cocoa_spotlight",
        "stickup_cam_longfin",
        "stickup_cam_v4",
        "unknown",
        "Floodlight Pro",
        "floodlight_pro_new",
    ],
)
def test_absent_battery_or_wired_label_is_never_positive_power_evidence(kind):
    camera = replace(
        inventory().cameras[0],
        kind=kind,
        model="Spotlight Cam Wired",
        capabilities=CameraCapabilities(video=True, evidence=("sdk:video",)),
    )
    assert not mains_confirmed(camera)


@pytest.mark.asyncio
async def test_configuration_only_update_preserves_scope_and_hap_cannot_set_owner_optin(tmp_path):
    bridge, boot, scope, service, client, _ = await selected_bridge(tmp_path)
    preferences = RecordingPreferences(True, True, configuration())
    try:
        initial = await bridge.set_recording_settings(
            boot, scope, bridge._recording_settings.entries[0].preferences_revision, preferences
        )
        scope = service.homekit.inventory().cameras[0].scope
        changed = replace(
            preferences, configuration=replace(configuration(), i_frame_interval_ms=1000)
        )
        envelope = request(
            boot,
            "recording_settings_update",
            {
                "scope": asdict(scope),
                "expected_revision": initial.entries[0].preferences_revision,
                "active": changed.active,
                "audio_active": changed.audio_active,
                "configuration": configuration_wire(changed.configuration),
            },
        )
        result = await bridge._request(envelope)
        validate_reply(
            envelope, envelope | {"kind": "recording_settings_result", "payload": result}
        )
        assert result["accepted"] and not bridge._recording_settings.entries[0].owner_warm
        service.homekit.validate_selected_scope(boot, scope)
        assert not bridge._recording_policy.pending_resources and client.submissions == 0
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_cancelled_recording_disable_fences_before_disk_then_joins_durable_commit(
    tmp_path, monkeypatch
):
    bridge, boot, scope, service, _, _ = await selected_bridge(tmp_path, count=2)
    first = await bridge.set_recording_settings(
        boot,
        scope,
        bridge._recording_settings.entries[0].preferences_revision,
        RecordingPreferences(True, True, configuration()),
    )
    scope = service.homekit.inventory().cameras[0].scope
    scope = await service.homekit.update_policy(
        boot, scope, CameraPolicy(synchronized=True, recording=True, recording_audio=True)
    )
    other = service.homekit.inventory().cameras[1].scope
    entered, release = threading.Event(), threading.Event()
    original = bridge._recording_store.set_hap_preferences

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge._recording_store, "set_hap_preferences", blocked)
    writing = asyncio.create_task(
        bridge.set_recording_settings(
            boot,
            scope,
            first.entries[0].preferences_revision,
            RecordingPreferences(False, False, configuration()),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(HomeKitOperationError):
            service.homekit.authorize(boot, scope, "recording")
        service.homekit.validate_selected_scope(boot, other)
        writing.cancel()
        await asyncio.sleep(0)
        assert not writing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing
        assert not bridge._recording_settings.entries[0].preferences.active
        assert bridge._recording_store.load() == bridge._recording_settings
        assert bridge._updates.is_set()
    finally:
        release.set()
        await asyncio.gather(writing, return_exceptions=True)
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_uncertain_recording_save_reloads_truth_and_lost_store_fences(tmp_path, monkeypatch):
    bridge, boot, scope, service, _, _ = await selected_bridge(tmp_path)
    original = bridge._recording_store.set_hap_preferences

    def saved_then_failed(*args, **kwargs):
        original(*args, **kwargs)
        raise RecordingSettingsError("Injected fsync error", possibly_saved=True)

    monkeypatch.setattr(bridge._recording_store, "set_hap_preferences", saved_then_failed)
    try:
        with pytest.raises(RecordingSettingsError):
            await bridge.set_recording_settings(
                boot,
                scope,
                bridge._recording_settings.entries[0].preferences_revision,
                RecordingPreferences(True, True, configuration()),
            )
        assert bridge._recording_settings == bridge._recording_store.load()
        assert bridge._recording_settings.entries[0].preferences.active
        scope = service.homekit.inventory().cameras[0].scope
        bridge._recording_store.path.unlink()
        await bridge._load_registry()
        assert not bridge._recording_available and bridge._recording_for_scope(scope) is None
        with pytest.raises(HomeKitOperationError):
            service.homekit.validate_selected_scope(boot, scope)
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_owner_admin_optin_is_persistent_power_checked_and_never_starts_capture_by_itself(
    tmp_path,
):
    service, client, _ = service_fixture(tmp_path)
    page = await service.handle_admin("homekit_list", {})
    await service.handle_admin(
        "homekit_select",
        {
            "profile_id": page["profile_id"],
            "account_generation": page["account_generation"],
            "expected_revision": page["revision"],
            "camera_ids": ["42"],
        },
    )
    page = await service.handle_admin("homekit_recording_list", {})
    assert not page["cameras"][0]["owner_warm"] and page["cameras"][0]["mains_confirmed"]
    parameters = {
        "profile_id": page["profile_id"],
        "account_generation": page["account_generation"],
        "camera_id": "42",
        "expected_revision": page["revision"],
        "enabled": True,
    }
    result = await service.handle_admin("homekit_recording_warm", parameters)
    assert result["saved"] and service._homekit_bridge._recording_store.load().entries[0].owner_warm
    assert not service._media_sessions and client.submissions == 0
    with pytest.raises(AdminProtocolError) as stale:
        await service.handle_admin("homekit_recording_warm", parameters)
    assert stale.value.code == "configuration_conflict"
    with pytest.raises(AdminProtocolError):
        await service.handle_admin("homekit_recording_warm", parameters | {"mains_confirmed": True})


@pytest.mark.asyncio
async def test_owner_admin_rejects_battery_shared_kind_without_changing_optin(tmp_path):
    camera = Camera()
    camera.kind = "cocoa_spotlight"
    service, _, _ = service_fixture(tmp_path, cameras=[camera])
    page = await service.handle_admin("homekit_list", {})
    await service.handle_admin(
        "homekit_select",
        {
            "profile_id": page["profile_id"],
            "account_generation": page["account_generation"],
            "expected_revision": page["revision"],
            "camera_ids": ["42"],
        },
    )
    page = await service.handle_admin("homekit_recording_list", {})
    with pytest.raises(AdminProtocolError) as denied:
        await service.handle_admin(
            "homekit_recording_warm",
            {
                "profile_id": page["profile_id"],
                "account_generation": page["account_generation"],
                "camera_id": "42",
                "expected_revision": page["revision"],
                "enabled": True,
            },
        )
    assert denied.value.code == "unsupported"
    assert not service._homekit_bridge._recording_store.load().entries[0].owner_warm
