import json
import stat

import pytest

from halo_server.devices.policy import (
    DeviceCommandDenied,
    DevicePolicy,
    DevicePolicyConflictError,
    DevicePolicyError,
    DevicePolicyStore,
    classify_action,
    deny_device_command,
    safety_snapshot,
)


def test_policy_defaults_disabled_without_touching_files_and_is_durable_private(tmp_path):
    path = tmp_path / "private" / "devices.json"
    store = DevicePolicyStore(path)
    assert store.load() == DevicePolicy()
    assert not path.parent.exists()
    enabled = store.set_enabled(True, expected_revision=0)
    assert enabled.enabled and enabled.revision == 1
    assert DevicePolicyStore(path).load() == enabled
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert store.set_enabled(True, expected_revision=1) == enabled
    with pytest.raises(DevicePolicyConflictError):
        store.set_enabled(False, expected_revision=0)
    assert store.load().enabled
    assert not store.set_enabled(False, expected_revision=1).enabled


@pytest.mark.parametrize(
    "action",
    [
        "panic",
        "alarm.fire",
        "send-medical-alarm",
        "burglAr",
        "trigger/emergency",
        "dispatch",
        "bypass",
        "siren",
        "police",
        "duress",
        "alarm.arm",
        "lock.unlock",
        "light.on",
        "refresh",
        "UNKNOWN",
        {"panic": True},
        None,
        "x" * 10000,
    ],
)
def test_every_command_denied_before_any_device_side_effect(action):
    side_effects = []
    with pytest.raises(DeviceCommandDenied) as error:
        deny_device_command(action)
        side_effects.append("sent")
    assert not error.value.possibly_sent
    assert not side_effects


def test_enabling_monitoring_never_enables_commands_or_mutable_safety_flags(tmp_path):
    store = DevicePolicyStore(tmp_path / "devices.json")
    store.set_enabled(True, expected_revision=0)
    with pytest.raises(DeviceCommandDenied) as error:
        deny_device_command("trigger-panic")
    assert error.value.code == "dangerous_device_action_disabled"
    result = safety_snapshot()
    result["panic_enabled"] = True
    assert safety_snapshot() == {
        "read_only": True,
        "panic_enabled": False,
        "emergency_dispatch_enabled": False,
    }
    assert classify_action("GET/new_write_alias") == "control"


@pytest.mark.parametrize(
    "extra",
    [
        {"panic_enabled": True},
        {"controls_enabled": True},
        {"enabled": 1},
        {"version": 2},
        {"revision": True},
    ],
)
def test_policy_cannot_grant_extra_authority_or_accept_malformed_values(tmp_path, extra):
    store = DevicePolicyStore(tmp_path / "devices.json")
    store.set_enabled(False, expected_revision=0)
    # No-op setting deliberately leaves no file; write a corrupt fixture directly.
    store.path.write_text(json.dumps(DevicePolicy().as_dict() | extra))
    store.path.chmod(0o600)
    with pytest.raises(DevicePolicyError):
        store.load()


def test_policy_rejects_symlink_and_oversize_input(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(DevicePolicy().as_dict()))
    target.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(DevicePolicyError):
        DevicePolicyStore(link).load()
    target.write_bytes(b" " * 4097)
    with pytest.raises(DevicePolicyError):
        DevicePolicyStore(target).load()
