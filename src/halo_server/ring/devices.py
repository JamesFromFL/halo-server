"""Allowlisted public device models for Halo Server APIs."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from typing import Any

from halo_server.media.ring_speaker import supports_ring_camera_intercom

CAMERA_FAMILIES = frozenset({"doorbots", "stickup_cams", "authorized_doorbots"})
MAX_DEVICE_ID_UNITS = 128
MAX_DEVICE_NAME_UNITS = 256
MAX_DEVICE_KIND_UNITS = 128
MAX_DEVICE_MODEL_UNITS = 128
SDK_MODEL_FALLBACKS = {
    "stickup_cam_mini_v3": "Unknown Stickup Cam",
}
# The observed mini_v3 is Halo's 2K Indoor Cam Plus media override; Ring's
# public product contract includes a siren, but its internal-kind map is private.
INFERRED_KIND_CAPABILITY_FALLBACKS = {
    "stickup_cam_mini_v3": frozenset({"siren"}),
}

_log = logging.getLogger(__name__)


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
    light_on: bool | None
    has_siren: bool
    siren_on: bool | None
    has_intercom: bool

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
    kind = _text_attr(device, "kind", maximum_units=MAX_DEVICE_KIND_UNITS)
    model = SDK_MODEL_FALLBACKS.get(kind)
    if model is None:
        model = _text_attr(device, "model", maximum_units=MAX_DEVICE_MODEL_UNITS)
    has_light = has_ring_camera_capability(device, "light")
    has_siren = has_ring_camera_capability(device, "siren")
    return DeviceSummary(
        id=device_id,
        name=_text_attr(device, "name", maximum_units=MAX_DEVICE_NAME_UNITS),
        family=family,
        kind=kind,
        model=model,
        online=_bool_attr(device, "subscribed"),
        battery_percent=_battery_attr(device),
        motion_detection=_bool_attr(device, "motion_detection"),
        has_light=has_light,
        light_on=_bool_attr(device, "light") if has_light else None,
        has_siren=has_siren,
        siren_on=_siren_on_attr(device) if has_siren else None,
        has_intercom=supports_ring_camera_intercom(device),
    )


def summarize_cameras(devices: list[Any]) -> list[dict[str, Any]]:
    unique: dict[str, DeviceSummary] = {}
    for device in devices:
        summary = summarize_device(device)
        if summary is None:
            continue
        if summary.id in unique:
            _log.warning("Ignoring duplicate Ring camera identifier id=%s", summary.id)
            continue
        unique[summary.id] = summary
    summaries = list(unique.values())
    summaries.sort(key=lambda summary: (summary.name.casefold(), summary.id))
    return [summary.as_dict() for summary in summaries]


def supports_ring_camera_snapshot(device: Any) -> bool:
    """Return whether one allowlisted Ring camera supports still images."""
    family = _text_attr(device, "family")
    if family not in CAMERA_FAMILIES:
        return False
    kind = _text_attr(device, "kind", maximum_units=MAX_DEVICE_KIND_UNITS)
    return callable(_safe_attr(device, "async_get_snapshot")) and (
        kind in SDK_MODEL_FALLBACKS or _capability(device, "video")
    )


def has_ring_camera_capability(device: Any, capability: str) -> bool:
    """Return one SDK capability plus Halo's exact-kind compatibility overrides."""
    kind = _text_attr(device, "kind", maximum_units=MAX_DEVICE_KIND_UNITS)
    return capability in INFERRED_KIND_CAPABILITY_FALLBACKS.get(kind, frozenset()) or _capability(
        device, capability
    )


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
    try:
        encoded = normalized.encode("utf-16-le")
    except UnicodeEncodeError:
        return None
    if not normalized or len(encoded) // 2 > MAX_DEVICE_ID_UNITS:
        return None
    return normalized


def _text_attr(
    device: Any,
    name: str,
    *,
    maximum_units: int | None = None,
) -> str:
    value = _safe_attr(device, name)
    if not isinstance(value, str):
        return ""
    if maximum_units is None:
        return value
    encoded = value.encode("utf-16-le", errors="replace")
    return encoded[: maximum_units * 2].decode("utf-16-le", errors="ignore")


def _bool_attr(device: Any, name: str) -> bool | None:
    value = _safe_attr(device, name)
    return value if isinstance(value, bool) else None


def _battery_attr(device: Any) -> int | None:
    if not has_ring_camera_capability(device, "battery"):
        return None
    value = _safe_attr(device, "battery_life")
    if isinstance(value, bool):
        return None
    try:
        percentage = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(percentage, 100))


def _siren_on_attr(device: Any) -> bool | None:
    value = _safe_attr(device, "siren")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value > 0


def _capability(device: Any, capability: str) -> bool:
    checker = _safe_attr(device, "has_capability")
    if not callable(checker):
        return False
    try:
        return bool(checker(capability))
    except Exception:
        return False
