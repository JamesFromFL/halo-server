"""Explicit per-account/location grants for normal alarm modes only.

These preferences grant Halo permission to attempt a normal mode change. They do
not grant Ring account authority or a subscription entitlement. Missing or damaged
state denies control; no startup action is stored here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from halo_server.account_scope import valid_profile_id
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

MAX_GRANTS = 128


def valid_device_id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


class AlarmPolicyError(PrivateJsonError):
    pass


class AlarmPolicyConflict(AlarmPolicyError):
    pass


@dataclass(frozen=True, order=True)
class AlarmGrant:
    profile_id: str
    location_id: str

    def __post_init__(self):
        if not valid_profile_id(self.profile_id) or not valid_device_id(self.location_id):
            raise AlarmPolicyError("Alarm control identity is invalid")

    def as_dict(self):
        return {"profile_id": self.profile_id, "location_id": self.location_id}


@dataclass(frozen=True)
class AlarmPolicy:
    version: int = 1
    revision: int = 0
    grants: tuple[AlarmGrant, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or not isinstance(self.grants, tuple)
            or len(self.grants) > MAX_GRANTS
            or any(not isinstance(item, AlarmGrant) for item in self.grants)
            or tuple(sorted(set(self.grants))) != self.grants
        ):
            raise AlarmPolicyError("Alarm control policy is invalid")

    def allows(self, profile_id, location_id):
        return AlarmGrant(profile_id, location_id) in self.grants

    def as_dict(self):
        return {
            "version": self.version,
            "revision": self.revision,
            "grants": [item.as_dict() for item in self.grants],
        }


def _decode(value):
    try:
        if set(value) != {"version", "revision", "grants"} or not isinstance(value["grants"], list):
            raise ValueError
        return AlarmPolicy(
            value["version"], value["revision"], tuple(AlarmGrant(**row) for row in value["grants"])
        )
    except (TypeError, ValueError) as error:
        raise AlarmPolicyError("Alarm control policy has an invalid format") from error


class AlarmPolicyStore(PrivateJsonStore[AlarmPolicy]):
    def __init__(self, path):
        super().__init__(
            path,
            snapshot_type=AlarmPolicy,
            decode=_decode,
            default=AlarmPolicy,
            max_bytes=32768,
            label="Alarm control policy",
            noun="alarm policy",
            error_type=AlarmPolicyError,
            conflict_type=AlarmPolicyConflict,
        )

    def load(self):
        try:
            self.path.lstat()
        except FileNotFoundError:
            return AlarmPolicy()
        except OSError as error:
            raise AlarmPolicyError("Cannot access alarm control policy") from error
        return super().load()

    def configure(self, profile_id, location_id, enabled, *, expected_revision):
        grant = AlarmGrant(profile_id, location_id)
        if type(enabled) is not bool:
            raise AlarmPolicyError("Alarm control preference must be boolean")

        def change(current):
            grants = set(current.grants)
            if enabled:
                grants.add(grant)
            else:
                grants.discard(grant)
            return replace(current, grants=tuple(sorted(grants)))

        return self.update(change, expected_revision=expected_revision)
