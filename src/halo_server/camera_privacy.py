"""Durable local camera privacy and read-only effective Ring permissions.

Alarm mode is not camera permission evidence. HomeKit ownership is an explicit
local exception, never a Ring setting write or a compatibility-client grant.
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, replace

from halo_server.account_scope import AccountScope, valid_profile_id
from halo_server.homekit.text import literal_text
from halo_server.paths import PathSecurityError, open_private_directory
from halo_server.private_json import MAX_REVISION, PrivateJsonError, PrivateJsonStore

PRIVACY_TTL = 120.0
MAX_CAMERAS = 256


class CameraPrivacyError(PrivateJsonError):
    pass


class CameraPrivacyConflict(CameraPrivacyError):
    pass


@dataclass(frozen=True)
class Preference:
    profile_id: str
    camera_id: str
    mode: str = "ring"
    revision: int = 0

    def __post_init__(self):
        if (
            not valid_profile_id(self.profile_id)
            or not literal_text(self.camera_id, 128, empty=False)
            or self.mode not in ("ring", "homekit")
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
        ):
            raise CameraPrivacyError("Camera privacy preference is invalid")

    @property
    def key(self):
        return self.profile_id, self.camera_id

    def as_dict(self):
        return {
            "profile_id": self.profile_id,
            "camera_id": self.camera_id,
            "mode": self.mode,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class Preferences:
    version: int = 1
    revision: int = 0
    entries: tuple[Preference, ...] = ()

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision <= MAX_REVISION
            or not isinstance(self.entries, tuple)
            or len(self.entries) > MAX_CAMERAS
            or any(not isinstance(row, Preference) for row in self.entries)
            or len({row.key for row in self.entries}) != len(self.entries)
        ):
            raise CameraPrivacyError("Camera privacy preferences are invalid")

    def camera(self, profile_id, camera_id):
        return next(
            (row for row in self.entries if row.key == (profile_id, camera_id)),
            Preference(profile_id, camera_id),
        )

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
        or len(value["entries"]) > MAX_CAMERAS
        or any(
            not isinstance(row, dict) or set(row) != {"profile_id", "camera_id", "mode", "revision"}
            for row in value["entries"]
        )
    ):
        raise CameraPrivacyError("Camera privacy preferences have an invalid format")
    return Preferences(
        **(value | {"entries": tuple(Preference(**row) for row in value["entries"])})
    )


@dataclass(frozen=True)
class RingPrivacyObservation:
    camera_id: str
    device: object
    live_allowed: bool | None
    motion_allowed: bool | None
    audio_allowed: bool | None = None


@dataclass(frozen=True)
class RingPrivacyReceipt:
    received_at: float
    rows: tuple[RingPrivacyObservation, ...]


def ring_privacy_receipt(devices, received_at):
    """Copy only explicit settings in the same Ring-loop turn as a successful GET."""
    rows, duplicates = {}, set()
    for device in devices:
        identifier = getattr(device, "id", None)
        if type(identifier) not in (int, str):
            continue
        identifier = str(identifier)
        if not literal_text(identifier, 128, empty=False):
            continue
        if identifier in rows:
            duplicates.add(identifier)
        try:
            attrs = device._attrs
            settings = attrs.get("settings") if isinstance(attrs, dict) else None
            settings = settings if isinstance(settings, dict) else {}
            disabled = settings.get("live_view_disabled")
            motion = settings.get("motion_detection_enabled")
            live = not disabled if type(disabled) is bool else None
            motion = motion if type(motion) is bool else None
            audio = settings.get("enable_audio_recording")
            audio = audio if type(audio) is bool else None
        except Exception:
            live = motion = audio = None
        rows[identifier] = RingPrivacyObservation(identifier, device, live, motion, audio)
    return RingPrivacyReceipt(
        received_at, tuple(row for key, row in rows.items() if key not in duplicates)
    )


def ring_permissions(live, motion):
    """Three-valued permissions: an explicit deny dominates an unknown input."""
    recording = (
        False
        if live is False or motion is False
        else True
        if live is True and motion is True
        else None
    )
    return live, recording, recording


def permission_kind(purpose):
    value = getattr(purpose, "value", purpose)
    if value in ("live", "talkback", "intercom", "homekit_live", "scrypted_live"):
        return 0
    if value in ("recording", "event", "homekit_recording", "homekit_recording_warm"):
        return 1
    if value in ("audio", "live_audio", "recording_audio"):
        return 3
    if value in ("snapshot", "event_snapshot"):
        return 2
    return None


class CameraPrivacyStore(PrivateJsonStore):
    def load(self):
        """Read one atomically replaced snapshot without creating files or locks.

        Writers still use the generic private transaction/CAS lock. Its atomic
        replacement means this pinned read observes one complete old/new file;
        it cannot combine snapshots or grant from partially written contents.
        """
        try:
            self.path.lstat()
        except FileNotFoundError:
            return Preferences()
        except OSError as error:
            raise CameraPrivacyError("Cannot read camera privacy preferences") from error
        directory = -1
        try:
            directory = open_private_directory(self.path.parent)
            return self._read_locked(directory)
        except (OSError, PathSecurityError) as error:
            raise CameraPrivacyError("Cannot safely read camera privacy preferences") from error
        finally:
            if directory >= 0:
                os.close(directory)


class CameraPrivacy:
    def __init__(self, path, *, clock=time.monotonic):
        self.store = CameraPrivacyStore(
            path,
            snapshot_type=Preferences,
            decode=_decode,
            default=Preferences,
            max_bytes=128 * 1024,
            label="Camera privacy",
            noun="camera privacy",
            error_type=CameraPrivacyError,
            conflict_type=CameraPrivacyConflict,
        )
        self._clock, self._lock = clock, threading.RLock()
        self._preferences, self._store_available = Preferences(), False
        self._scope = self._client = self._received_at = None
        self._rows, self._epochs, self._signatures = {}, {}, {}
        self._serial = 0
        self._pending = set()
        try:
            self._preferences = self.store.load()
            self._store_available = True
        except CameraPrivacyError:
            pass

    @property
    def available(self):
        return self._store_available

    def _fresh(self):
        return (
            type(self._received_at) in (int, float)
            and math.isfinite(self._received_at)
            and 0 <= self._clock() - self._received_at <= PRIVACY_TTL
        )

    def _preference(self, camera_id):
        return self._preferences.camera(self._scope.profile_id, camera_id)

    def _signature(self, camera_id):
        device, observation = self._rows[camera_id]
        fresh = self._fresh()
        values = (
            (observation.live_allowed, observation.motion_allowed, observation.audio_allowed)
            if fresh and observation
            else (None, None, None)
        )
        return (
            self._scope,
            id(self._client),
            id(device),
            values,
            fresh,
            self._preference(camera_id).mode,
            self._preference(camera_id).revision,
            self._store_available,
            camera_id in self._pending,
        )

    def _changed(self):
        signatures = {key: self._signature(key) for key in self._rows}
        changed = {
            key
            for key in self._signatures.keys() | signatures.keys()
            if self._signatures.get(key) != signatures.get(key)
        }
        for key in changed:
            self._serial += 1
            self._epochs[key] = self._serial
        self._signatures = signatures
        self._epochs = {key: value for key, value in self._epochs.items() if key in self._rows}
        return changed

    def observe(self, scope, client, devices, receipt):
        with self._lock:
            if not isinstance(scope, AccountScope):
                return self.invalidate()
            # A fresh GET after an expired observation cannot resurrect a
            # previously issued token even if the expiry sweep did not run.
            changed = self._changed()
            rows, duplicates = {}, set()
            observations = (
                {row.camera_id: row for row in receipt.rows}
                if isinstance(receipt, RingPrivacyReceipt)
                else {}
            )
            for device in devices:
                identifier = str(getattr(device, "id", ""))
                if not literal_text(identifier, 128, empty=False):
                    continue
                if identifier in rows:
                    duplicates.add(identifier)
                observation = observations.get(identifier)
                if observation is not None and observation.device is not device:
                    observation = None
                rows[identifier] = (device, observation)
            if len(rows) > MAX_CAMERAS:
                return self.invalidate()
            self._scope, self._client = scope, client
            self._rows = {key: value for key, value in rows.items() if key not in duplicates}
            self._received_at = (
                receipt.received_at if isinstance(receipt, RingPrivacyReceipt) else None
            )
            return changed | self._changed()

    def invalidate(self):
        with self._lock:
            changed = set(self._rows)
            self._scope = self._client = self._received_at = None
            self._rows, self._signatures, self._epochs = {}, {}, {}
            self._serial += 1
            return changed

    def expire(self):
        with self._lock:
            return self._changed()

    def current(self, scope, client, camera_id, device):
        with self._lock:
            return (
                scope is not None
                and scope == self._scope
                and client is self._client
                and camera_id in self._rows
                and self._rows[camera_id][0] is device
            )

    def epoch(self, camera_id, *, homekit=False):
        with self._lock:
            return (
                self._serial
                if camera_id not in self._rows
                else (self._epochs.get(camera_id), self._signature(camera_id))
            )

    def allowed(self, camera_id, purpose, *, homekit=False):
        with self._lock:
            index = permission_kind(purpose)
            if (
                index is None
                or camera_id not in self._rows
                or not self._store_available
                or camera_id in self._pending
            ):
                return False
            if homekit and self._preference(camera_id).mode == "homekit":
                return True
            observation = self._rows[camera_id][1]
            if not self._fresh() or observation is None:
                return False
            if index == 3:
                return observation.audio_allowed is True
            if purpose in ("event", "scrypted_live") and observation.audio_allowed is not True:
                # These compatibility consumers cannot yet filter their audio
                # track. Do not leak audio through a nominally video-only grant.
                return False
            return (
                ring_permissions(observation.live_allowed, observation.motion_allowed)[index]
                is True
            )

    def view(self, camera_id):
        with self._lock:
            preference, observation, fresh = (
                self._preference(camera_id),
                self._rows[camera_id][1],
                self._fresh(),
            )
            live, recording, snapshot = (
                ring_permissions(observation.live_allowed, observation.motion_allowed)
                if fresh and observation
                else (None, None, None)
            )
            audio = observation.audio_allowed if fresh and observation else None
            reason = (
                "source_unknown"
                if not self._store_available or self._received_at is None
                else "source_stale"
                if not fresh
                else "ring_live_disabled"
                if live is False
                else "ring_motion_disabled"
                if snapshot is False
                else "ring_audio_disabled"
                if audio is False
                else "source_unknown"
                if live is None or snapshot is None or audio is None
                else None
            )
            return {
                "camera_id": camera_id,
                "revision": preference.revision,
                "mode": preference.mode,
                "ring_live_allowed": live,
                "ring_recording_allowed": recording,
                "ring_snapshot_allowed": snapshot,
                "ring_audio_allowed": audio,
                "source_fresh": fresh,
                "effective_live_allowed": self.allowed(camera_id, "live", homekit=True),
                "effective_recording_allowed": self.allowed(camera_id, "recording", homekit=True),
                "effective_snapshot_allowed": self.allowed(camera_id, "snapshot", homekit=True),
                "effective_audio_allowed": self.allowed(camera_id, "audio", homekit=True),
                "reason": reason,
            }

    def begin_change(self, camera_id):
        with self._lock:
            self._pending.add(camera_id)
            return self._changed()

    def finish_change(self, camera_id):
        with self._lock:
            self._pending.discard(camera_id)
            return self._changed()

    def set_mode(self, profile_id, camera_id, mode, *, expected_revision):
        proposed = Preference(profile_id, camera_id, mode, expected_revision)
        with self._lock:
            if not self._store_available:
                raise CameraPrivacyError("Camera privacy preferences are unavailable")

        def update(current):
            previous = current.camera(profile_id, camera_id)
            if previous.revision != expected_revision:
                raise CameraPrivacyConflict("Camera privacy changed; reload before saving")
            if previous.mode == mode:
                return current
            if previous.revision == MAX_REVISION:
                raise CameraPrivacyError("Camera privacy revision is exhausted")
            row = replace(proposed, revision=previous.revision + 1)
            return replace(
                current,
                entries=tuple(item for item in current.entries if item.key != row.key) + (row,),
            )

        try:
            preferences = self.store.update(update)
        except CameraPrivacyConflict:
            try:
                preferences = self.store.load()
            except CameraPrivacyError:
                with self._lock:
                    self._store_available = False
            else:
                with self._lock:
                    self._preferences = preferences
            raise
        except CameraPrivacyError:
            # A failed replacement may have committed: never retain a cached
            # override until a deliberate reload/restart.
            with self._lock:
                self._store_available = False
            raise
        with self._lock:
            self._preferences = preferences
            return self._changed()
