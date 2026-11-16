"""Synthetic privacy receipts and durable preferences; no Ring/device calls."""

import asyncio
import concurrent.futures
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_native_service import service_fixture

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.api.plugin import PluginCameraOperationError
from halo_server.camera_privacy import (
    PRIVACY_TTL,
    CameraPrivacy,
    CameraPrivacyConflict,
    CameraPrivacyError,
    RingPrivacyReceipt,
    ring_privacy_receipt,
)
from halo_server.media.leases import LeasePurpose


def fixture(tmp_path, *, live=False, motion=True, audio=True):
    clock = [1000.0]
    device = SimpleNamespace(
        id="42",
        _attrs={
            "settings": {
                "live_view_disabled": live,
                "motion_detection_enabled": motion,
                "enable_audio_recording": audio,
            }
        },
    )
    scope, client = AccountScope(str(uuid4()), 1), object()
    manager = CameraPrivacy(tmp_path / "private" / "privacy.json", clock=lambda: clock[0])
    manager.observe(scope, client, [device], ring_privacy_receipt([device], clock[0]))
    return manager, scope, client, device, clock


@pytest.mark.parametrize(
    "disabled,motion,audio,live,recording,snapshot,audible",
    [
        (False, True, True, True, True, True, True),
        (True, True, True, False, False, False, True),
        (False, False, True, True, False, False, True),
        (None, True, True, False, False, False, True),
        (False, None, True, True, False, False, True),
        (False, True, None, True, True, True, False),
        (0, 1, "yes", False, False, False, False),
    ],
)
def test_pure_effective_settings_not_alarm_mode(
    tmp_path, disabled, motion, audio, live, recording, snapshot, audible
):
    manager, *_ = fixture(tmp_path, live=disabled, motion=motion, audio=audio)
    assert [manager.allowed("42", p) for p in ("live", "recording", "snapshot", "audio")] == [
        live,
        recording,
        snapshot,
        audible,
    ]


def test_unknown_stale_future_and_cache_hits_never_gain_freshness(tmp_path):
    manager, scope, client, device, clock = fixture(tmp_path)
    epoch = manager.epoch("42")
    receipt = ring_privacy_receipt([device], clock[0])
    clock[0] += PRIVACY_TTL + 0.001
    assert not manager.allowed("42", "live")
    assert manager.epoch("42") != epoch
    manager.observe(scope, client, [device], receipt)
    assert manager.view("42")["reason"] == "source_stale"
    manager.observe(scope, client, [device], RingPrivacyReceipt(clock[0] + 1, receipt.rows))
    assert not manager.allowed("42", "recording")
    manager.observe(scope, client, [device], None)
    assert manager.view("42")["reason"] == "source_unknown"


def test_receipt_is_immutable_and_device_identity_scoped(tmp_path):
    manager, scope, client, device, clock = fixture(tmp_path)
    receipt = ring_privacy_receipt([device], clock[0])
    device._attrs["settings"]["live_view_disabled"] = True
    manager.observe(scope, client, [device], receipt)
    assert manager.allowed("42", "live")
    manager.observe(scope, client, [SimpleNamespace(id="42")], receipt)
    assert not manager.allowed("42", "live")


def test_repeat_identical_current_receipt_preserves_epoch(tmp_path):
    manager, scope, client, device, clock = fixture(tmp_path)
    epoch = manager.epoch("42")
    clock[0] += 60
    assert not manager.observe(scope, client, [device], ring_privacy_receipt([device], clock[0]))
    assert manager.epoch("42") == epoch


def test_override_is_durable_scoped_and_only_homekit(tmp_path):
    manager, scope, client, device, clock = fixture(tmp_path, live=True, motion=False, audio=False)
    manager.set_mode(scope.profile_id, "42", "homekit", expected_revision=0)
    assert manager.view("42")["revision"] == 1
    for purpose in ("live", "talkback", "recording", "snapshot", "audio"):
        assert manager.allowed("42", purpose, homekit=True)
        assert not manager.allowed("42", purpose)
    clock[0] += PRIVACY_TTL + 10
    assert manager.allowed("42", "live", homekit=True)
    restored = CameraPrivacy(manager.store.path, clock=lambda: clock[0])
    restored.observe(scope, client, [device], None)
    assert restored.allowed("42", "live", homekit=True)
    restored.observe(AccountScope(str(uuid4()), 1), client, [device], None)
    assert not restored.allowed("42", "live", homekit=True)
    restored.invalidate()
    assert not restored.allowed("42", "live", homekit=True)


