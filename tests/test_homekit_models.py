from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from halo_server.homekit.models import (
    CameraCapabilities,
    CameraPolicy,
    MediaScope,
    NativeCamera,
    NativeInventory,
    safe_display_text,
)


def camera():
    return NativeCamera(
        MediaScope(str(uuid4()), "42", 1, 1, 1),
        "Camera",
        "doorbots",
        "kind",
        "model",
        CameraCapabilities(),
    )


def test_native_camera_is_immutable_and_unknown_format_and_reachability_stay_unknown():
    value = camera()
    assert value.connection_state is None
    assert value.observed_format is None
    assert value.battery_percent is None
    with pytest.raises(FrozenInstanceError):
        value.name = "Changed"


@pytest.mark.parametrize("text", ["control\n", "format\u200e", "surrogate\ud800", "\x7f"])
def test_scope_and_literal_metadata_reject_nonliteral_values(text):
    with pytest.raises(ValueError):
        replace(camera().scope, camera_id=text)
    with pytest.raises(ValueError):
        replace(camera(), name=text)
    assert safe_display_text(text) != text


def test_inventory_rejects_same_api_id_in_different_device_generation():
    first = camera()
    second = replace(first, scope=replace(first.scope, device_generation=2))
    with pytest.raises(ValueError, match="duplicate"):
        NativeInventory(1, (first, second), True, first.scope.profile_id, 1)


def test_inventory_never_mix_accounts_or_claim_unscoped_authority():
    first, second = camera(), camera()
    with pytest.raises(ValueError):
        NativeInventory(1, (first, second), True, first.scope.profile_id, 1)
    with pytest.raises(ValueError):
        NativeInventory(0, (), True)
    assert NativeInventory(0, (), False).profile_id is None


@pytest.mark.parametrize(
    "capability",
    ["video", "audio", "snapshot", "talkback", "light", "siren", "doorbell", "battery"],
)
def test_positive_capability_requires_matching_evidence(capability):
    with pytest.raises(ValueError):
        CameraCapabilities(**{capability: True})
    value = CameraCapabilities(**{capability: True}, evidence=(f"sdk:{capability}",))
    assert getattr(value, capability)


@pytest.mark.parametrize(
    "field",
    [
        "synchronized",
        "streaming",
        "live_audio",
        "snapshots",
        "recording",
        "recording_audio",
        "talkback",
        "controls",
    ],
)
def test_policy_has_no_truthy_nonboolean_inputs(field):
    with pytest.raises(ValueError):
        CameraPolicy(**{field: 1})
