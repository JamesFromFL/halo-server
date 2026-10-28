"""An external settings owner cannot leave already admitted audio unmuted."""

from dataclasses import asdict

import pytest
from test_homekit_broker import request
from test_homekit_selection import selected_bridge

from halo_server.homekit.models import CameraPolicy, HomeKitOperationError
from halo_server.homekit.settings import (
    AudioPreferences,
    AudioSettingsConflict,
    AudioSettingsStore,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_path", ["inventory", "cached-noop-conflict"])
async def test_external_audio_mute_fences_current_python_scope_before_any_child_ack(
    tmp_path, reload_path
):
    bridge, boot, scope, service, client, _ = await selected_bridge(tmp_path, count=2)
    scope = await service.homekit.update_policy(
        boot, scope, CameraPolicy(synchronized=True, streaming=True, live_audio=True)
    )
    other = service.homekit.inventory().cameras[1].scope
    before = bridge._audio_settings
    row = before.camera(scope.profile_id, scope.camera_id)
    external = AudioSettingsStore(bridge._audio_store.path)
    saved = external.set_preferences(
        row.profile_id,
        row.camera_id,
        row.accessory_id,
        AudioPreferences(microphone_muted=True),
        expected_revision=before.revision,
    )
    try:
        service.homekit.authorize(boot, scope, "live_audio")
        if reload_path == "inventory":
            await bridge._load_registry()
        else:
            # These equal the bridge cache, so its initial no-op check cannot
            # detect the external revocation. The disk CAS reload must fence.
            with pytest.raises(AudioSettingsConflict):
                await bridge.set_audio_settings(boot, scope, before.revision, row.preferences)
        assert bridge._audio_settings == saved
        with pytest.raises(HomeKitOperationError):
            service.homekit.authorize(boot, scope, "live_audio")
        service.homekit.validate_selected_scope(boot, other)
        current = next(
            camera.scope
            for camera in service.homekit.inventory().cameras
            if camera.scope.camera_id == scope.camera_id
        )
        denied = await bridge._broker.request(
            request(
                boot,
                "policy_update",
                {
                    "scope": asdict(current),
                    "policy": asdict(
                        CameraPolicy(synchronized=True, streaming=True, live_audio=True)
                    ),
                },
            )
        )
        assert not denied["accepted"] and denied["code"] == "policy-denied"
        assert client.submissions == 0
    finally:
        await bridge._broker.retired(boot)
