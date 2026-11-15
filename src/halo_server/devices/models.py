"""Bounded immutable observations independent of HomeKit and Ring SDKs.

Identity uses account and provider identifiers, never display names. The catalog
exposes hashed identities rather than raw location or hardware IDs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from halo_server.account_scope import valid_profile_id
from halo_server.homekit.text import literal_text, safe_display_text

MAX_DEVICES = 1024
MAX_OBSERVATIONS = 48
MAX_CAPABILITIES = 32
_TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}\Z")
SOURCE_HEALTH = frozenset({"unknown", "online", "offline", "stale", "unavailable"})
OBSERVATION_STATUS = frozenset({"current", "unknown", "stale"})
_BOOLEAN_OBSERVATIONS = frozenset(
    {
        "contact_open",
        "motion_detected",
        "battery_low",
        "tampered",
        "light_on",
        "leak_detected",
        "freeze_detected",
        "smoke_detected",
        "co_detected",
        "connected",
        "ac_power",
        "siren_active",
        "bypassed",
        "faulted",
        "glass_break_detected",
        "tilt_detected",
        "valve_open",
    }
)
_ENUM_OBSERVATIONS = frozenset({"alarm_mode", "alarm_state", "alarm_signal", "lock_state"})


def valid_generation(value: object, *, zero: bool = False) -> bool:
    return type(value) is int and (0 if zero else 1) <= value < 2**53


def valid_timestamp(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value < 2**53 and math.isfinite(value)


def valid_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def _digest(parts: tuple[str, ...]) -> str:
    body = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class DeviceIdentity:
    profile_id: str
    provider: str
    location_id: str
    asset_id: str
    device_id: str
    endpoint: str = ""

    def __post_init__(self) -> None:
        if not valid_profile_id(self.profile_id) or not valid_token(self.provider):
            raise ValueError("invalid device account or provider identity")
        for value in (self.location_id, self.asset_id, self.device_id, self.endpoint):
            if not literal_text(value, 128):
                raise ValueError("invalid device identity component")
        if not self.device_id:
            raise ValueError("device identity is required")

    @property
    def key(self) -> str:
        return _digest(
            (
                "halo-device-v1",
                self.profile_id,
                self.provider,
                self.location_id,
                self.asset_id,
                self.device_id,
                self.endpoint,
            )
        )

    @property
    def location_key(self) -> str:
        return _digest(("halo-location-v1", self.profile_id, self.location_id))

    @property
    def source_key(self) -> str:
        return _digest(
            ("halo-source-v1", self.profile_id, self.provider, self.location_id, self.asset_id)
        )


@dataclass(frozen=True)
class DeviceObservation:
    name: str
    value: bool | int | float | str | None
    observed_at: float | None
    status: str = "current"
    source: str = "rest"

    def __post_init__(self) -> None:
        if not valid_token(self.name) or not valid_token(self.source):
            raise ValueError("invalid observation label")
        if not isinstance(self.status, str) or self.status not in OBSERVATION_STATUS:
            raise ValueError("invalid observation freshness")
        if self.observed_at is not None and not valid_timestamp(self.observed_at):
            raise ValueError("invalid observation time")
        if self.status == "unknown" and self.value is not None:
            raise ValueError("unknown observation cannot assert a value")
        if self.status == "current" and (self.value is None or self.observed_at is None):
            raise ValueError("current observation requires a value and receipt time")
        if self.value is not None and type(self.value) not in (bool, int, float, str):
            raise ValueError("observation must be a bounded scalar")
        if type(self.value) in (int, float) and (
            not -(2**53) < self.value < 2**53 or not math.isfinite(self.value)
        ):
            raise ValueError("observation number is outside the safe range")
        if isinstance(self.value, str) and not literal_text(self.value, 128):
            raise ValueError("observation text is invalid")
        if self.value is not None:
            if self.name in _BOOLEAN_OBSERVATIONS and type(self.value) is not bool:
                raise ValueError("boolean observation requires explicit boolean evidence")
            if self.name in _ENUM_OBSERVATIONS and not isinstance(self.value, str):
                raise ValueError("device state requires a normalized state label")
            if self.name == "battery_percent" and (
                type(self.value) is not int or not 0 <= self.value <= 100
            ):
                raise ValueError("battery percentage is outside the valid range")
            if self.name == "keypad_brightness" and (
                type(self.value) is not int or not 0 <= self.value <= 100
            ):
                raise ValueError("keypad brightness is outside the valid range")
            if self.name == "temperature_celsius" and type(self.value) not in (int, float):
                raise ValueError("temperature requires a measured numeric value")

    def as_dict(self, *, now: float, max_age: float, stale: bool = False) -> dict[str, Any]:
        if not valid_timestamp(now) or not valid_timestamp(max_age) or max_age == 0:
            raise ValueError("invalid freshness clock")
        status = self.status
        if status == "current" and (
            stale
            or self.observed_at is None
            or self.observed_at > now
            or now - self.observed_at > max_age
        ):
            status = "stale"
        return {
            "value": self.value if status == "current" else None,
            "last_known_value": self.value if status == "stale" else None,
            "status": status,
            "observed_at": self.observed_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class DeviceRecord:
    identity: DeviceIdentity
    account_generation: int
    provider_generation: int
    name: str
    family: str
    kind: str
    model: str = ""
    capabilities: tuple[str, ...] = ()
    capability_evidence: tuple[str, ...] = ()
    observations: tuple[DeviceObservation, ...] = ()
    source_health: str = "unknown"
    unavailable_reason: str | None = None
    location_name: str = ""
    supported: bool = False
    unsupported_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, DeviceIdentity):
            raise ValueError("invalid device identity")
        if not valid_generation(self.account_generation) or not valid_generation(
            self.provider_generation
        ):
            raise ValueError("invalid device generation")
        for field, limit in (
            ("name", 256),
            ("family", 128),
            ("kind", 128),
            ("model", 128),
            ("location_name", 256),
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or len(value) > 4096:
                raise ValueError("invalid device display metadata")
            object.__setattr__(self, field, safe_display_text(value, limit))
        for field in ("unavailable_reason", "unsupported_reason"):
            value = getattr(self, field)
            if value is not None:
                if not isinstance(value, str) or len(value) > 4096:
                    raise ValueError("invalid device reason")
                object.__setattr__(self, field, safe_display_text(value, 256))
        for values in (self.capabilities, self.capability_evidence):
            if (
                not isinstance(values, tuple)
                or len(values) > MAX_CAPABILITIES
                or any(not valid_token(value) for value in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError("invalid device capabilities or evidence")
        if self.capabilities and not self.capability_evidence:
            raise ValueError("device capabilities require evidence")
        if (
            not isinstance(self.observations, tuple)
            or len(self.observations) > MAX_OBSERVATIONS
            or any(not isinstance(value, DeviceObservation) for value in self.observations)
            or len({value.name for value in self.observations}) != len(self.observations)
        ):
            raise ValueError("invalid or duplicate device observations")
        if (
            not isinstance(self.source_health, str)
            or self.source_health not in SOURCE_HEALTH
            or type(self.supported) is not bool
        ):
            raise ValueError("invalid device availability")

    @property
    def key(self) -> str:
        return self.identity.key
