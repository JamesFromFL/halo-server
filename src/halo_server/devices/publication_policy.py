"""Explicit bridge publication and deny-first reset authority in one private file."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import UUID

from halo_server.account_scope import valid_profile_id
from halo_server.homekit.pairing_maintenance import manifest_revision
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

PHASES = ("denied", "quiesced", "cleanup_started", "completed")
_MARKER = b"halo-device-publication-v1\n"


class PublicationError(PrivateJsonError):
    pass


class PublicationConflict(PublicationError):
    pass


@dataclass(frozen=True)
class BridgeReset:
    operation_id: str
    profile_id: str
    accessory_id: str
    username: str
    pairing_revision: str
    manifest: tuple[tuple[str, str | None], ...]
    phase: str = "denied"

    def __post_init__(self):
        if (
            any(
                not valid_profile_id(item)
                for item in (self.operation_id, self.profile_id, self.accessory_id)
            )
            or not isinstance(self.username, str)
            or re.fullmatch(r"[0-9A-F][26AE](?::[0-9A-F]{2}){5}", self.username) is None
            or self.phase not in PHASES
            or not isinstance(self.manifest, tuple)
            or len(self.manifest) != 3
        ):
            raise PublicationError("Bridge reset identity is invalid")
        try:
            if (
                len(dict(self.manifest)) != 3
                or manifest_revision(dict(self.manifest)) != self.pairing_revision
            ):
                raise ValueError
        except (TypeError, ValueError, RuntimeError) as error:
            raise PublicationError("Bridge reset manifest is invalid") from error

    def as_dict(self):
        return asdict(self) | {"manifest": dict(self.manifest)}


@dataclass(frozen=True)
class PublicationProfile:
    profile_id: str
    accessory_id: str
    enabled: bool = False
    reset: BridgeReset | None = None

    def __post_init__(self):
        if (
            not valid_profile_id(self.profile_id)
            or not valid_profile_id(self.accessory_id)
            or UUID(self.accessory_id).version != 4
            or type(self.enabled) is not bool
        ):
            raise PublicationError("Bridge publication identity is invalid")
        if self.reset is not None and (
            not isinstance(self.reset, BridgeReset)
            or (self.reset.profile_id, self.reset.accessory_id)
            != (self.profile_id, self.accessory_id)
            or (self.enabled and self.reset.phase != "completed")
        ):
            raise PublicationError("Bridge reset must remain unpublished")

    def as_dict(self):
        return {
            "profile_id": self.profile_id,
            "accessory_id": self.accessory_id,
            "enabled": self.enabled,
            "reset": self.reset.as_dict() if self.reset else None,
        }


@dataclass(frozen=True)
class PublicationPolicy:
    version: int = 1
    revision: int = 0
    profiles: tuple[PublicationProfile, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or not isinstance(self.profiles, tuple)
            or len(self.profiles) > 16
            or any(not isinstance(row, PublicationProfile) for row in self.profiles)
            or len({row.profile_id for row in self.profiles}) != len(self.profiles)
            or len({row.accessory_id for row in self.profiles}) != len(self.profiles)
        ):
            raise PublicationError("Bridge publication policy is invalid")
        resets = [row.reset.operation_id for row in self.profiles if row.reset]
        if len(set(resets)) != len(resets):
            raise PublicationError("Bridge reset operation identity is duplicated")

    def as_dict(self):
        return {
            "version": self.version,
            "revision": self.revision,
            "profiles": [row.as_dict() for row in self.profiles],
        }

    def profile(self, profile_id):
        return next((row for row in self.profiles if row.profile_id == profile_id), None)


def _decode(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "revision", "profiles"}
        or not isinstance(value["profiles"], list)
    ):
        raise PublicationError("Bridge publication policy format is invalid")
    rows = []
    for row in value["profiles"]:
        if not isinstance(row, dict) or set(row) != {
            "profile_id",
            "accessory_id",
            "enabled",
            "reset",
        }:
            raise PublicationError("Bridge publication profile format is invalid")
        reset = row["reset"]
        if reset is not None:
            if (
                not isinstance(reset, dict)
                or set(reset) != set(BridgeReset.__dataclass_fields__)
                or not isinstance(reset["manifest"], dict)
            ):
                raise PublicationError("Bridge reset format is invalid")
            reset = BridgeReset(**(reset | {"manifest": tuple(sorted(reset["manifest"].items()))}))
        rows.append(PublicationProfile(**(row | {"reset": reset})))
    return PublicationPolicy(value["version"], value["revision"], tuple(rows))


class PublicationStore(PrivateJsonStore):
    def __init__(self, path: Path):
        super().__init__(
            path,
            snapshot_type=PublicationPolicy,
            decode=_decode,
            default=PublicationPolicy,
            max_bytes=64 * 1024,
            label="Bridge publication policy",
            noun="bridge publication policy",
            error_type=PublicationError,
            conflict_type=PublicationConflict,
        )
        self.marker = self.path.with_name(f".{self.path.name}.enrolled")

    def load(self):
        if (
            not self.path.exists()
            and not self.path.is_symlink()
            and not self.marker.exists()
            and not self.marker.is_symlink()
        ):
            return PublicationPolicy()
        return super().load()

    def _marked(self, directory):
        try:
            descriptor = os.open(
                self.marker.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return False
        try:
            self._validate_file(descriptor, "Bridge publication enrollment")
            if os.read(descriptor, len(_MARKER) + 1) != _MARKER:
                raise PublicationError("Bridge publication enrollment is invalid")
            return True
        finally:
            os.close(descriptor)

    def _read_locked(self, directory):
        current = super()._read_locked(directory)
        if bool(current.profiles) != self._marked(directory):
            raise PublicationError("Enrolled bridge publication state is missing")
        return current

    def _write_locked(self, directory, snapshot):
        if snapshot.profiles and not self._marked(directory):
            try:
                descriptor = os.open(
                    self.marker.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(descriptor, "wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(_MARKER)
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(directory)
            except OSError as error:
                raise PublicationError(
                    "Bridge publication enrollment may have started", possibly_saved=True
                ) from error
        super()._write_locked(directory, snapshot)

    def _save_locked(self, directory, current, snapshot):
        for old in current.profiles:
            new = snapshot.profile(old.profile_id)
            if new is None or new.accessory_id != old.accessory_id:
                raise PublicationError("Enrolled publication identity cannot be replaced")
            if old.reset is not None:
                if new.reset is None:
                    raise PublicationError("Reset history cannot be removed")
                if new.reset.operation_id == old.reset.operation_id:
                    if (
                        replace(new.reset, phase=old.reset.phase) != old.reset
                        or not 0
                        <= PHASES.index(new.reset.phase) - PHASES.index(old.reset.phase)
                        <= 1
                    ):
                        raise PublicationError("Reset identity or phase changed")
                elif old.reset.phase != "completed" or new.reset.phase != "denied":
                    raise PublicationError("Pending reset cannot be replaced")
            elif new.reset is not None and new.reset.phase != "denied":
                raise PublicationError("Reset must begin denied")
        return super()._save_locked(directory, current, snapshot)

    def set_enabled(self, profile_id, accessory_id, enabled, *, expected_revision):
        proposed = PublicationProfile(profile_id, accessory_id, enabled)

        def change(current):
            old = current.profile(profile_id)
            if old and old.accessory_id != accessory_id:
                raise PublicationConflict("Bridge publication identity changed")
            if old and old.reset and old.reset.phase != "completed" and enabled:
                raise PublicationConflict("Bridge pairing reset is pending")
            row = replace(proposed, reset=old.reset if old else None)
            return replace(
                current,
                profiles=tuple(item for item in current.profiles if item.profile_id != profile_id)
                + (row,),
            )

        return self.update(change, expected_revision=expected_revision)

    def begin_reset(self, reset):
        if not isinstance(reset, BridgeReset) or reset.phase != "denied":
            raise PublicationError("Bridge reset must begin denied")

        def change(current):
            old = current.profile(reset.profile_id)
            if old is None or old.accessory_id != reset.accessory_id:
                raise PublicationConflict("Bridge reset publication identity changed")
            if old.reset and old.reset.operation_id == reset.operation_id:
                if replace(old.reset, phase="denied") != reset:
                    raise PublicationConflict("Bridge reset operation identity changed")
                return current
            if old.reset and old.reset.phase != "completed":
                raise PublicationConflict("Bridge reset is already pending")
            row = replace(old, enabled=False, reset=reset)
            return replace(
                current,
                profiles=tuple(
                    row if item.profile_id == row.profile_id else item for item in current.profiles
                ),
            )

        return self.update(change)

    def advance(self, profile_id, operation_id, phase):
        if phase not in PHASES:
            raise PublicationError("Bridge reset phase is invalid")

        def change(current):
            row = current.profile(profile_id)
            if row is None or row.reset is None or row.reset.operation_id != operation_id:
                raise PublicationConflict("Bridge reset operation changed")
            if not 0 <= PHASES.index(phase) - PHASES.index(row.reset.phase) <= 1:
                raise PublicationError("Bridge reset phase cannot be skipped or reversed")
            updated = replace(row, enabled=False, reset=replace(row.reset, phase=phase))
            return replace(
                current,
                profiles=tuple(updated if item is row else item for item in current.profiles),
            )

        return self.update(change)
