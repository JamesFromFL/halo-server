"""Private, profile-scoped camera selection and stable HomeKit identities.

Discovery never selects cameras. Missing devices retain their identity and
selection, while runtime resolution always uses the current account inventory.
HAP secrets belong to Node's separate pairing store, never to this registry.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from halo_server.account_scope import valid_profile_id
from halo_server.homekit.models import NativeCamera, NativeInventory
from halo_server.homekit.text import literal_text
from halo_server.media.formats import MAX_NATIVE_CAMERAS
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

REGISTRY_VERSION = 1
MAX_REGISTRY_BYTES = 256 * 1024
_USERNAME = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\Z")


class RegistryError(PrivateJsonError):
    """The stable camera registry is invalid or could not be safely committed."""


class RegistryConflictError(RegistryError):
    """Selection or discovery used an obsolete registry revision."""


def _valid_accessory_uuid(value: object) -> bool:
    if not valid_profile_id(value):
        return False
    return UUID(value).version == 4


def _valid_username(value: object) -> bool:
    return (
        isinstance(value, str)
        and _USERNAME.fullmatch(value) is not None
        and int(value[:2], 16) & 3 == 2
    )


@dataclass(frozen=True)
class CameraIdentity:
    profile_id: str
    camera_id: str
    accessory_uuid: str
    username: str
    doorbell: bool = False
    selected: bool = False

    def __post_init__(self) -> None:
        if not valid_profile_id(self.profile_id):
            raise RegistryError("Registry profile identity is invalid")
        if not literal_text(self.camera_id, 128, empty=False):
            raise RegistryError("Registry camera identity is invalid")
        if not _valid_accessory_uuid(self.accessory_uuid):
            raise RegistryError("Registry accessory UUID is invalid")
        if not _valid_username(self.username):
            raise RegistryError("Registry HomeKit username is invalid")
        if type(self.doorbell) is not bool or type(self.selected) is not bool:
            raise RegistryError("Registry camera settings must be booleans")

    @property
    def key(self) -> tuple[str, str]:
        return self.profile_id, self.camera_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "camera_id": self.camera_id,
            "accessory_uuid": self.accessory_uuid,
            "username": self.username,
            "doorbell": self.doorbell,
            "selected": self.selected,
        }


@dataclass(frozen=True)
class RegisteredCamera:
    identity: CameraIdentity
    camera: NativeCamera

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CameraIdentity) or not isinstance(
            self.camera, NativeCamera
        ):
            raise RegistryError("Resolved registry camera is invalid")
        if self.identity.key != (self.camera.scope.profile_id, self.camera.scope.camera_id):
            raise RegistryError("Resolved registry camera has mismatched authority")


@dataclass(frozen=True)
class CameraRegistry:
    version: int = REGISTRY_VERSION
    revision: int = 0
    installation_id: str | None = None
    entries: tuple[CameraIdentity, ...] = ()

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != REGISTRY_VERSION:
            raise RegistryError("HomeKit registry version is unsupported")
        if type(self.revision) is not int or not 0 <= self.revision <= MAX_REVISION:
            raise RegistryError("HomeKit registry revision is invalid")
        if self.installation_id is not None and not _valid_accessory_uuid(self.installation_id):
            raise RegistryError("HomeKit installation identity is invalid")
        if (
            not isinstance(self.entries, tuple)
            or len(self.entries) > MAX_NATIVE_CAMERAS
            or any(not isinstance(entry, CameraIdentity) for entry in self.entries)
        ):
            raise RegistryError("HomeKit registry camera count or entries are invalid")
        if self.entries and self.installation_id is None:
            raise RegistryError("HomeKit registry cameras require an installation identity")
        for values in (
            [entry.key for entry in self.entries],
            [entry.accessory_uuid for entry in self.entries],
            [entry.username for entry in self.entries],
        ):
            if len(set(values)) != len(values):
                raise RegistryError("HomeKit registry contains duplicate camera identities")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "revision": self.revision,
            "installation_id": self.installation_id,
            "entries": [entry.as_dict() for entry in self.entries],
        }

    def selected(self, profile_id: str) -> tuple[CameraIdentity, ...]:
        if not valid_profile_id(profile_id):
            raise RegistryError("Registry profile identity is invalid")
        return tuple(
            entry for entry in self.entries if entry.profile_id == profile_id and entry.selected
        )

    def resolve(self, inventory: NativeInventory) -> tuple[RegisteredCamera, ...]:
        if not isinstance(inventory, NativeInventory):
            raise RegistryError("Registry resolution requires a native inventory")
        if inventory.profile_id is None:
            return ()
        known = {entry.camera_id: entry for entry in self.selected(inventory.profile_id)}
        return tuple(
            RegisteredCamera(known[camera.scope.camera_id], camera)
            for camera in inventory.cameras
            if camera.scope.camera_id in known
        )


def _decode_registry(payload: dict[str, Any]) -> CameraRegistry:
    if set(payload) != {"version", "revision", "installation_id", "entries"}:
        raise RegistryError("HomeKit registry has an invalid format")
    raw_entries = payload["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_NATIVE_CAMERAS:
        raise RegistryError("HomeKit registry camera count or entries are invalid")
    entries = []
    fields = {"profile_id", "camera_id", "accessory_uuid", "username", "doorbell", "selected"}
    for item in raw_entries:
        if not isinstance(item, dict) or set(item) != fields:
            raise RegistryError("HomeKit registry camera has an invalid format")
        entries.append(CameraIdentity(**item))
    return CameraRegistry(**(payload | {"entries": tuple(entries)}))


class RegistryStore(PrivateJsonStore[CameraRegistry]):
    """The sole Python writer for camera identities; Node receives snapshots."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(
            path,
            snapshot_type=CameraRegistry,
            decode=_decode_registry,
            default=CameraRegistry,
            max_bytes=MAX_REGISTRY_BYTES,
            label="HomeKit registry",
            noun="registry",
            error_type=RegistryError,
            conflict_type=RegistryConflictError,
        )

    def _save_locked(
        self, directory: int, current: CameraRegistry, snapshot: CameraRegistry
    ) -> CameraRegistry:
        if (
            current.installation_id is not None
            and snapshot.installation_id != current.installation_id
        ):
            raise RegistryError("HomeKit installation identity cannot be replaced implicitly")
        replacements = {entry.key: entry for entry in snapshot.entries}
        for existing in current.entries:
            replacement = replacements.get(existing.key)
            if (
                replacement is None
                or replace(existing, selected=replacement.selected) != replacement
            ):
                raise RegistryError(
                    "HomeKit camera identities cannot be removed or replaced implicitly"
                )
        return super()._save_locked(directory, current, snapshot)

    def reconcile(
        self, inventory: NativeInventory, *, expected_revision: int | None = None
    ) -> CameraRegistry:
        if not isinstance(inventory, NativeInventory):
            raise RegistryError("Registry enrollment requires a native inventory")

        def enroll(current: CameraRegistry) -> CameraRegistry:
            if not inventory.complete or inventory.profile_id is None:
                return current
            known = {entry.key for entry in current.entries}
            additions = [
                camera
                for camera in inventory.cameras
                if (inventory.profile_id, camera.scope.camera_id) not in known
            ]
            if not additions:
                return current
            if len(current.entries) + len(additions) > MAX_NATIVE_CAMERAS:
                raise RegistryError(
                    "HomeKit registry capacity reached; retained identities are full"
                )
            uuids = {entry.accessory_uuid for entry in current.entries}
            usernames = {entry.username for entry in current.entries}
            entries = list(current.entries)
            for camera in additions:
                accessory_uuid = _new_unique_uuid(uuids)
                username = _new_unique_username(usernames)
                entries.append(
                    CameraIdentity(
                        inventory.profile_id,
                        camera.scope.camera_id,
                        accessory_uuid,
                        username,
                        doorbell=camera.capabilities.doorbell,
                    )
                )
            return replace(
                current,
                installation_id=current.installation_id or str(uuid4()),
                entries=tuple(sorted(entries, key=lambda item: item.key)),
            )

        return self.update(enroll, expected_revision=expected_revision)

    def set_selected(
        self,
        profile_id: str,
        camera_ids: Iterable[str],
        *,
        expected_revision: int,
    ) -> CameraRegistry:
        if not valid_profile_id(profile_id):
            raise RegistryError("Registry profile identity is invalid")
        if isinstance(camera_ids, (str, bytes)):
            raise RegistryError("Camera selection must be a collection of API identities")
        requested = []
        try:
            identities = iter(camera_ids)
        except TypeError as exc:
            raise RegistryError("Camera selection must be a collection of API identities") from exc
        for camera_id in identities:
            if len(requested) == MAX_NATIVE_CAMERAS:
                raise RegistryError("Camera selection exceeds the camera limit")
            if not literal_text(camera_id, 128, empty=False):
                raise RegistryError("Selected camera identity is invalid")
            requested.append(camera_id)
        selected = frozenset(requested)
        if len(selected) != len(requested):
            raise RegistryError("Camera selection contains duplicate identities")

        def choose(current: CameraRegistry) -> CameraRegistry:
            known = {entry.camera_id for entry in current.entries if entry.profile_id == profile_id}
            if not selected.issubset(known):
                raise RegistryError("Camera selection contains an unknown profile camera")
            return replace(
                current,
                entries=tuple(
                    replace(entry, selected=entry.camera_id in selected)
                    if entry.profile_id == profile_id
                    else entry
                    for entry in current.entries
                ),
            )

        return self.update(choose, expected_revision=expected_revision)


def _new_unique_uuid(used: set[str]) -> str:
    for _ in range(32):
        value = str(uuid4())
        if value not in used:
            used.add(value)
            return value
    raise RegistryError("Cannot allocate a unique HomeKit accessory identity")


def _new_unique_username(used: set[str]) -> str:
    for _ in range(32):
        raw = bytearray(secrets.token_bytes(6))
        raw[0] = (raw[0] & 0xFC) | 0x02
        value = ":".join(f"{byte:02X}" for byte in raw)
        if value not in used:
            used.add(value)
            return value
    raise RegistryError("Cannot allocate a unique HomeKit username")
