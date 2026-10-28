"""Real daemon leases stay independent of unsolicited direct-mode events."""

import asyncio

import pytest
from test_native_service import service_fixture

from halo_server.configuration import ServerMode
from halo_server.incidents.coordinator import IncidentAction, IncidentActionType
from halo_server.media.leases import LeasePurpose


@pytest.mark.asyncio
async def test_direct_mode_event_cannot_start_compatibility_capture(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    service._main_loop = asyncio.get_running_loop()
    await service._execute_media_actions("42", [], 17)
    assert not service._media_sessions


@pytest.mark.asyncio
async def test_dual_mode_preserves_separate_legacy_event_lease(tmp_path):
    service, _, _ = service_fixture(tmp_path, mode=ServerMode.DUAL)
    service._main_loop = asyncio.get_running_loop()
    await service._execute_media_actions("42", [], 17)
    capture = service._media_sessions["42"]
    assert capture.acquired == [LeasePurpose.EVENT]
    await service._execute_media_actions(
        "42", [IncidentAction(IncidentActionType.STOP_CAPTURE, 17, "fixture")], None
    )
    assert not capture.leases


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "purpose,prefix",
    [
        ("HOMEKIT_RECORDING", "homekit-recording-"),
        ("HOMEKIT_RECORDING_WARM", "homekit-recording-warm-"),
    ],
)
async def test_recording_consumer_uses_exact_independent_purpose(tmp_path, purpose, prefix):
    service, _, _ = service_fixture(tmp_path)
    service._main_loop = asyncio.get_running_loop()
    purpose = getattr(LeasePurpose, purpose)
    capture, lease = await service._acquire_camera_consumer("42", purpose, admission=lambda: True)
    assert capture.acquired == [purpose]
    assert lease.startswith(prefix)
    assert await service._renew_camera_consumer("42", capture, lease, admission=lambda: True)
    await service._release_camera_consumer("42", capture, lease)
    assert not capture.leases


@pytest.mark.asyncio
async def test_recording_lease_cannot_start_after_admission_was_revoked(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    service._main_loop = asyncio.get_running_loop()
    with pytest.raises(RuntimeError, match="authority"):
        await service._acquire_camera_consumer(
            "42", LeasePurpose.HOMEKIT_RECORDING, admission=lambda: False
        )
    assert not service._media_sessions
