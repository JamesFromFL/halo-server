"""Private monitoring preference and unconditional first-wave command denial.

Monitoring is opt-in. It never enables a command; panic/emergency actions have no
configuration escape hatch. Saved policy cannot grant dispatch authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore


class DevicePolicyError(PrivateJsonError):
    pass


class DevicePolicyConflictError(DevicePolicyError):
    pass


class DeviceCommandDenied(PermissionError):
    def __init__(self, code: str = "device_controls_disabled") -> None:
        self.code = code
        self.possibly_sent = False
        super().__init__("Non-camera device controls are disabled")


def classify_action(action: object) -> str:
    """Advisory categorization only; unknown aliases remain denied by the gate."""
    if not isinstance(action, str) or not action or len(action) > 128:
        return "unknown"
    tokens = set(re.findall(r"[a-z]+", action.lower()))
    if tokens.intersection(
        {
            "panic",
            "emergency",
            "burglar",
            "fire",
            "medical",
            "police",
            "dispatch",
            "bypass",
            "siren",
            "duress",
        }
    ):
        return "dangerous"
    if action in {"discover", "read", "status", "monitor", "refresh"}:
        return "read"
    return "control"


def deny_device_command(action: object) -> None:
    """Call at the dispatch boundary before resolving or sending any command."""
    code = (
        "dangerous_device_action_disabled"
        if classify_action(action) == "dangerous"
        else ("device_controls_disabled")
    )
    raise DeviceCommandDenied(code)


def safety_snapshot() -> dict[str, bool]:
    return {"read_only": True, "panic_enabled": False, "emergency_dispatch_enabled": False}


@dataclass(frozen=True)
class DevicePolicy:
    version: int = 1
    revision: int = 0
    enabled: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or type(self.enabled) is not bool
        ):
            raise DevicePolicyError("Device monitoring policy is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "revision": self.revision, "enabled": self.enabled}


def _decode(payload: dict[str, Any]) -> DevicePolicy:
    if set(payload) != {"version", "revision", "enabled"}:
        raise DevicePolicyError("Device monitoring policy has an invalid format")
    return DevicePolicy(**payload)


class DevicePolicyStore(PrivateJsonStore[DevicePolicy]):
    def __init__(self, path: str | Path) -> None:
        super().__init__(
            path,
            snapshot_type=DevicePolicy,
            decode=_decode,
            default=DevicePolicy,
            max_bytes=4096,
            label="Device monitoring policy",
            noun="device policy",
            error_type=DevicePolicyError,
            conflict_type=DevicePolicyConflictError,
        )

    def load(self) -> DevicePolicy:
        # Discovery remains inert on existing installations until explicitly
        # enabled. Merely rendering owner state must not create a new store.
        try:
            self.path.lstat()
        except FileNotFoundError:
            return DevicePolicy()
        except OSError as exc:
            raise DevicePolicyError("Cannot access device monitoring policy") from exc
        return super().load()

    def set_enabled(self, enabled: bool, *, expected_revision: int) -> DevicePolicy:
        if type(enabled) is not bool:
            raise DevicePolicyError("Device monitoring preference must be boolean")
        return self.update(
            lambda current: replace(current, enabled=enabled), expected_revision=expected_revision
        )
