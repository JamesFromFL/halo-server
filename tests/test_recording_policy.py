"""Only trusted, current demand can own real pre-event recording history."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from test_homekit_bulk_cancel import until
from test_recording_capture import setup

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.h264 import VideoConfiguration
from halo_server.media.recording import RecordingAudioConfiguration, RecordingConfiguration
from halo_server.media.recording_policy import RecordingAuthority, RecordingPolicyOwner


def configuration():
    return RecordingConfiguration(
        VideoConfiguration(1280, 720, 15, "main", "4.0", 2000), RecordingAudioConfiguration()
    )


@pytest.mark.asyncio
async def test_warm_requires_owner_mains_and_actual_current_hap_policy():
    controller, _, boot, (scope,), _, source, captures = await setup()
    state = [RecordingAuthority(True, True, configuration())]
    owner = RecordingPolicyOwner(controller, captures, lambda scope: state[0])
    try:
        await owner.ready(boot)
        assert not source.admissions and owner.status(scope).state == "disabled"
        state[0] = replace(state[0], owner_opt_in=True)
        await owner.refresh(boot)
        assert not source.admissions
        state[0] = replace(state[0], mains_confirmed=True)
        # A trusted setting alone cannot bypass HomeKitCameraActive/policy off.
        controller.fence_camera(boot, scope)
        scope = controller.inventory().cameras[0].scope
        await owner.refresh(boot)
        assert not source.admissions and not owner.pending_resources
    finally:
        await owner.close()
        await captures.close()
        await controller.close()


@pytest.mark.asyncio
async def test_actual_warm_coverage_admits_only_after_four_seconds_and_quality_keeps_history():
    controller, _, boot, (scope,), _, source, captures = await setup()
    state = [RecordingAuthority(True, True, configuration(), True, True)]
    notifications = []
    owner = RecordingPolicyOwner(
        controller, captures, lambda scope: state[0], changed=lambda: notifications.append(True)
    )
    try:
        await owner.ready(boot)
        await until(lambda: len(source.admissions) == 1)
        entry = owner._entries[scope.source_key]
        await until(lambda: entry.capture is not None)
        with pytest.raises(HomeKitOperationError) as cold:
            owner.authorize_open(boot, scope, state[0].effective_configuration)
        assert cold.value.code == "prebuffer_unavailable"
        for index in range(53):
            previous = entry.capture.history.cursor
            source.append(index)
            await until(lambda previous=previous: entry.capture.history.cursor >= previous + 6)
        status = owner.status(scope)
        assert status.history_ready and status.video_seconds >= 4 and status.audio_seconds >= 4
        await owner.refresh(boot)
        count = len(notifications)
        await owner.refresh(boot)
        assert count >= 2 and len(notifications) == count
        owner.authorize_open(boot, scope, state[0].effective_configuration)
        original = entry.capture
        changed = replace(
            state[0].configuration, video=replace(state[0].configuration.video, fps=30)
        )
        state[0] = replace(state[0], configuration=changed)
        await owner.refresh(boot)
        assert owner._entries[scope.source_key].capture is original
        assert len(source.admissions) == 1 and owner.status(scope).history_ready
        owner.authorize_open(boot, scope, changed)
        with pytest.raises(HomeKitOperationError):
            owner.authorize_open(boot, scope, configuration())
    finally:
        await owner.close()
        await captures.close()
        await controller.close()


@pytest.mark.asyncio
async def test_warm_rollover_has_no_overlap_and_reports_rebuffer_without_old_history():
    controller, _, boot, (scope,), now, source, captures = await setup()
    state = RecordingAuthority(True, False, configuration(), True, True)
    owner = RecordingPolicyOwner(controller, captures, lambda scope: state, clock=lambda: now[0])
    try:
        await owner.ready(boot)
        await until(lambda: len(source.admissions) == 1)
        original = owner._entries[scope.source_key]
        await until(lambda: original.capture is not None)
        now[0] += 571
        await owner.refresh(boot)
        await until(lambda: len(source.admissions) == 2)
        replacement = owner._entries[scope.source_key]
        assert replacement is not original and source.closes == 1
        assert captures.pending_resources == owner.pending_resources == 1
        assert not owner.status(scope).history_ready
    finally:
        await owner.close()
        await captures.close()
        await controller.close()


@pytest.mark.asyncio
async def test_exact_capture_stop_retries_failed_warm_without_stopping_cold_recording():
    controller, _, boot, (scope,), _, source, captures = await setup()
    warm_id, cold_id = str(uuid4()), str(uuid4())
    warm = await captures.open(
        boot, scope, warm_id, warm=True, owner_opt_in=True, mains_confirmed=True
    )
    cold = await captures.open(boot, scope, cold_id)
    try:
        source.fail_close = True
        with pytest.raises(HomeKitOperationError):
            await captures.stop(boot, scope, warm_id)
        assert cold.handle.is_current() and captures.pending_resources == 2
        with pytest.raises(HomeKitOperationError) as conflict:
            await captures.stop(
                boot, replace(scope, privacy_generation=scope.privacy_generation + 1), cold_id
            )
        assert conflict.value.code == "session_conflict"
        source.fail_close = False
        await captures.stop(boot, scope, warm_id)
        assert warm.retired and cold.handle.is_current() and captures.pending_resources == 1
        await captures.stop(boot, scope, str(uuid4()))
        assert cold.handle.is_current()
    finally:
        source.fail_close = False
        await captures.close()
        await controller.close()


@pytest.mark.asyncio
async def test_warm_revocation_retains_failed_cleanup_and_cannot_rearm_around_it():
    controller, _, boot, (scope,), _, source, captures = await setup()
    state = [RecordingAuthority(True, False, configuration(), True, True)]
    owner = RecordingPolicyOwner(controller, captures, lambda scope: state[0])
    try:
        await owner.ready(boot)
        await until(lambda: len(source.admissions) == 1)
        await until(lambda: owner._entries[scope.source_key].capture is not None)
        source.fail_close = True
        state[0] = replace(state[0], owner_opt_in=False)
        with pytest.raises(ExceptionGroup):
            await owner.refresh(boot)
        assert owner.cleanup_failures == owner.pending_resources == 1
        assert owner.warm_budget == {"capacity": 2, "reserved": 1}
        assert owner.status(scope).state == "cleanup_pending"
        with pytest.raises(ExceptionGroup):
            await owner.close()
        with pytest.raises(HomeKitOperationError):
            await owner.ready(str(uuid4()))
        source.fail_close = False
        await owner.retired(boot)
        assert not owner.pending_resources and not captures.pending_resources
        assert owner.warm_budget == {"capacity": 2, "reserved": 0}
        await owner.ready(boot)
        assert not owner.pending_resources  # Revoked owner setting stays off.
    finally:
        source.fail_close = False
        await owner.close()
        await captures.close()
        await controller.close()


@pytest.mark.asyncio
async def test_warm_owner_capacity_counts_pending_acquisitions_and_old_boot_does_not_stop_new():
    controller, _, boot, scopes, _, _, captures = await setup(3)
    state = RecordingAuthority(True, False, configuration(), True, True)
    gate = asyncio.Event()
    opened = []
    original = captures.open

    async def blocked(*args, **kwargs):
        opened.append(args)
        await gate.wait()
        return await original(*args, **kwargs)

    captures.open = blocked
    owner = RecordingPolicyOwner(controller, captures, lambda scope: state)
    try:
        await owner.ready(boot)
        await until(lambda: len(opened) == 2)
        assert owner.pending_resources == 2
        assert owner.warm_budget == {"capacity": 2, "reserved": 2}
        assert all(entry.capture is None for entry in owner._entries.values())
        assert owner.status(scopes[2]).state == "capacity"
        assert not owner.status(scopes[2]).history_ready
        await owner.refresh(boot)
        assert len(opened) == 2
        await owner.retired(str(uuid4()))
        assert owner.pending_resources == 2 and owner._maintenance is not None
    finally:
        gate.set()
        await owner.close()
        await captures.close()
        await controller.close()


def test_authority_audio_toggle_preserves_selected_aac_without_granting_output_audio():
    selected = configuration()
    state = RecordingAuthority(True, False, selected, True, True)
    assert state.configuration.audio is not None and state.effective_configuration.audio is None
    assert (
        state.capture_signature
        == replace(
            state, configuration=replace(selected, video=replace(selected.video, max_bit_rate=1000))
        ).capture_signature
    )


@pytest.mark.asyncio
async def test_retirement_bounds_cancel_ignoring_maintenance_and_retains_exact_owner_for_retry(
    monkeypatch,
):
    from halo_server.media import recording_policy as module

    controller, _, boot, _, _, _, captures = await setup()
    owner = RecordingPolicyOwner(controller, captures, lambda scope: None)
    entered, release = asyncio.Event(), asyncio.Event()
    cancellations = []

    async def stubborn():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellations.append(True)
            await release.wait()

    try:
        await owner.ready(boot)
        owner._maintenance.cancel()
        await asyncio.gather(owner._maintenance, return_exceptions=True)
        maintenance = owner._maintenance = asyncio.create_task(stubborn())
        await entered.wait()
        monkeypatch.setattr(module, "CLEANUP_SECONDS", 0.02)
        for _ in range(2):
            with pytest.raises(HomeKitOperationError) as pending:
                await owner.retired(boot)
            assert pending.value.code == "cleanup_pending"
            assert owner._maintenance is maintenance and not maintenance.done()
            assert owner.pending_resources == owner.cleanup_failures == 1
            with pytest.raises(HomeKitOperationError):
                await owner.ready(boot)
        assert len(cancellations) == 1
        with pytest.raises(ExceptionGroup):
            await owner.close()
        release.set()
        await maintenance
        await owner.retired(boot)
        assert owner._maintenance is None and owner._retiring_boot is None
        await owner.ready(boot)
    finally:
        release.set()
        await owner.close()
        await captures.close()
        await controller.close()
