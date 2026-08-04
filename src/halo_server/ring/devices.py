"""Allowlisted public device models for Halo Server APIs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

CAMERA_FAMILIES = frozenset({"doorbots", "stickup_cams", "authorized_doorbots"})
SDK_MODEL_FALLBACKS = {
    "stickup_cam_mini_v3": "Unknown Stickup Cam",
}


@dataclass(frozen=True)
class DeviceSummary:
    """Small stable subset of a Ring device safe for API consumers."""

    id: str
    name: str
    family: str
    kind: str
    model: str
    online: bool | None
    battery_percent: int | None
    motion_detection: bool | None
    has_light: bool
    has_siren: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize_device(device: Any) -> DeviceSummary | None:
    """Serialize a Ring camera without exposing arbitrary SDK attributes."""
    family = _text_attr(device, "family")
    if family not in CAMERA_FAMILIES:
        return None
    device_id = _identifier_attr(device, "id")
    if device_id is None:
        return None
    return DeviceSummary(
        id=device_id,
        name=_text_attr(device, "name"),
        family=family,
        kind=(kind := _text_attr(device, "kind")),
        model=SDK_MODEL_FALLBACKS.get(kind, _text_attr(device, "model")),
        online=_bool_attr(device, "subscribed"),
        battery_percent=_battery_attr(device),
        motion_detection=_bool_attr(device, "motion_detection"),
        has_light=_capability(device, "light"),
        has_siren=_capability(device, "siren"),
    )


def summarize_cameras(devices: list[Any]) -> list[dict[str, Any]]:
    summaries = [summary for device in devices if (summary := summarize_device(device))]
    summaries.sort(key=lambda summary: (summary.name.casefold(), summary.id))
    return [summary.as_dict() for summary in summaries]


def _safe_attr(device: Any, name: str) -> Any:
    try:
        return getattr(device, name, None)
    except Exception:
        return None


def _identifier_attr(device: Any, name: str) -> str | None:
    value = _safe_attr(device, name)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _text_attr(device: Any, name: str) -> str:
    value = _safe_attr(device, name)
    return value if isinstance(value, str) else ""


def _bool_attr(device: Any, name: str) -> bool | None:
    value = _safe_attr(device, name)
    return value if isinstance(value, bool) else None


def _battery_attr(device: Any) -> int | None:
    value = _safe_attr(device, "battery_life")
    if isinstance(value, bool):
        return None
    try:
        percentage = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(percentage, 100))


def _capability(device: Any, capability: str) -> bool:
    checker = _safe_attr(device, "has_capability")
    if not callable(checker):
        return False
    try:
        return bool(checker(capability))
    except Exception:
        return False
