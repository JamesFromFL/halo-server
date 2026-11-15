"""Private keypad control grants and persistent local brightness locks.

Saving preferences never changes a Ring device. Locks apply to Halo commands only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from halo_server.account_scope import valid_profile_id
from halo_server.devices.alarm_policy import valid_device_id
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

MAX_PREFERENCES = 512


def qualified_keypad(record):
    return bool(
        record.kind == "keypad"
        and record.family == "hub"
        and record.supported
        and record.identity.provider == "ring-hub"
        and record.model == "security-keypad"
        and "keypad-brightness" in record.capabilities
        and "ring-hub-device-schema" in record.capability_evidence
    )


class KeypadPolicyError(PrivateJsonError):
    pass


class KeypadPolicyConflict(KeypadPolicyError):
    pass


@dataclass(frozen=True, order=True)
class KeypadPreference:
    profile_id: str
    device_id: str
    enabled: bool = False
    locked: bool = True

    def __post_init__(self):
        if (
            not valid_profile_id(self.profile_id)
            or not valid_device_id(self.device_id)
            or type(self.enabled) is not bool
            or type(self.locked) is not bool
        ):
            raise KeypadPolicyError("Keypad preference is invalid")

    def as_dict(self):
        return {
            "profile_id": self.profile_id,
            "device_id": self.device_id,
            "enabled": self.enabled,
            "locked": self.locked,
        }


@dataclass(frozen=True)
class KeypadPolicy:
    version: int = 1
    revision: int = 0
    preferences: tuple[KeypadPreference, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or not isinstance(self.preferences, tuple)
            or len(self.preferences) > MAX_PREFERENCES
            or any(not isinstance(item, KeypadPreference) for item in self.preferences)
            or tuple(sorted(self.preferences)) != self.preferences
            or len({(item.profile_id, item.device_id) for item in self.preferences})
            != len(self.preferences)
        ):
            raise KeypadPolicyError("Keypad control policy is invalid")

    def preference(self, profile_id, device_id):
        return next(
            (
                item
                for item in self.preferences
                if (item.profile_id, item.device_id) == (profile_id, device_id)
            ),
            KeypadPreference(profile_id, device_id),
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
        return KeypadPolicy(
            value["version"],
            value["revision"],
            tuple(KeypadPreference(**row) for row in value["preferences"]),
        )
    except (TypeError, ValueError) as error:
        raise KeypadPolicyError("Keypad control policy has an invalid format") from error


class KeypadPolicyStore(PrivateJsonStore[KeypadPolicy]):
    def __init__(self, path):
        super().__init__(
            path,
            snapshot_type=KeypadPolicy,
            decode=_decode,
            default=KeypadPolicy,
            max_bytes=131072,
            label="Keypad control policy",
            noun="keypad policy",
            error_type=KeypadPolicyError,
            conflict_type=KeypadPolicyConflict,
        )

    def load(self):
        try:
            self.path.lstat()
        except FileNotFoundError:
            return KeypadPolicy()
        except OSError as error:
            raise KeypadPolicyError("Cannot access keypad control policy") from error
        return super().load()

    def configure(self, profile_id, device_id, enabled, locked, *, expected_revision):
        preference = KeypadPreference(profile_id, device_id, enabled, locked)

        def change(current):
            values = [
                item
                for item in current.preferences
                if (item.profile_id, item.device_id) != (profile_id, device_id)
            ]
            # Preserve an explicitly unlocked preference even while disabled.
            if enabled or not locked:
                values.append(preference)
            return replace(current, preferences=tuple(sorted(values)))

        return self.update(change, expected_revision=expected_revision)
