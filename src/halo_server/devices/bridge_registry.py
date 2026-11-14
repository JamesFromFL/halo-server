"""Account-bound identities and import preferences for the non-camera bridge.

This registry cannot publish HomeKit accessories or issue Ring commands. Its v1
mapping allowlist covers sensors and alarm state. Camera identities and HAP secrets
remain in their separate stores. Missing devices never lose their identities.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from halo_server.account_scope import valid_profile_id
from halo_server.devices.models import DeviceIdentity, DeviceObservation, DeviceRecord
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

MAX_PROFILES = 16
MAX_RETAINED_DEVICES = 1024
MAX_BRIDGE_CHILDREN = 149
MAX_DEVICE_PREFERENCES = 512
MAX_REGISTRY_BYTES = 1024 * 1024
MAPPINGS = frozenset({"contact", "motion", "leak", "smoke", "carbon_monoxide", "security_system"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_KIND = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_USERNAME = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\Z")
_MARKER = b"halo-device-bridge-registry-v1\n"
_KIND_MAPPINGS = {
    "security-panel": (("mode",), ("security_system",)),
    "contact-sensor": (("contact",), ("contact",)),
    "retrofit-zone": (("contact",), ("contact",)),
    "motion-sensor": (("motion",), ("motion",)),
    "flood-sensor": (("flood",), ("leak",)),
    "flood-freeze-sensor": (("flood",), ("leak",)),
    "smoke-alarm": (("smoke",), ("smoke",)),
    "co-alarm": (("carbon-monoxide",), ("carbon_monoxide",)),
    "smoke-co-alarm": (("smoke", "carbon-monoxide"), ("carbon_monoxide", "smoke")),
    "smoke-co-listener": (("smoke", "carbon-monoxide"), ("carbon_monoxide", "smoke")),
}
_FILTERS = (
    "included_devices",
    "excluded_devices",
    "excluded_locations",
    "excluded_kinds",
    "excluded_mappings",
)


class BridgeRegistryError(PrivateJsonError):
    """Bridge state is invalid or could not be safely persisted."""


class BridgeRegistryConflictError(BridgeRegistryError):
    """A caller attempted to modify an obsolete registry revision."""


def _uuid(value: object) -> bool:
    return valid_profile_id(value) and UUID(value).version == 4


def _hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _kind(value: object) -> bool:
    return isinstance(value, str) and _KIND.fullmatch(value) is not None


def _tuple(values: object, limit: int, validator: Any) -> bool:
    return (
        isinstance(values, tuple)
        and len(values) <= limit
        and all(validator(value) for value in values)
        and len(set(values)) == len(values)
        and tuple(sorted(values)) == values
    )


@dataclass(frozen=True)
class BridgeProfile:
    profile_id: str
    accessory_uuid: str
    username: str
    enabled: bool = False
    auto_import: bool = False
    included_devices: tuple[str, ...] = ()
    excluded_devices: tuple[str, ...] = ()
    excluded_locations: tuple[str, ...] = ()
    excluded_kinds: tuple[str, ...] = ()
    excluded_mappings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not valid_profile_id(self.profile_id) or not _uuid(self.accessory_uuid):
            raise BridgeRegistryError("Bridge account or accessory identity is invalid")
        if (
            not isinstance(self.username, str)
            or _USERNAME.fullmatch(self.username) is None
            or int(self.username[:2], 16) & 3 != 2
        ):
            raise BridgeRegistryError("Bridge HomeKit username is invalid")
        if type(self.enabled) is not bool or type(self.auto_import) is not bool:
            raise BridgeRegistryError("Bridge preferences must be booleans")
        for name, limit, check in (
            ("included_devices", MAX_DEVICE_PREFERENCES, _hash),
            ("excluded_devices", MAX_DEVICE_PREFERENCES, _hash),
            ("excluded_locations", 128, _hash),
            ("excluded_kinds", 64, _kind),
            ("excluded_mappings", 6, lambda value: isinstance(value, str) and value in MAPPINGS),
        ):
            if not _tuple(getattr(self, name), limit, check):
                raise BridgeRegistryError("Bridge selection filters are invalid")
        if len(self.included_devices) + len(self.excluded_devices) > MAX_DEVICE_PREFERENCES or set(
            self.included_devices
        ).intersection(self.excluded_devices):
            raise BridgeRegistryError("Bridge device preferences overlap or exceed the limit")

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "accessory_uuid": self.accessory_uuid,
            "username": self.username,
            "enabled": self.enabled,
            "auto_import": self.auto_import,
            **{field: list(getattr(self, field)) for field in _FILTERS},
        }


@dataclass(frozen=True)
class BridgeDevice:
    profile_id: str
    device_id: str
    location_id: str
    provider: str
    kind: str
    accessory_uuid: str
    mappings: tuple[str, ...] = ()
    selected: bool = False
    qualified: bool = False

    def __post_init__(self) -> None:
        if (
            not valid_profile_id(self.profile_id)
            or not _hash(self.device_id)
            or not _hash(self.location_id)
            or not _uuid(self.accessory_uuid)
        ):
            raise BridgeRegistryError("Bridge device identity is invalid")
        if not _kind(self.provider) or not _kind(self.kind):
            raise BridgeRegistryError("Bridge device classification is invalid")
        if not _tuple(self.mappings, 5, lambda value: isinstance(value, str) and value in MAPPINGS):
            raise BridgeRegistryError("Bridge device mappings are invalid")
        allowed = _KIND_MAPPINGS.get(self.kind)
        if self.mappings and (
            self.provider != "ring-hub" or allowed is None or self.mappings != allowed[1]
        ):
            raise BridgeRegistryError("Bridge device mapping is outside the qualified allowlist")
        if (
            type(self.selected) is not bool
            or type(self.qualified) is not bool
            or (self.qualified and not self.mappings)
            or (self.selected and not self.qualified)
        ):
            raise BridgeRegistryError("Bridge device selection is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "device_id": self.device_id,
            "location_id": self.location_id,
            "provider": self.provider,
            "kind": self.kind,
            "accessory_uuid": self.accessory_uuid,
            "mappings": list(self.mappings),
            "selected": self.selected,
            "qualified": self.qualified,
        }


@dataclass(frozen=True)
class BridgeRegistry:
    version: int = 1
    revision: int = 0
    profiles: tuple[BridgeProfile, ...] = ()
    entries: tuple[BridgeDevice, ...] = ()

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise BridgeRegistryError("Bridge registry version is unsupported")
        if type(self.revision) is not int or not 0 <= self.revision <= MAX_REVISION:
            raise BridgeRegistryError("Bridge registry revision is invalid")
        if (
            not isinstance(self.profiles, tuple)
            or len(self.profiles) > MAX_PROFILES
            or any(not isinstance(item, BridgeProfile) for item in self.profiles)
            or not isinstance(self.entries, tuple)
            or len(self.entries) > MAX_RETAINED_DEVICES
            or any(not isinstance(item, BridgeDevice) for item in self.entries)
        ):
            raise BridgeRegistryError("Bridge registry count or entries are invalid")
        for values in (
            [item.profile_id for item in self.profiles],
            [item.username for item in self.profiles],
            [item.accessory_uuid for item in (*self.profiles, *self.entries)],
            [item.device_id for item in self.entries],
        ):
            if len(set(values)) != len(values):
                raise BridgeRegistryError("Bridge registry contains duplicate identities")
        profiles = {item.profile_id for item in self.profiles}
        if any(item.profile_id not in profiles for item in self.entries):
            raise BridgeRegistryError("Bridge device has no configured account")
        for profile in self.profiles:
            entries = tuple(item for item in self.entries if item.profile_id == profile.profile_id)
            if sum(item.selected for item in entries) > MAX_BRIDGE_CHILDREN:
                raise BridgeRegistryError("Bridge selection exceeds HomeKit capacity")
            if any(item.selected and not _wanted(profile, item) for item in entries):
                raise BridgeRegistryError("Bridge selection conflicts with its import policy")
            _known_preferences(profile, entries, ())

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "revision": self.revision,
            "profiles": [item.as_dict() for item in self.profiles],
            "entries": [item.as_dict() for item in self.entries],
        }

    def profile(self, profile_id: str) -> BridgeProfile | None:
        _profile(profile_id)
        return next((item for item in self.profiles if item.profile_id == profile_id), None)


def _profile(profile_id: str) -> None:
    if not valid_profile_id(profile_id):
        raise BridgeRegistryError("Bridge account identity is invalid")


def _decode(payload: dict[str, Any]) -> BridgeRegistry:
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "revision",
        "profiles",
        "entries",
    }:
        raise BridgeRegistryError("Bridge registry has an invalid format")
    profiles, entries = payload["profiles"], payload["entries"]
    if (
        not isinstance(profiles, list)
        or len(profiles) > MAX_PROFILES
        or not isinstance(entries, list)
        or len(entries) > MAX_RETAINED_DEVICES
    ):
        raise BridgeRegistryError("Bridge registry count or entries are invalid")
    decoded_profiles, decoded_entries = [], []
    for item in profiles:
        if not isinstance(item, dict) or set(item) != set(BridgeProfile.__dataclass_fields__):
            raise BridgeRegistryError("Bridge profile has an invalid format")
        if any(not isinstance(item[field], list) for field in _FILTERS):
            raise BridgeRegistryError("Bridge profile filters must be lists")
        decoded_profiles.append(
            BridgeProfile(**(item | {field: tuple(item[field]) for field in _FILTERS}))
        )
    for item in entries:
        if (
            not isinstance(item, dict)
            or set(item) != set(BridgeDevice.__dataclass_fields__)
            or not isinstance(item["mappings"], list)
        ):
            raise BridgeRegistryError("Bridge device has an invalid format")
        decoded_entries.append(BridgeDevice(**(item | {"mappings": tuple(item["mappings"])})))
    return BridgeRegistry(
        payload["version"], payload["revision"], tuple(decoded_profiles), tuple(decoded_entries)
    )


def _records(profile_id: str, records: tuple[DeviceRecord, ...]) -> tuple[DeviceRecord, ...]:
    _profile(profile_id)
    if not isinstance(records, tuple) or len(records) > MAX_RETAINED_DEVICES:
        raise BridgeRegistryError("Bridge discovery must be a bounded device tuple")
    try:
        for record in records:
            if not isinstance(record, DeviceRecord):
                raise ValueError("record")
            identity = DeviceIdentity(**vars(record.identity))
            observations = tuple(DeviceObservation(**vars(item)) for item in record.observations)
            DeviceRecord(**(vars(record) | {"identity": identity, "observations": observations}))
            if (
                identity.profile_id != profile_id
                or not _kind(identity.provider)
                or not _kind(record.kind)
            ):
                raise ValueError("scope or classification")
        if len({item.key for item in records}) != len(records):
            raise ValueError("duplicate")
    except (AttributeError, TypeError, ValueError) as exc:
        raise BridgeRegistryError(
            "Bridge discovery contains invalid or cross-account devices"
        ) from exc
    return records


def _mapping(record: DeviceRecord) -> tuple[str, ...]:
    allowed = _KIND_MAPPINGS.get(record.kind)
    if (
        allowed is None
        or record.identity.provider != "ring-hub"
        or record.family != "hub"
        or not record.supported
        or "ring-hub-device-schema" not in record.capability_evidence
        or not set(allowed[0]).issubset(record.capabilities)
    ):
        return ()
    return allowed[1]


def _exclusion(profile: BridgeProfile, entry: BridgeDevice) -> str | None:
    if entry.device_id in profile.excluded_devices:
        return "device-excluded"
    if entry.location_id in profile.excluded_locations:
        return "location-excluded"
    if entry.kind in profile.excluded_kinds:
        return "kind-excluded"
    if entry.mappings and not set(entry.mappings).difference(profile.excluded_mappings):
        return "mappings-excluded"
    return None


def _wanted(profile: BridgeProfile, entry: BridgeDevice) -> bool:
    return (
        bool(entry.mappings)
        and _exclusion(profile, entry) is None
        and (profile.auto_import or entry.device_id in profile.included_devices)
    )


def _eligible(entry: BridgeDevice, record: DeviceRecord | None) -> bool:
    return bool(entry.mappings) and (
        entry.qualified
        if record is None
        else (
            entry.provider == record.identity.provider
            and entry.kind == record.kind
            and entry.location_id == record.identity.location_key
            and entry.mappings == _mapping(record)
        )
    )


def _known_preferences(
    profile: BridgeProfile, entries: tuple[BridgeDevice, ...], records: tuple[DeviceRecord, ...]
) -> None:
    devices = {item.device_id for item in entries} | {item.key for item in records}
    locations = {item.location_id for item in entries} | {
        item.identity.location_key for item in records
    }
    kinds = {item.kind for item in entries} | {item.kind for item in records}
    if (
        not set((*profile.included_devices, *profile.excluded_devices)).issubset(devices)
        or not set(profile.excluded_locations).issubset(locations)
        or not set(profile.excluded_kinds).issubset(kinds)
    ):
        raise BridgeRegistryError("Bridge preferences reference unknown account devices or filters")


def _new_uuid(used: set[str]) -> str:
    for _ in range(32):
        value = str(uuid4())
        if value not in used:
            used.add(value)
            return value
    raise BridgeRegistryError("Cannot allocate a unique bridge accessory identity")


def _new_username(used: set[str]) -> str:
    for _ in range(32):
        raw = bytearray(secrets.token_bytes(6))
        raw[0] = (raw[0] & 0xFC) | 0x02
        value = ":".join(f"{byte:02X}" for byte in raw)
        if value not in used:
            return value
    raise BridgeRegistryError("Cannot allocate a unique bridge HomeKit username")


def _enroll(
    current: BridgeRegistry, profile: BridgeProfile, records: tuple[DeviceRecord, ...]
) -> BridgeRegistry:
    entries = list(current.entries)
    known = {item.device_id for item in entries}
    used = {item.accessory_uuid for item in (*current.profiles, *current.entries)} | {
        profile.accessory_uuid
    }
    for record in sorted(records, key=lambda item: item.key):
        if record.key in known or len(entries) == MAX_RETAINED_DEVICES:
            continue
        entries.append(
            BridgeDevice(
                profile.profile_id,
                record.key,
                record.identity.location_key,
                record.identity.provider,
                record.kind,
                _new_uuid(used),
                _mapping(record),
                qualified=bool(_mapping(record)),
            )
        )
    records_by_id = {item.key: item for item in records}
    updated = []
    for item in entries:
        record = records_by_id.get(item.device_id)
        if record is not None:
            same_metadata = (
                item.provider == record.identity.provider
                and item.kind == record.kind
                and item.location_id == record.identity.location_key
            )
            mappings = item.mappings or (_mapping(record) if same_metadata else ())
            qualified = bool(mappings) and same_metadata and mappings == _mapping(record)
            item = replace(
                item, mappings=mappings, qualified=qualified, selected=item.selected and qualified
            )
        updated.append(item)
    entries = updated
    # Missing selected identities reserve their slots. Discovery order must not
    # evict an accessory that has already been selected for pairing.
    eligible = sorted(
        (
            item
            for item in entries
            if item.profile_id == profile.profile_id
            and _wanted(profile, item)
            and _eligible(item, records_by_id.get(item.device_id))
        ),
        key=lambda item: (not item.selected, item.device_id),
    )
    selected = {item.device_id for item in eligible[:MAX_BRIDGE_CHILDREN]}
    entries = [
        replace(item, selected=item.device_id in selected)
        if item.profile_id == profile.profile_id
        else item
        for item in entries
    ]
    profiles = tuple(
        sorted(
            (
                *(item for item in current.profiles if item.profile_id != profile.profile_id),
                profile,
            ),
            key=lambda item: item.profile_id,
        )
    )
    return replace(
        current,
        profiles=profiles,
        entries=tuple(sorted(entries, key=lambda item: (item.profile_id, item.device_id))),
    )


class BridgeRegistryStore(PrivateJsonStore[BridgeRegistry]):
    def __init__(self, path: str | Path) -> None:
        super().__init__(
            path,
            snapshot_type=BridgeRegistry,
            decode=_decode,
            default=BridgeRegistry,
            max_bytes=MAX_REGISTRY_BYTES,
            label="HomeKit device bridge registry",
            noun="device bridge registry",
            error_type=BridgeRegistryError,
            conflict_type=BridgeRegistryConflictError,
        )
        self.enrollment_path = self.path.with_name(f".{self.path.name}.enrolled")

    def load(self) -> BridgeRegistry:
        try:
            self.path.lstat()
        except FileNotFoundError:
            try:
                self.enrollment_path.lstat()
            except FileNotFoundError:
                return BridgeRegistry()
            except OSError as exc:
                raise BridgeRegistryError("Cannot access bridge enrollment") from exc
        except OSError as exc:
            raise BridgeRegistryError("Cannot access device bridge registry") from exc
        return super().load()

    def _marker_present(self, directory: int) -> bool:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(self.enrollment_path.name, flags, dir_fd=directory)
        except FileNotFoundError:
            return False
        try:
            self._validate_file(descriptor, "Bridge enrollment marker")
            if os.read(descriptor, len(_MARKER) + 1) != _MARKER:
                raise BridgeRegistryError("Bridge enrollment marker is invalid")
            return True
        finally:
            os.close(descriptor)

    def _read_locked(self, directory: int) -> BridgeRegistry:
        current = super()._read_locked(directory)
        enrolled = self._marker_present(directory)
        if enrolled and not current.profiles:
            raise BridgeRegistryError("Enrolled bridge registry is missing")
        if current.profiles and not enrolled:
            raise BridgeRegistryError("Bridge registry enrollment marker is missing")
        return current

    def _write_locked(self, directory: int, snapshot: BridgeRegistry) -> None:
        # Mark enrollment durably BEFORE creating identities. A crash between
        # these writes fails closed instead of silently allocating replacements.
        if snapshot.profiles and not self._marker_present(directory):
            descriptor = -1
            try:
                descriptor = os.open(
                    self.enrollment_path.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(_MARKER)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(directory)
            except OSError as exc:
                raise BridgeRegistryError(
                    "Bridge enrollment may have started; reload before retrying",
                    possibly_saved=True,
                ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        super()._write_locked(directory, snapshot)

    def _validated(self, snapshot: BridgeRegistry) -> BridgeRegistry:
        try:
            return super()._validated(snapshot)
        except (AttributeError, TypeError, ValueError) as exc:
            raise BridgeRegistryError("Bridge registry contains invalid objects") from exc

    def _save_locked(
        self, directory: int, current: BridgeRegistry, snapshot: BridgeRegistry
    ) -> BridgeRegistry:
        profiles = {item.profile_id: item for item in snapshot.profiles}
        entries = {item.device_id: item for item in snapshot.entries}
        for profile in current.profiles:
            replacement = profiles.get(profile.profile_id)
            if replacement is None or (replacement.accessory_uuid, replacement.username) != (
                profile.accessory_uuid,
                profile.username,
            ):
                raise BridgeRegistryError(
                    "Bridge account identities cannot be removed or replaced implicitly"
                )
        for entry in current.entries:
            replacement = entries.get(entry.device_id)
            if (
                replacement is None
                or (entry.mappings and replacement.mappings != entry.mappings)
                or replace(
                    entry,
                    mappings=replacement.mappings,
                    qualified=replacement.qualified,
                    selected=replacement.selected,
                )
                != replacement
            ):
                raise BridgeRegistryError(
                    "Bridge device identities cannot be removed or replaced implicitly"
                )
        return super()._save_locked(directory, current, snapshot)

    def reconcile(
        self,
        profile_id: str,
        records: tuple[DeviceRecord, ...],
        *,
        expected_revision: int | None = None,
    ) -> BridgeRegistry:
        records = _records(profile_id, records)
        current = self.load()
        if expected_revision is not None:
            self._expected(expected_revision)
            self._check_revision(current, expected_revision)
        if current.profile(profile_id) is None:
            return current
        return self.update(
            lambda snapshot: _enroll(snapshot, snapshot.profile(profile_id), records),
            expected_revision=expected_revision,
        )

    def configure(
        self,
        profile_id: str,
        *,
        expected_revision: int,
        enabled: bool,
        auto_import: bool,
        records: tuple[DeviceRecord, ...] = (),
        included_devices: tuple[str, ...] = (),
        excluded_devices: tuple[str, ...] = (),
        excluded_locations: tuple[str, ...] = (),
        excluded_kinds: tuple[str, ...] = (),
        excluded_mappings: tuple[str, ...] = (),
    ) -> BridgeRegistry:
        records = _records(profile_id, records)
        filters = {
            "included_devices": included_devices,
            "excluded_devices": excluded_devices,
            "excluded_locations": excluded_locations,
            "excluded_kinds": excluded_kinds,
            "excluded_mappings": excluded_mappings,
        }
        sample = BridgeProfile(
            profile_id,
            "11111111-1111-4111-8111-111111111111",
            "02:00:00:00:00:00",
            enabled=enabled,
            auto_import=auto_import,
            **filters,
        )

        def configure(current: BridgeRegistry) -> BridgeRegistry:
            existing = current.profile(profile_id)
            if existing is None:
                if len(current.profiles) == MAX_PROFILES:
                    raise BridgeRegistryError("Bridge account capacity reached")
                used = {item.accessory_uuid for item in (*current.profiles, *current.entries)}
                profile = replace(
                    sample,
                    accessory_uuid=_new_uuid(used),
                    username=_new_username({item.username for item in current.profiles}),
                )
            else:
                profile = replace(existing, enabled=enabled, auto_import=auto_import, **filters)
            entries = tuple(item for item in current.entries if item.profile_id == profile_id)
            _known_preferences(profile, entries, records)
            return _enroll(current, profile, records)

        return self.update(configure, expected_revision=expected_revision)


def bridge_projection(
    registry: BridgeRegistry, profile_id: str | None, records: tuple[DeviceRecord, ...] = ()
) -> dict[str, Any]:
    """Pure registry projection; the coordinator supplies runtime publication state."""
    if profile_id is not None:
        records = _records(profile_id, records)
    elif records:
        raise BridgeRegistryError("Bridge projection needs an account for discovered devices")
    try:
        registry = _decode(registry.as_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise BridgeRegistryError("Bridge projection requires a valid registry") from exc
    profile = registry.profile(profile_id) if profile_id else None
    entries = {item.device_id: item for item in registry.entries if item.profile_id == profile_id}
    discovered = {item.key: item for item in records}
    devices = []
    for device_id in sorted(entries.keys() | discovered.keys()):
        entry, record = entries.get(device_id), discovered.get(device_id)
        mappings = entry.mappings if entry is not None else _mapping(record)
        eligible = bool(mappings) and (entry is None or _eligible(entry, record))
        excluded = _exclusion(profile, entry) if profile is not None and entry is not None else None
        if profile is not None and entry is None:
            if device_id in profile.excluded_devices:
                excluded = "device-excluded"
            elif record.identity.location_key in profile.excluded_locations:
                excluded = "location-excluded"
            elif record.kind in profile.excluded_kinds:
                excluded = "kind-excluded"
            elif mappings and not set(mappings).difference(profile.excluded_mappings):
                excluded = "mappings-excluded"
        selected = entry.selected if entry is not None else False
        present = record is not None and record.source_health != "unavailable"
        if profile is None:
            reason = "bridge-not-configured"
        elif excluded:
            reason = excluded
        elif not eligible:
            reason = "unsupported-device" if not mappings else "capability-mismatch"
        elif not (profile.auto_import or device_id in profile.included_devices):
            reason = "not-selected"
        elif entry is None:
            reason = "registry-capacity"
        elif not selected:
            reason = "bridge-capacity"
        elif not profile.enabled:
            reason = "bridge-disabled"
        else:
            reason = "ready" if present else "device-missing"
        effective_mappings = tuple(
            value for value in mappings if profile is None or value not in profile.excluded_mappings
        )
        devices.append(
            {
                "id": device_id,
                "accessory_uuid": entry.accessory_uuid if entry else None,
                "mappings": list(effective_mappings),
                "selected": selected,
                "excluded": excluded is not None,
                "eligible": eligible,
                "reason": reason,
                "present": present,
            }
        )
    return {
        "revision": registry.revision,
        "configured": profile is not None,
        "enabled": profile.enabled if profile else False,
        "auto_import": profile.auto_import if profile else False,
        "accessory_uuid": profile.accessory_uuid if profile else None,
        "username": profile.username if profile else None,
        "capacity": MAX_BRIDGE_CHILDREN,
        "selected_count": sum(item.selected for item in entries.values()),
        "retained_count": len(entries),
        "overflow_count": sum(
            item["reason"] in {"bridge-capacity", "registry-capacity"} for item in devices
        ),
        **{field: list(getattr(profile, field)) if profile else [] for field in _FILTERS},
        "publication": "not_implemented",
        "devices": devices,
    }
