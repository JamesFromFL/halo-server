"""Durable owner warm opt-in and HAP recording selections, without HAP secrets."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

from halo_server.account_scope import valid_profile_id
from halo_server.homekit.models import NativeInventory
from halo_server.homekit.registry import CameraRegistry
from halo_server.homekit.text import literal_text
from halo_server.media.h264 import H264Error, VideoConfiguration
from halo_server.media.recording import (
    RecordingAudioConfiguration,
    RecordingConfiguration,
    RecordingError,
)
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore


class RecordingSettingsError(PrivateJsonError):
    """Recording authority could not be loaded or durably saved."""


class RecordingSettingsConflict(RecordingSettingsError):
    """A newer durable recording revision superseded the submitted change."""


VIDEO_FIELDS = {"width", "height", "fps", "profile", "level", "max_bit_rate"}
AUDIO_FIELDS = {"sample_rate", "bit_rate", "channels", "bitrate_mode"}
CONFIG_FIELDS = {"video", "audio", "fragment_length_ms", "i_frame_interval_ms"}


def configuration_wire(configuration):
    if configuration is None:
        return None
    if not isinstance(configuration, RecordingConfiguration):
        raise RecordingSettingsError("Recording configuration is invalid")
    return {
        "video": {name: getattr(configuration.video, name) for name in sorted(VIDEO_FIELDS)},
        "audio": None
        if configuration.audio is None
        else {name: getattr(configuration.audio, name) for name in sorted(AUDIO_FIELDS)},
        "fragment_length_ms": configuration.fragment_length_ms,
        "i_frame_interval_ms": configuration.i_frame_interval_ms,
    }


def configuration_from_wire(value):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != CONFIG_FIELDS
        or not isinstance(value["video"], dict)
        or set(value["video"]) != VIDEO_FIELDS
        or value["audio"] is not None
        and (not isinstance(value["audio"], dict) or set(value["audio"]) != AUDIO_FIELDS)
    ):
        raise RecordingSettingsError("Recording configuration has an invalid format")
    try:
        return RecordingConfiguration(
            VideoConfiguration(**value["video"]),
            RecordingAudioConfiguration(**value["audio"]) if value["audio"] is not None else None,
            fragment_length_ms=value["fragment_length_ms"],
            i_frame_interval_ms=value["i_frame_interval_ms"],
        )
    except (TypeError, ValueError, H264Error, RecordingError) as error:
        raise RecordingSettingsError("Recording configuration is unsupported") from error


@dataclass(frozen=True)
class RecordingPreferences:
    active: bool = False
    audio_active: bool = False
    configuration: RecordingConfiguration | None = None

    def __post_init__(self):
        if (
            type(self.active) is not bool
            or type(self.audio_active) is not bool
            or (
                self.configuration is not None
                and not isinstance(self.configuration, RecordingConfiguration)
            )
        ):
            raise RecordingSettingsError("Recording preferences are invalid")

    def wire(self):
        return {
            "active": self.active,
            "audio_active": self.audio_active,
            "configuration": configuration_wire(self.configuration),
        }


@dataclass(frozen=True)
class CameraRecordingSettings:
    profile_id: str
    camera_id: str
    accessory_id: str
    owner_warm: bool = False
    preferences: RecordingPreferences = RecordingPreferences()
    preferences_revision: int = 0

    def __post_init__(self):
        try:
            valid_id = (
                isinstance(self.accessory_id, str)
                and str(UUID(self.accessory_id)) == self.accessory_id
            )
        except (ValueError, AttributeError):
            valid_id = False
        if (
            not valid_profile_id(self.profile_id)
            or not literal_text(self.camera_id, 128, empty=False)
            or not valid_id
            or type(self.owner_warm) is not bool
            or not isinstance(self.preferences, RecordingPreferences)
            or type(self.preferences_revision) is not int
            or not 0 <= self.preferences_revision <= MAX_REVISION
        ):
            raise RecordingSettingsError("Recording camera identity is invalid")

    @property
    def key(self):
        return self.profile_id, self.camera_id

    def wire(self, *, enabled):
        return {
            "revision": self.preferences_revision,
            "enabled": enabled,
            **self.preferences.wire(),
        }

    def as_dict(self):
        return {
            "profile_id": self.profile_id,
            "camera_id": self.camera_id,
            "accessory_id": self.accessory_id,
            "owner_warm": self.owner_warm,
            "preferences": self.preferences.wire(),
            "preferences_revision": self.preferences_revision,
        }


@dataclass(frozen=True)
class RecordingSettings:
    version: int = 2
    revision: int = 0
    entries: tuple[CameraRecordingSettings, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version not in {1, 2}
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or not isinstance(self.entries, tuple)
            or len(self.entries) > 256
            or any(not isinstance(row, CameraRecordingSettings) for row in self.entries)
            or len({row.key for row in self.entries}) != len(self.entries)
            or len({row.accessory_id for row in self.entries}) != len(self.entries)
        ):
            raise RecordingSettingsError("Recording settings are invalid")

    def camera(self, profile_id, camera_id):
        return next((row for row in self.entries if row.key == (profile_id, camera_id)), None)

    def as_dict(self):
        return {
            "version": self.version,
            "revision": self.revision,
            "entries": [row.as_dict() for row in self.entries],
        }


def _decode(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "revision", "entries"}
        or not isinstance(value["entries"], list)
        or len(value["entries"]) > 256
        or type(value["version"]) is not int
        or value["version"] not in {1, 2}
    ):
        raise RecordingSettingsError("Recording settings have an invalid format")
    entries = []
    for row in value["entries"]:
        if (
            not isinstance(row, dict)
            or set(row)
            != (
                {"profile_id", "camera_id", "accessory_id", "owner_warm", "preferences"}
                | ({"preferences_revision"} if value["version"] == 2 else set())
            )
            or not isinstance(row["preferences"], dict)
            or set(row["preferences"]) != {"active", "audio_active", "configuration"}
        ):
            raise RecordingSettingsError("Recording settings have an invalid camera row")
        preferences = dict(row["preferences"])
        preferences["configuration"] = configuration_from_wire(preferences["configuration"])
        entries.append(
            CameraRecordingSettings(
                **(
                    row
                    | {"preferences": RecordingPreferences(**preferences)}
                    | ({"preferences_revision": value["revision"]} if value["version"] == 1 else {})
                )
            )
        )
    return RecordingSettings(**(value | {"entries": tuple(entries)}))


@dataclass(frozen=True)
class _Enrollment:
    version: int = 1
    revision: int = 0
    initialized: bool = False

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or type(self.initialized) is not bool
        ):
            raise RecordingSettingsError("Recording enrollment marker is invalid")

    def as_dict(self):
        return {"version": self.version, "revision": self.revision, "initialized": self.initialized}


def _decode_enrollment(value):
    if not isinstance(value, dict) or set(value) != {"version", "revision", "initialized"}:
        raise RecordingSettingsError("Recording enrollment marker has an invalid format")
    return _Enrollment(**value)


def privacy_signature(row):
    if row is None:
        return None
    preferences = row.preferences
    return (
        row.owner_warm,
        preferences.active,
        preferences.configuration is not None,
        preferences.audio_active
        and preferences.configuration is not None
        and preferences.configuration.audio is not None,
    )


class RecordingSettingsStore(PrivateJsonStore[RecordingSettings]):
    def __init__(self, path: str | Path):
        super().__init__(
            path,
            snapshot_type=RecordingSettings,
            decode=_decode,
            default=RecordingSettings,
            max_bytes=512 * 1024,
            label="HomeKit recording settings",
            noun="recording settings",
            error_type=RecordingSettingsError,
            conflict_type=RecordingSettingsConflict,
        )
        self._enrollment = PrivateJsonStore(
            self.path.with_name(f".{self.path.name}.enrolled"),
            snapshot_type=_Enrollment,
            decode=_decode_enrollment,
            default=_Enrollment,
            max_bytes=1024,
            label="HomeKit recording enrollment",
            noun="recording enrollment",
            error_type=RecordingSettingsError,
            conflict_type=RecordingSettingsConflict,
        )

    def _read_locked(self, directory):
        snapshot = super()._read_locked(directory)
        if snapshot == RecordingSettings() and self._enrollment.load().initialized:
            raise RecordingSettingsError(
                "Enrolled recording settings are missing; restore durable state"
            )
        if snapshot.version == 1:
            # Known S12 format: preserve every identity and preference. Its
            # global token becomes the initial camera token, then both move
            # independently only through future validated transactions.
            snapshot = self._save_locked(directory, snapshot, replace(snapshot, version=2))
        return snapshot

    def _save_locked(self, directory, current, snapshot):
        proposed = {row.key: row for row in snapshot.entries}
        for old in current.entries:
            new = proposed.get(old.key)
            if new is None or new.accessory_id != old.accessory_id:
                raise RecordingSettingsError(
                    "Recording identities cannot be removed or replaced implicitly"
                )
            expected = old.preferences_revision + int(new.preferences != old.preferences)
            if expected > MAX_REVISION or new.preferences_revision != expected:
                raise RecordingSettingsError(
                    "Recording preferences revision must advance exactly once"
                )
        if any(
            row.key not in {old.key for old in current.entries} and row.preferences_revision != 0
            for row in snapshot.entries
        ):
            raise RecordingSettingsError("New recording preferences must start at revision zero")
        return super()._save_locked(directory, current, snapshot)

    def reconcile(self, registry: CameraRegistry, inventory: NativeInventory):
        if not isinstance(registry, CameraRegistry) or not isinstance(inventory, NativeInventory):
            raise RecordingSettingsError(
                "Recording enrollment requires trusted registry and inventory"
            )

        def enroll(current):
            if not inventory.complete or inventory.profile_id is None:
                return current
            available = {camera.scope.camera_id for camera in inventory.cameras}
            entries = {row.key: row for row in current.entries}
            for identity in registry.entries:
                if (
                    identity.profile_id != inventory.profile_id
                    or identity.camera_id not in available
                ):
                    continue
                old = entries.get(identity.key)
                if old is not None and old.accessory_id != identity.accessory_uuid:
                    raise RecordingSettingsError(
                        "Recording accessory identity does not match registry"
                    )
                if old is None:
                    if len(entries) >= 256:
                        raise RecordingSettingsError("Recording identity limit reached")
                    entries[identity.key] = CameraRecordingSettings(
                        identity.profile_id, identity.camera_id, identity.accessory_uuid
                    )
            return replace(
                current, entries=tuple(sorted(entries.values(), key=lambda row: row.key))
            )

        saved = self.update(enroll)
        if saved.revision:
            self._enrollment.update(lambda current: replace(current, initialized=True))
        return saved

    def set_preferences(
        self, profile_id, camera_id, accessory_id, preferences, *, expected_revision
    ):
        if not isinstance(preferences, RecordingPreferences):
            raise RecordingSettingsError("Recording preferences are invalid")
        return self._change(
            profile_id,
            camera_id,
            accessory_id,
            expected_revision,
            lambda row: self._preferences(row, preferences),
        )

    @staticmethod
    def _preferences(row, preferences):
        if row.preferences == preferences:
            return row
        if row.preferences_revision == MAX_REVISION:
            raise RecordingSettingsError("Recording preferences revision limit reached")
        return replace(
            row, preferences=preferences, preferences_revision=row.preferences_revision + 1
        )

    def set_hap_preferences(
        self, profile_id, camera_id, accessory_id, preferences, *, expected_preferences_revision
    ):
        """CAS only the target camera while preserving concurrent other rows."""
        if not isinstance(preferences, RecordingPreferences):
            raise RecordingSettingsError("Recording preferences are invalid")
        self._expected(expected_preferences_revision)

        def change(current):
            row = current.camera(profile_id, camera_id)
            if row is None or row.accessory_id != accessory_id:
                raise RecordingSettingsError("Recording settings do not belong to this accessory")
            if row.preferences_revision != expected_preferences_revision:
                raise RecordingSettingsConflict(
                    "Recording camera preferences changed; reload before saving"
                )
            updated = self._preferences(row, preferences)
            return replace(
                current, entries=tuple(updated if item is row else item for item in current.entries)
            )

        return self.update(change)

    def set_owner_warm(self, profile_id, camera_id, accessory_id, enabled, *, expected_revision):
        if type(enabled) is not bool:
            raise RecordingSettingsError("Owner warm opt-in must be a boolean")
        return self._change(
            profile_id,
            camera_id,
            accessory_id,
            expected_revision,
            lambda row: replace(row, owner_warm=enabled),
        )

    def _change(self, profile_id, camera_id, accessory_id, revision, transform):
        def change(current):
            row = current.camera(profile_id, camera_id)
            if row is None or row.accessory_id != accessory_id:
                raise RecordingSettingsError("Recording settings do not belong to this accessory")
            return replace(
                current,
                entries=tuple(transform(item) if item is row else item for item in current.entries),
            )

        return self.update(change, expected_revision=revision)