def test_per_camera_revision_and_wrong_revision_cas(tmp_path):
    manager, scope, *_ = fixture(tmp_path)
    manager.set_mode(scope.profile_id, "43", "homekit", expected_revision=0)
    manager.set_mode(scope.profile_id, "42", "homekit", expected_revision=0)
    with pytest.raises(CameraPrivacyConflict):
        manager.set_mode(scope.profile_id, "42", "ring", expected_revision=0)
    manager.set_mode(scope.profile_id, "42", "ring", expected_revision=1)
    assert manager.view("42")["revision"] == 2
    manager.set_mode(scope.profile_id, "42", "ring", expected_revision=2)
    assert manager.view("42")["revision"] == 2


def test_corrupt_preferences_do_not_retain_homekit_override(tmp_path):
    manager, scope, client, device, _ = fixture(tmp_path)
    manager.set_mode(scope.profile_id, "42", "homekit", expected_revision=0)
    manager.store.path.write_text(json.dumps({"invalid": "settings"}))
    with pytest.raises(CameraPrivacyError):
        manager.set_mode(scope.profile_id, "42", "ring", expected_revision=1)
    assert not manager.allowed("42", "live", homekit=True)
    restarted = CameraPrivacy(manager.store.path)
    restarted.observe(scope, client, [device], None)
    assert not restarted.allowed("42", "live", homekit=True)


def test_pending_mutation_blocks_media_before_write_then_reopens_only_current_authority(tmp_path):
    manager, scope, *_ = fixture(tmp_path)
    initial = manager.epoch("42")
    assert manager.begin_change("42") == {"42"}
    assert not manager.allowed("42", "live", homekit=True)
    manager.set_mode(scope.profile_id, "42", "homekit", expected_revision=0)
    assert not manager.allowed("42", "live", homekit=True)
    manager.finish_change("42")
    assert manager.allowed("42", "live", homekit=True)
    assert manager.epoch("42") != initial


def test_compatibility_cannot_export_audio_when_ring_audio_disabled(tmp_path):
    manager, *_ = fixture(tmp_path, audio=False)
    assert manager.allowed("42", LeasePurpose.HOMEKIT_LIVE)
    assert not manager.allowed("42", LeasePurpose.SCRYPTED_LIVE)
    assert not manager.allowed("42", LeasePurpose.EVENT)


