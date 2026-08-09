"""Version-pinned Ring WebRTC camera-speaker control."""

from __future__ import annotations

import asyncio
import json
from importlib import metadata as importlib_metadata

RING_DOORBELL_SPEAKER_VERSIONS = frozenset({"0.9.14"})
VALIDATED_INTERCOM_VIDEO_KINDS = frozenset({"stickup_cam_mini_v3"})
DEFAULT_SPEAKER_TIMEOUT = 5.0
_SESSION_POLL_SECONDS = 0.01
_SPEAKER_STATE_ATTRIBUTE = "_halo_camera_speaker_activated"


class RingSpeakerControlError(RuntimeError):
    """Raised when Halo cannot control a Ring WebRTC camera speaker."""


def supports_ring_camera_intercom(device) -> bool:
    """Return whether one camera exposes Halo's validated speaker contract."""
    if _ring_doorbell_version() not in RING_DOORBELL_SPEAKER_VERSIONS:
        return False
    try:
        kind = getattr(device, "kind", None)
        if kind not in VALIDATED_INTERCOM_VIDEO_KINDS and not device.has_capability("video"):
            return False
        return all(
            callable(getattr(device, attribute, None))
            for attribute in (
                "generate_async_webrtc_stream",
                "on_webrtc_candidate",
                "close_webrtc_stream",
                "get_ice_servers",
            )
        )
    except Exception:
        return False


async def activate_ring_camera_speaker(
    device,
    halo_session_id: str,
    *,
    timeout: float = DEFAULT_SPEAKER_TIMEOUT,
) -> bool:
    """Activate the speaker on a validated python-ring-doorbell stream.

    Ring latches speaker activation for the lifetime of a live-view session.
    The session close resets that state, so sending ``stealth_mode: true`` when
    an intercom lease ends can mute audio that the camera has not rendered yet.

    Returns ``True`` when a command was sent and ``False`` when Halo already
    activated the speaker for this stream.
    """
    version = _ring_doorbell_version()
    if version not in RING_DOORBELL_SPEAKER_VERSIONS:
        raise RingSpeakerControlError(
            f"ring-doorbell {version} is not validated for camera speaker control"
        )

    streams = getattr(device, "_webrtc_streams", None)
    if not isinstance(streams, dict):
        raise RingSpeakerControlError("Ring camera WebRTC stream registry is unavailable")
    stream = streams.get(halo_session_id)
    if stream is None:
        raise RingSpeakerControlError("Ring camera WebRTC stream is unavailable")

    offered = getattr(stream, "_offered_event", None)
    answered = getattr(stream, "_sdp_answer_event", None)
    get_session_message = getattr(stream, "get_session_message", None)
    if (
        offered is None
        or not callable(getattr(offered, "wait", None))
        or answered is None
        or not callable(getattr(answered, "wait", None))
        or not callable(get_session_message)
    ):
        raise RingSpeakerControlError("Ring camera speaker signaling contract changed")

    try:
        async with asyncio.timeout(timeout):
            await offered.wait()
            await answered.wait()
            while getattr(stream, "session_id", None) is None:
                if not getattr(stream, "is_alive", False):
                    raise RingSpeakerControlError("Ring camera WebRTC stream closed")
                await asyncio.sleep(_SESSION_POLL_SECONDS)
    except TimeoutError as exc:
        raise RingSpeakerControlError("Ring camera speaker signaling timed out") from exc

    if not getattr(stream, "is_alive", False):
        raise RingSpeakerControlError("Ring camera WebRTC stream closed")
    if getattr(stream, _SPEAKER_STATE_ATTRIBUTE, False):
        return False

    websocket = getattr(stream, "websocket", None)
    send = getattr(websocket, "send", None)
    if not callable(send):
        raise RingSpeakerControlError("Ring camera WebRTC signaling socket is unavailable")

    message = get_session_message(
        "camera_options",
        {"stealth_mode": False},
    )
    try:
        await send(json.dumps(message, separators=(",", ":")))
    except Exception as exc:
        raise RingSpeakerControlError("Ring camera speaker command failed") from exc
    setattr(stream, _SPEAKER_STATE_ATTRIBUTE, True)
    return True


def _ring_doorbell_version() -> str:
    try:
        return importlib_metadata.version("ring-doorbell")
    except importlib_metadata.PackageNotFoundError:
        return "not-installed"
