"""Private per-device Chime controls and separate sound-test opt-in.

Preferences never actuate a chime. They grant no Ring-side role or capability.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from halo_server.account_scope import valid_profile_id
from halo_server.devices.alarm_policy import valid_device_id
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

MAX_PREFERENCES = 512
CHIME_TYPES = frozenset({"chime", "chime_v2", "chime_pro", "chime_pro_v2"})


def qualified_chime(record):
    return bool(
        record.kind == "chime"
        and record.family == "chimes"
        and record.supported
        and record.identity.provider == "ring-rest"
        and record.model in CHIME_TYPES
        and "ring-rest-schema" in record.capability_evidence
    )


class ChimePolicyError(PrivateJsonError):
    pass


class ChimePolicyConflict(ChimePolicyError):
    pass


@dataclass(frozen=True, order=True)
class ChimePreference:
    profile_id: str
    device_id: str
    enabled: bool = False
    sound_tests_enabled: bool = False

    def __post_init__(self):
        if (
            not valid_profile_id(self.profile_id)
            or not valid_device_id(self.device_id)
            or type(self.enabled) is not bool
            or type(self.sound_tests_enabled) is not bool
            or self.sound_tests_enabled
            and not self.enabled
        ):
            raise ChimePolicyError("Chime preference is invalid")

    def as_dict(self):
        return {
            "profile_id": self.profile_id,
            "device_id": self.device_id,
            "enabled": self.enabled,
            "sound_tests_enabled": self.sound_tests_enabled,
        }


@dataclass(frozen=True)
class ChimePolicy:
    version: int = 1
    revision: int = 0
    preferences: tuple[ChimePreference, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or not isinstance(self.preferences, tuple)
            or len(self.preferences) > MAX_PREFERENCES
            or any(not isinstance(item, ChimePreference) for item in self.preferences)
            or tuple(sorted(self.preferences)) != self.preferences
            or len({(item.profile_id, item.device_id) for item in self.preferences})
            != len(self.preferences)
        ):
            raise ChimePolicyError("Chime control policy is invalid")

    def preference(self, profile_id, device_id):
        return next(
            (
                item
                for item in self.preferences
                if (item.profile_id, item.device_id) == (profile_id, device_id)
            ),
            ChimePreference(profile_id, device_id),
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
        return ChimePolicy(
            value["version"],
            value["revision"],
            tuple(ChimePreference(**row) for row in value["preferences"]),
        )
    except (TypeError, ValueError) as error:
        raise ChimePolicyError("Chime control policy has an invalid format") from error


class ChimePolicyStore(PrivateJsonStore[ChimePolicy]):
    def __init__(self, path):
        super().__init__(
            path,
            snapshot_type=ChimePolicy,
            decode=_decode,
            default=ChimePolicy,
            max_bytes=131072,
            label="Chime control policy",
            noun="chime policy",
            error_type=ChimePolicyError,
            conflict_type=ChimePolicyConflict,
        )

    def load(self):
        try:
            self.path.lstat()
        except FileNotFoundError:
            return ChimePolicy()
        except OSError as error:
            raise ChimePolicyError("Cannot access chime control policy") from error
        return super().load()

    def configure(self, profile_id, device_id, enabled, sound_tests_enabled, *, expected_revision):
        preference = ChimePreference(profile_id, device_id, enabled, sound_tests_enabled)

        def change(current):
            values = [
                item
                for item in current.preferences
                if (item.profile_id, item.device_id) != (profile_id, device_id)
            ]
            if enabled:
                values.append(preference)
            return replace(current, preferences=tuple(sorted(values)))

        return self.update(change, expected_revision=expected_revision)
