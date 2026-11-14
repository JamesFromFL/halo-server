"""Private per-device permission and finite On duration for physical Beams lights.

Preferences never actuate a light. They grant no Ring-side role or capability.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from halo_server.account_scope import valid_profile_id
from halo_server.devices.alarm_policy import valid_device_id
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

ON_DURATIONS = frozenset({30, 60, 300, 900})
MAX_PREFERENCES = 512
BEAMS_TYPES = frozenset({"switch.beams", "switch.multilevel.beams", "switch.transformer.beams"})


def qualified_light(record):
    return bool(
        record.kind == "beams-light"
        and record.family == "hub"
        and record.supported
        and record.identity.provider == "ring-hub"
        and record.model in BEAMS_TYPES
        and "switch" in record.capabilities
        and "ring-hub-device-schema" in record.capability_evidence
    )


class LightPolicyError(PrivateJsonError):
    pass


class LightPolicyConflict(LightPolicyError):
    pass


@dataclass(frozen=True, order=True)
class LightPreference:
    profile_id: str
    device_id: str
    enabled: bool = False
    on_duration_seconds: int = 60

    def __post_init__(self):
        if (
            not valid_profile_id(self.profile_id)
            or not valid_device_id(self.device_id)
            or type(self.enabled) is not bool
            or type(self.on_duration_seconds) is not int
            or self.on_duration_seconds not in ON_DURATIONS
        ):
            raise LightPolicyError("Light preference is invalid")

    def as_dict(self):
        return {
            "profile_id": self.profile_id,
            "device_id": self.device_id,
            "enabled": self.enabled,
            "on_duration_seconds": self.on_duration_seconds,
        }


@dataclass(frozen=True)
class LightPolicy:
    version: int = 1
    revision: int = 0
    preferences: tuple[LightPreference, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or not isinstance(self.preferences, tuple)
            or len(self.preferences) > MAX_PREFERENCES
            or any(not isinstance(item, LightPreference) for item in self.preferences)
            or tuple(sorted(self.preferences)) != self.preferences
            or len({(item.profile_id, item.device_id) for item in self.preferences})
            != len(self.preferences)
        ):
            raise LightPolicyError("Light control policy is invalid")

    def preference(self, profile_id, device_id):
        return next(
            (
                item
                for item in self.preferences
                if (item.profile_id, item.device_id) == (profile_id, device_id)
            ),
            LightPreference(profile_id, device_id),
        )

    def allows(self, profile_id, device_id):
        return self.preference(profile_id, device_id).enabled

    def as_dict(self):
        return {
            "version": self.version,
            "revision": self.revision,
            "preferences": [item.as_dict() for item in self.preferences],
        }


def _decode(value):
    try:
        if set(value) != {"version", "revision", "preferences"} or not isinstance(
            value["preferences"], list
        ):
            raise ValueError
        return LightPolicy(
            value["version"],
            value["revision"],
            tuple(LightPreference(**row) for row in value["preferences"]),
        )
    except (TypeError, ValueError) as error:
        raise LightPolicyError("Light control policy has an invalid format") from error


class LightPolicyStore(PrivateJsonStore[LightPolicy]):
    def __init__(self, path):
        super().__init__(
            path,
            snapshot_type=LightPolicy,
            decode=_decode,
            default=LightPolicy,
            max_bytes=131072,
            label="Light control policy",
            noun="light policy",
            error_type=LightPolicyError,
            conflict_type=LightPolicyConflict,
        )

    def load(self):
        try:
            self.path.lstat()
        except FileNotFoundError:
            return LightPolicy()
        except OSError as error:
            raise LightPolicyError("Cannot access light control policy") from error
        return super().load()

    def configure(self, profile_id, device_id, enabled, on_duration_seconds, *, expected_revision):
        preference = LightPreference(profile_id, device_id, enabled, on_duration_seconds)

        def change(current):
            values = [
                item
                for item in current.preferences
                if (item.profile_id, item.device_id) != (profile_id, device_id)
            ]
            # The default consumes no persistent slot; disabled customized
            # durations survive until explicitly returned to the default.
            if enabled or on_duration_seconds != 60:
                values.append(preference)
            return replace(current, preferences=tuple(sorted(values)))

        return self.update(change, expected_revision=expected_revision)