@pytest.mark.asyncio
async def test_missing_receipt_denies_every_source_without_submitting(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    client._camera_privacy_receipt = None
    service._remember_camera_privacy(client)
    for purpose in (
        LeasePurpose.HOMEKIT_LIVE,
        LeasePurpose.HOMEKIT_RECORDING,
        LeasePurpose.SCRYPTED_LIVE,
    ):
        with pytest.raises(RuntimeError):
            await service._acquire_camera_consumer("42", purpose, homekit=True)
    with pytest.raises(PluginCameraOperationError):
        await service._get_camera_snapshot("42", False)
    assert not service._media_sessions and client.submissions == 0


@pytest.mark.asyncio
async def test_snapshot_reply_is_discarded_after_privacy_changes(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    client.snapshot_future = concurrent.futures.Future()
    task = asyncio.create_task(service._get_camera_snapshot("42", False, homekit=True))
    await asyncio.sleep(0)
    client.all_devices[0]._attrs["settings"]["motion_detection_enabled"] = False
    client._camera_privacy_receipt = ring_privacy_receipt(
        client.all_devices, service._camera_privacy._clock()
    )
    service._remember_camera_privacy(client)
    client.snapshot_future.set_result(b"private-image")
    with pytest.raises(PluginCameraOperationError):
        await task


@pytest.mark.asyncio
async def test_preference_change_fences_before_durable_io_and_cancellation_joins(
    tmp_path, monkeypatch
):
    service, client, sessions = service_fixture(tmp_path)
    session, _ = await service._acquire_camera_consumer(
        "42", LeasePurpose.HOMEKIT_LIVE, homekit=True
    )
    entered, release = threading.Event(), threading.Event()
    original = service._camera_privacy.store.update

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(service._camera_privacy.store, "update", delayed)
    task = asyncio.create_task(
        service.camera_privacy_set(
            profile_id=sessions.active_scope.profile_id,
            account_generation=1,
            camera_id="42",
            mode="homekit",
            expected_revision=0,
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    assert "42" not in service._media_sessions
    assert not service._camera_privacy_allowed("42", "live", homekit=True)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await service._drain_media_tasks()
    assert not session.leases
    assert service.camera_privacy_list()["cameras"][0]["mode"] == "homekit"
    assert not service._media_sessions  # A preference is never new capture demand.


@pytest.mark.asyncio
async def test_changed_account_cannot_save_camera_preference(tmp_path):
    service, _, sessions = service_fixture(tmp_path)
    with pytest.raises(AdminProtocolError) as error:
        await service.camera_privacy_set(
            profile_id=sessions.active_scope.profile_id,
            account_generation=2,
            camera_id="42",
            mode="homekit",
            expected_revision=0,
        )
    assert error.value.code == "account_changed"
    assert service.camera_privacy_list()["cameras"][0]["mode"] == "ring"


def test_expiry_and_refresh_without_sweep_never_resurrects_old_token(tmp_path):
    manager, scope, client, device, clock = fixture(tmp_path)
    original = manager.epoch("42")
    clock[0] += PRIVACY_TTL + 1
    assert manager.observe(scope, client, [device], ring_privacy_receipt([device], clock[0])) == {
        "42"
    }
    assert manager.epoch("42") != original
    assert manager.allowed("42", "live")


@pytest.mark.asyncio
async def test_known_stale_revision_and_idempotent_save_do_not_retire_viewer(tmp_path):
    service, _, sessions = service_fixture(tmp_path)
    capture, _ = await service._acquire_camera_consumer(
        "42", LeasePurpose.HOMEKIT_LIVE, homekit=True
    )
    params = dict(
        profile_id=sessions.active_scope.profile_id,
        account_generation=1,
        camera_id="42",
        mode="ring",
    )
    with pytest.raises(AdminProtocolError) as error:
        await service.camera_privacy_set(**params, expected_revision=1)
    assert error.value.code == "configuration_conflict"
    assert service._media_sessions["42"] is capture
    result = await service.camera_privacy_set(**params, expected_revision=0)
    assert result["revision"] == 0
    assert service._media_sessions["42"] is capture and capture.leases
    await service._stop_media_sessions("test_complete")


def test_synchronous_capture_fence_clears_history_and_rejects_late_worker():
    from test_media_session import SPS_PPS_IDR, _receiver_kwargs, _session

    capture, client = _session()
    capture.acquire(LeasePurpose.HOMEKIT_LIVE)
    callbacks = _receiver_kwargs(client.futures[0])
    callbacks["on_encoded_video"](SPS_PPS_IDR, 200)
    assert capture.buffer.snapshot().packets
    requests = capture.fence()
    assert not capture.buffer.snapshot().packets
    assert not client.futures[0].finished  # The synchronous fence never waits for I/O.
    callbacks["on_encoded_video"](SPS_PPS_IDR, 300)
    assert not capture.buffer.snapshot().packets
    with pytest.raises(RuntimeError, match="privacy"):
        capture.acquire(LeasePurpose.HOMEKIT_LIVE)
    capture.finish_fence(requests)
    assert capture.receiver_retired and len(client.futures) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("permitted", [False, None])
async def test_muted_original_and_legacy_ingress_never_retains_or_forwards_audio(permitted):
    from test_media_legacy_lane import Encoder, audio_packets, frame
    from test_media_session import SPS_PPS_IDR, FakeClient, _receiver_kwargs, fake_supervisor

    from halo_server.media.session import CameraMediaSession

    client, forwarded, traced = FakeClient(), [], []
    capture = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="fixture"),
        receiver_supervisor=fake_supervisor,
        audio_encoder_factory=Encoder,
        on_raw_audio=lambda *args: forwarded.append(args),
        diagnostic_trace=SimpleNamespace(
            event=lambda *args, **kwargs: None, media=lambda *args, **kwargs: traced.append(args)
        ),
    )
    capture.set_source_audio_admission(lambda: permitted)
    capture.acquire(LeasePurpose.HOMEKIT_LIVE)
    callbacks = _receiver_kwargs(client.futures[0])
    try:
        assert callbacks["on_encoded_audio"](b"\xf8\xff\xfe", 100) is False
        capture._raw_audio(1, b"\x00\x00" * 1920, 2, 48000)
        await callbacks["on_decoded_audio"](frame())
        callbacks["diagnostic_trace"].media("source_frame", "audio", b"secret-samples")
        callbacks["on_encoded_video"](SPS_PPS_IDR, 200)
        assert capture.buffer.snapshot().packets  # Video is still admitted.
        assert not capture.original_audio_buffer.snapshot().packets
        assert not audio_packets(capture) and not forwarded and not traced
    finally:
        capture.stop_all()


@pytest.mark.asyncio
async def test_store_reread_retires_other_camera_changed_outside_cached_snapshot(tmp_path):
    from test_native_service import Camera

    service, _, sessions = service_fixture(tmp_path, cameras=[Camera("42"), Camera("43")])
    profile = sessions.active_scope.profile_id
    first, _ = await service._acquire_camera_consumer("42", LeasePurpose.HOMEKIT_LIVE, homekit=True)
    second, _ = await service._acquire_camera_consumer(
        "43", LeasePurpose.HOMEKIT_LIVE, homekit=True
    )
    external = CameraPrivacy(service._camera_privacy.store.path)
    external.set_mode(profile, "43", "homekit", expected_revision=0)
    # An idempotent camera42 save must still fence camera43's changed authority.
    await service.camera_privacy_set(
        profile_id=profile, account_generation=1, camera_id="42", mode="ring", expected_revision=0
    )
    assert service._media_sessions["42"] is first
    assert "43" not in service._media_sessions
    await service._drain_media_tasks()
    assert not second.leases
    await service._stop_media_sessions("test_complete")


@pytest.mark.asyncio
async def test_real_source_audio_gate_uses_override_and_cannot_revive_old_token(tmp_path):
    service, client, sessions = service_fixture(tmp_path)
    epoch = service._camera_privacy_epoch("42", homekit=True)
    assert service._camera_source_audio_current("42", epoch)
    client.all_devices[0]._attrs["settings"]["enable_audio_recording"] = False
    client._camera_privacy_receipt = ring_privacy_receipt(
        client.all_devices, service._camera_privacy._clock()
    )
    service._remember_camera_privacy(client)
    assert not service._camera_source_audio_current("42", epoch)
    assert not service._camera_source_audio_current(
        "42", service._camera_privacy_epoch("42", homekit=True)
    )
    await service.camera_privacy_set(
        profile_id=sessions.active_scope.profile_id,
        account_generation=1,
        camera_id="42",
        mode="homekit",
        expected_revision=0,
    )
    assert service._camera_source_audio_current(
        "42", service._camera_privacy_epoch("42", homekit=True)
    )
    assert not service._camera_source_audio_current("42", epoch)


def test_read_initialization_creates_no_directory_or_lock(tmp_path):
    parent = tmp_path / "absent-state"
    manager = CameraPrivacy(parent / "privacy.json")
    assert manager.available and not parent.exists()
    profile = str(uuid4())
    manager.set_mode(profile, "42", "homekit", expected_revision=0)
    lock = parent / ".privacy.json.lock"
    assert lock.exists()
    lock.unlink()
    restored = CameraPrivacy(manager.store.path)
    assert restored.available and not lock.exists()
    assert restored._preferences.camera(profile, "42").mode == "homekit"


def test_same_final_mode_with_newer_durable_revision_changes_source_token(tmp_path):
    manager, scope, *_ = fixture(tmp_path)
    initial = manager.epoch("42")
    external = CameraPrivacy(manager.store.path)
    external.set_mode(scope.profile_id, "42", "homekit", expected_revision=0)
    external.set_mode(scope.profile_id, "42", "ring", expected_revision=1)
    with pytest.raises(CameraPrivacyConflict):
        manager.set_mode(scope.profile_id, "42", "ring", expected_revision=0)
    assert manager.view("42")["mode"] == "ring"
    assert manager.view("42")["revision"] == 2
    assert manager.epoch("42") != initial
