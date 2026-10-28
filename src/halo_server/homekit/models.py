"""Immutable authority and privacy inputs for native HomeKit consumers."""

from __future__ import annotations

from dataclasses import dataclass

from halo_server.account_scope import valid_profile_id
from halo_server.homekit.text import literal_text
from halo_server.homekit.text import safe_display_text as safe_display_text
from halo_server.media.formats import MAX_NATIVE_CAMERAS, SourceFormat


@dataclass(frozen=True)
class MediaScope:
    profile_id: str
    camera_id: str
    account_generation: int
    device_generation: int
    privacy_generation: int

    def __post_init__(self) -> None:
        if not valid_profile_id(self.profile_id):
            raise ValueError("invalid account profile")
        if not literal_text(self.camera_id, 128, empty=False):
            raise ValueError("invalid camera identity")
        for value in (self.account_generation, self.device_generation, self.privacy_generation):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < 2**53:
                raise ValueError("scope generations must be positive safe integers")

    @property
    def source_key(self) -> tuple[str, str, int, int]:
        return (self.profile_id, self.camera_id, self.account_generation, self.device_generation)


@dataclass(frozen=True)
class CameraPolicy:
    """An unsynchronized adapter has no authority to acquire or deliver media."""

    synchronized: bool = False
    streaming: bool = False
    live_audio: bool = False
    snapshots: bool = False
    recording: bool = False
    recording_audio: bool = False
    talkback: bool = False
    controls: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in vars(self).values()):
            raise ValueError("camera policy values must be booleans")
        if self.recording_audio and not self.recording:
            raise ValueError("recording audio requires recording permission")

    def permits(self, operation: str) -> bool:
        if operation not in {
            "streaming",
            "live_audio",
            "snapshots",
            "recording",
            "recording_audio",
            "talkback",
            "controls",
        }:
            raise ValueError("unknown camera operation")
        return self.synchronized and getattr(self, operation)


CAPABILITIES = frozenset(
    {"video", "audio", "snapshot", "talkback", "light", "siren", "doorbell", "battery"}
)
CAPABILITY_EVIDENCE = frozenset(
    f"{source}:{kind}" for source in ("sdk", "halo") for kind in CAPABILITIES
)


class HomeKitOperationError(RuntimeError):
    """Content-free native operation failure, with conservative write uncertainty."""

    def __init__(
        self,
        code: str,
        message: str = "HomeKit operation unavailable",
        *,
        possibly_sent: bool = False,
    ):
        if (
            not isinstance(code, str)
            or not code
            or len(code) > 64
            or any(char not in "abcdefghijklmnopqrstuvwxyz_0123456789" for char in code)
        ):
            raise ValueError("invalid native error code")
        if type(possibly_sent) is not bool:
            raise ValueError("invalid write uncertainty")
        self.code = code
        self.possibly_sent = possibly_sent
        super().__init__(safe_display_text(message, 256))


def _literal_text(value: object, maximum: int, label: str) -> None:
    if not isinstance(value, str) or value != safe_display_text(value, maximum):
        raise ValueError(f"invalid {label}")


@dataclass(frozen=True)
class CameraCapabilities:
    video: bool = False
    audio: bool = False
    snapshot: bool = False
    talkback: bool = False
    light: bool = False
    siren: bool = False
    doorbell: bool = False
    battery: bool = False
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence, tuple)
            or len(self.evidence) > 16
            or any(
                not isinstance(label, str) or label not in CAPABILITY_EVIDENCE
                for label in self.evidence
            )
            or len(set(self.evidence)) != len(self.evidence)
        ):
            raise ValueError("invalid camera capability evidence")
        for kind in CAPABILITIES:
            value = getattr(self, kind)
            if type(value) is not bool or (
                value and not any(f"{source}:{kind}" in self.evidence for source in ("sdk", "halo"))
            ):
                raise ValueError("camera capabilities require explicit boolean evidence")


@dataclass(frozen=True)
class NativeCamera:
    scope: MediaScope
    name: str
    family: str
    kind: str
    model: str
    capabilities: CameraCapabilities
    connection_state: bool | None = None
    battery_percent: int | None = None
    observed_format: SourceFormat | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MediaScope) or not isinstance(
            self.capabilities, CameraCapabilities
        ):
            raise ValueError("invalid native camera authority")
        _literal_text(self.name, 256, "camera name")
        for value in (self.family, self.kind, self.model):
            _literal_text(value, 128, "camera metadata")
        if self.connection_state is not None and type(self.connection_state) is not bool:
            raise ValueError("invalid camera connection evidence")
        if self.battery_percent is not None and (
            type(self.battery_percent) is not int or not 0 <= self.battery_percent <= 100
        ):
            raise ValueError("invalid camera battery evidence")
        if self.observed_format is not None and not isinstance(self.observed_format, SourceFormat):
            raise ValueError("invalid observed camera format")


@dataclass(frozen=True)
class NativeInventory:
    revision: int
    cameras: tuple[NativeCamera, ...]
    complete: bool
    profile_id: str | None = None
    account_generation: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.revision) is not int
            or not 0 <= self.revision < 2**53
            or type(self.complete) is not bool
        ):
            raise ValueError("invalid native inventory state")
        if (
            not isinstance(self.cameras, tuple)
            or len(self.cameras) > MAX_NATIVE_CAMERAS
            or any(not isinstance(camera, NativeCamera) for camera in self.cameras)
        ):
            raise ValueError("invalid native inventory cameras")
        if self.profile_id is None:
            if self.account_generation is not None or self.cameras or self.complete:
                raise ValueError("unscoped inventory cannot claim account authority")
        elif (
            not valid_profile_id(self.profile_id)
            or type(self.account_generation) is not int
            or not 1 <= self.account_generation < 2**53
        ):
            raise ValueError("invalid native inventory account")
        if any(
            camera.scope.profile_id != self.profile_id
            or camera.scope.account_generation != self.account_generation
            for camera in self.cameras
        ):
            raise ValueError("mixed account inventory")
        if len({camera.scope.camera_id for camera in self.cameras}) != len(self.cameras):
            raise ValueError("duplicate camera inventory authority")
