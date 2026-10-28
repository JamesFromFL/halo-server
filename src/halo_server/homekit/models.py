"""Immutable authority and privacy inputs for native HomeKit consumers."""

from __future__ import annotations

from dataclasses import dataclass

from halo_server.account_scope import valid_profile_id


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
        if (
            not isinstance(self.camera_id, str)
            or not self.camera_id
            or len(self.camera_id) > 128
            or any(ord(char) < 32 for char in self.camera_id)
        ):
            raise ValueError("invalid camera identity")
        for value in (self.account_generation, self.device_generation, self.privacy_generation):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < 2**53:
                raise ValueError("scope generations must be positive safe integers")


@dataclass(frozen=True)
class CameraPolicy:
    """An unsynchronized adapter has no authority to acquire or deliver media."""

    synchronized: bool = False
    streaming: bool = False
    snapshots: bool = False
    recording: bool = False
    recording_audio: bool = False
    talkback: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in vars(self).values()):
            raise ValueError("camera policy values must be booleans")
        if self.recording_audio and not self.recording:
            raise ValueError("recording audio requires recording permission")

    def permits(self, operation: str) -> bool:
        if operation not in {"streaming", "snapshots", "recording", "recording_audio", "talkback"}:
            raise ValueError("unknown camera operation")
        return self.synchronized and getattr(self, operation)
