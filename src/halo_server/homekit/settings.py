"""Durable audio graph birth and mute/volume preferences, without HAP secrets."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from uuid import UUID

from halo_server.account_scope import valid_profile_id
from halo_server.homekit.models import NativeInventory
from halo_server.homekit.registry import CameraRegistry
from halo_server.homekit.text import literal_text
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

MAX_AUDIO_SETTINGS = 256


class AudioSettingsError(PrivateJsonError):
    """Audio preferences could not be read or durably saved safely."""


class AudioSettingsConflict(AudioSettingsError):
    """Another owner changed the expected audio settings revision."""


@dataclass(frozen=True)
class AudioPreferences:
    microphone_muted: bool = False
    speaker_muted: bool = False
    microphone_volume: int = 100
    speaker_volume: int = 100

    def __post_init__(self):
        if any(type(value) is not bool for value in (self.microphone_muted, self.speaker_muted)):
            raise AudioSettingsError("Audio mute values must be booleans")
        if any(
            type(value) is not int or not 0 <= value <= 100
            for value in (self.microphone_volume, self.speaker_volume)
        ):
            raise AudioSettingsError("Audio volumes must be whole percentages")


@dataclass(frozen=True)
class CameraAudioSettings:
    profile_id: str
    camera_id: str
    accessory_id: str
    microphone: bool
    speaker: bool
    preferences: AudioPreferences = field(default_factory=AudioPreferences)

    def __post_init__(self):
        if (
            not valid_profile_id(self.profile_id)
            or not literal_text(self.camera_id, 128, empty=False)
            or not valid_profile_id(self.accessory_id)
            or UUID(self.accessory_id).version != 4
            or type(self.microphone) is not bool
            or type(self.speaker) is not bool
            or self.speaker
            and not self.microphone
            or not isinstance(self.preferences, AudioPreferences)
        ):
            raise AudioSettingsError("Audio accessory identity or graph is invalid")
        AudioPreferences(**asdict(self.preferences))

    @property
    def key(self):
        return self.profile_id, self.camera_id

    def wire(self, revision):
        return {
            "microphone": self.microphone,
            "speaker": self.speaker,
            **asdict(self.preferences),
            "revision": revision,
        }


@dataclass(frozen=True)
class AudioSettings:
    version: int = 1
    revision: int = 0
    entries: tuple[CameraAudioSettings, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or not isinstance(self.entries, tuple)
            or len(self.entries) > MAX_AUDIO_SETTINGS
            or any(not isinstance(row, CameraAudioSettings) for row in self.entries)
            or len({row.key for row in self.entries}) != len(self.entries)
            or len({row.accessory_id for row in self.entries}) != len(self.entries)
        ):
            raise AudioSettingsError("Audio settings have an invalid format")

    def as_dict(self):
        return {
            "version": self.version,
            "revision": self.revision,
            "entries": [asdict(row) for row in self.entries],
        }

    def camera(self, profile_id, camera_id):
        return next((row for row in self.entries if row.key == (profile_id, camera_id)), None)


@dataclass(frozen=True)
class _Enrollment:
    version: int = 1
    revision: int = 0
    initialized: bool = False

    def as_dict(self):
        return asdict(self)


def _decode_enrollment(payload):
    if (
        set(payload) != {"version", "revision", "initialized"}
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or type(payload["revision"]) is not int
        or not 0 <= payload["revision"] <= MAX_REVISION
        or type(payload["initialized"]) is not bool
        or payload["initialized"] is not True
    ):
        raise AudioSettingsError("Audio enrollment marker is invalid")
    return _Enrollment(**payload)


def _decode(payload):
    if set(payload) != {"version", "revision", "entries"} or not isinstance(
        payload["entries"], list
    ):
        raise AudioSettingsError("Audio settings have an invalid format")
    if len(payload["entries"]) > MAX_AUDIO_SETTINGS:
        raise AudioSettingsError("Audio settings exceed the retained identity limit")
    entries = []
    fields = {"profile_id", "camera_id", "accessory_id", "microphone", "speaker", "preferences"}
    for raw in payload["entries"]:
        if (
            not isinstance(raw, dict)
            or set(raw) != fields
            or not isinstance(raw["preferences"], dict)
            or set(raw["preferences"]) != set(AudioPreferences.__dataclass_fields__)
        ):
            raise AudioSettingsError("Audio camera settings have an invalid format")
        try:
            entries.append(
                CameraAudioSettings(
                    **(raw | {"preferences": AudioPreferences(**raw["preferences"])})
                )
            )
        except TypeError as exc:
            raise AudioSettingsError("Audio camera settings have an invalid format") from exc
    return AudioSettings(**(payload | {"entries": tuple(entries)}))


class AudioSettingsStore(PrivateJsonStore[AudioSettings]):
    def __init__(self, path: str | Path):
        super().__init__(
            path,
            snapshot_type=AudioSettings,
            decode=_decode,
            default=AudioSettings,
            max_bytes=256 * 1024,
            label="HomeKit audio settings",
            noun="audio settings",
            error_type=AudioSettingsError,
            conflict_type=AudioSettingsConflict,
        )
        self._enrollment = PrivateJsonStore(
            self.path.with_name(f".{self.path.name}.enrolled"),
            snapshot_type=_Enrollment,
            decode=_decode_enrollment,
            default=_Enrollment,
            max_bytes=1024,
            label="HomeKit audio enrollment",
            noun="audio enrollment",
            error_type=AudioSettingsError,
            conflict_type=AudioSettingsConflict,
        )

    def _read_locked(self, directory):
        snapshot = super()._read_locked(directory)
        if snapshot == AudioSettings() and self._enrollment.load().initialized:
            raise AudioSettingsError("Enrolled audio settings are missing; restore durable state")
        return snapshot

    def _save_locked(self, directory, current, snapshot):
        proposed = {row.key: row for row in snapshot.entries}
        for old in current.entries:
            new = proposed.get(old.key)
            if new is None or replace(old, preferences=new.preferences) != new:
                raise AudioSettingsError(
                    "Audio graph identities cannot be replaced or removed implicitly"
                )
        return super()._save_locked(directory, current, snapshot)

    def reconcile(self, registry: CameraRegistry, inventory: NativeInventory):
        if not isinstance(registry, CameraRegistry) or not isinstance(inventory, NativeInventory):
            raise AudioSettingsError("Audio enrollment requires a trusted registry and inventory")

        def enroll(current):
            if not inventory.complete or inventory.profile_id is None:
                return current
            available = {camera.scope.camera_id: camera for camera in inventory.cameras}
            known = {row.key: row for row in current.entries}
            entries = list(current.entries)
            for identity in registry.entries:
                camera = available.get(identity.camera_id)
                if identity.profile_id != inventory.profile_id or camera is None:
                    continue
                old = known.get(identity.key)
                if old is not None:
                    if old.accessory_id != identity.accessory_uuid:
                        raise AudioSettingsError(
                            "Audio accessory identity does not match its registry"
                        )
                    continue
                if len(entries) == MAX_AUDIO_SETTINGS:
                    raise AudioSettingsError("Retained audio identities are full")
                microphone = camera.capabilities.video
                entries.append(
                    CameraAudioSettings(
                        identity.profile_id,
                        identity.camera_id,
                        identity.accessory_uuid,
                        microphone,
                        microphone and camera.capabilities.talkback,
                    )
                )
            return replace(current, entries=tuple(sorted(entries, key=lambda row: row.key)))

        result = self.update(enroll)
        # No graph is published before both records are durable. A first-start
        # interruption after the primary write can finish the marker next time.
        # Losing the primary after successful enrollment must never mean unmute.
        if result.revision:
            self._enrollment.update(lambda current: replace(current, initialized=True))
        return result

    def set_preferences(
        self, profile_id, camera_id, accessory_id, preferences, *, expected_revision
    ):
        if not isinstance(preferences, AudioPreferences):
            raise AudioSettingsError("Audio preferences must be a validated snapshot")

        def change(current):
            row = current.camera(profile_id, camera_id)
            if row is None or row.accessory_id != accessory_id:
                raise AudioSettingsError("Audio settings do not belong to this accessory")
            return replace(
                current,
                entries=tuple(
                    replace(item, preferences=preferences) if item is row else item
                    for item in current.entries
                ),
            )

        return self.update(change, expected_revision=expected_revision)
