"""Read-only, GET-scoped observations of camera lighting.

Native manual-duration fields do not establish a saved timer policy. They are
retained only as immutable diagnostics and never interpreted as an On request,
motion enablement, or an indefinite duration.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from halo_server.account_scope import AccountScope
from halo_server.homekit.text import literal_text
from halo_server.ring.devices import CAMERA_FAMILIES, has_ring_camera_capability
from halo_server.ring.lighting import LightingReservation

LIGHTING_STATE_TTL = 90.0
MAX_LIGHTING_CAMERAS = 256


@dataclass(frozen=True)
class CameraLightingObservation:
    camera_id: str
    device: object
    light: bool | None
    motion_duration_seconds: int | None
    always_on: bool | None = None
    always_on_duration: int | float | None = None
    always_on_duration_secs: int | float | None = None


@dataclass(frozen=True)
class CameraLightingReceipt:
    received_at: float
    mutation_revision: int
    rows: tuple[CameraLightingObservation, ...]


def _duration(value):
    # Do not round, coerce text, or interpret zero as indefinite.
    return value if type(value) is int and 30 <= value <= 900 else None


def _diagnostic_duration(value):
    return (
        value
        if type(value) in (int, float) and 0 <= value <= 2**53 - 1 and math.isfinite(value)
        else None
    )


def camera_lighting_receipt(devices, *, received_at, mutation_revision):
    """Copy scalar evidence in the uninterrupted Ring-loop turn after a GET.

    The revision is sampled before that GET, so a concurrent lighting write
    cannot falsely certify an older response as post-command evidence.
    """
    rows, duplicates = {}, set()
    for device in devices:
        try:
            if getattr(device, "family", None) not in CAMERA_FAMILIES:
                continue
            identifier = getattr(device, "id", None)
            if type(identifier) not in (str, int):
                continue
            identifier = str(identifier)
            if not literal_text(identifier, 128, empty=False):
                continue
        except Exception:
            continue
        if identifier in rows:
            duplicates.add(identifier)
        if len(rows) >= MAX_LIGHTING_CAMERAS and identifier not in rows:
            # An incomplete oversized inventory must not choose arbitrary rows.
            return CameraLightingReceipt(received_at, mutation_revision, ())
        light = duration = always_on = manual_minutes = manual_seconds = None
        try:
            attrs = device._attrs
            if isinstance(attrs, dict):
                status = attrs.get("led_status")
                light = status == "on" if type(status) is str and status in ("on", "off") else None
                settings = attrs.get("settings")
                if isinstance(settings, dict):
                    floodlight = settings.get("floodlight_settings")
                    if isinstance(floodlight, dict):
                        duration = _duration(floodlight.get("duration"))
                        always_on = floodlight.get("always_on")
                        always_on = always_on if type(always_on) is bool else None
                        manual_minutes = _diagnostic_duration(floodlight.get("always_on_duration"))
                        manual_seconds = _diagnostic_duration(
                            floodlight.get("always_on_duration_secs")
                        )
                    alternate = settings.get("light_settings")
                    # This duplicate surface cannot silently override or
                    # rescue absent/malformed/conflicting floodlight data.
                    if ("light_settings" in settings and not isinstance(alternate, dict)) or (
                        isinstance(alternate, dict)
                        and "duration" in alternate
                        and _duration(alternate["duration"]) != duration
                    ):
                        duration = None
        except Exception:
            light = duration = always_on = manual_minutes = manual_seconds = None
        rows[identifier] = CameraLightingObservation(
            identifier, device, light, duration, always_on, manual_minutes, manual_seconds
        )
    return CameraLightingReceipt(
        received_at,
        mutation_revision,
        tuple(row for key, row in rows.items() if key not in duplicates),
    )


def camera_lighting_view(service, camera_id, device):
    """Project existing evidence only: no refresh, files, commands, or timers."""
    result = {
        "fresh": False,
        "observed_age_seconds": None,
        "light": None,
        "motion_duration_seconds": None,
        "manual_timer_status": "unverified",
        "activation_source": None,
        "reason": "source_unknown",
    }
    try:
        if not literal_text(camera_id, 128, empty=False):
            return result
        client = service._media_client
        scope = service._current_account_scope()

        def current():
            return (
                client is not None
                and isinstance(scope, AccountScope)
                and service._media_client is client
                and service._sessions.active_client is client
                and service._current_account_scope() == scope
                and service._account_authority_current()
                and service._account_integrations_ready is True
                and client.is_authenticated is True
                and service._find_media_device(camera_id) is device
                and getattr(device, "family", None) in CAMERA_FAMILIES
                and has_ring_camera_capability(device, "light")
            )

        if not current():
            return result
        receipt = getattr(client, "_camera_lighting_receipt", None)
        reservation = getattr(client, "lighting_reservation", None)
        if (
            not isinstance(receipt, CameraLightingReceipt)
            or not isinstance(reservation, LightingReservation)
            or type(receipt.rows) is not tuple
            or len(receipt.rows) > MAX_LIGHTING_CAMERAS
            or type(receipt.received_at) not in (int, float)
            or not math.isfinite(receipt.received_at)
            or type(receipt.mutation_revision) is not int
            or receipt.mutation_revision < 0
        ):
            return result
        rows = [row for row in receipt.rows if row.camera_id == camera_id]
        if len(rows) != 1 or rows[0].device is not device:
            return result
        age = time.monotonic() - receipt.received_at
        if not math.isfinite(age) or age < 0:
            return result
        result["observed_age_seconds"] = age
        if not reservation.observation_current(receipt.mutation_revision, camera_id):
            result["reason"] = "lighting_changed"
        elif age > LIGHTING_STATE_TTL:
            result["reason"] = "source_stale"
        else:
            row = rows[0]
            result.update(
                fresh=True,
                light=row.light if type(row.light) is bool else None,
                motion_duration_seconds=_duration(row.motion_duration_seconds),
            )
            if result["light"] is not None and result["motion_duration_seconds"] is not None:
                result["reason"] = None
        # A Ring-loop replacement/write can race an owner read. Revalidate
        # identity and revision before returning any supposedly current fields.
        if not current() or getattr(client, "_camera_lighting_receipt", None) is not receipt:
            return result | {
                "fresh": False,
                "observed_age_seconds": None,
                "light": None,
                "motion_duration_seconds": None,
                "reason": "source_unknown",
            }
        if not reservation.observation_current(receipt.mutation_revision, camera_id):
            result.update(
                fresh=False, light=None, motion_duration_seconds=None, reason="lighting_changed"
            )
        return result
    except Exception:
        # A malformed SDK wrapper must not break management of other devices.
        return result | {
            "fresh": False,
            "observed_age_seconds": None,
            "light": None,
            "motion_duration_seconds": None,
            "reason": "source_unknown",
        }
