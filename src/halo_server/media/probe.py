"""Bounded capability probes for Ring camera media."""

from __future__ import annotations

import contextlib
import logging
import threading
import time

from halo_server.media.profiles import EncodedMediaProfile, opus_frame_duration_ms
from halo_server.media.ring_media import RingStreamReceiver

_log = logging.getLogger(__name__)

PROBE_TIMEOUT = 45.0
PROBE_AUDIO_GRACE = 3.0
PROBE_CLEANUP_TIMEOUT = 10.0
ENCODED_PROBE_TIMEOUT = 45.0


def probe_caps(client, device) -> dict[str, int] | None:
    """Briefly stream a device to learn decoded video and audio capabilities."""
    got: dict[str, int] = {}
    done = threading.Event()
    video_ready = threading.Event()
    device_name = getattr(device, "name", "camera")

    def check() -> None:
        if "width" in got and "achannels" in got:
            done.set()

    def on_video(_raw, width, height) -> None:
        got.setdefault("width", width)
        got.setdefault("height", height)
        video_ready.set()
        check()

    def on_audio(_raw, channels, rate) -> None:
        got.setdefault("achannels", channels)
        got.setdefault("arate", rate)
        check()

    stop = threading.Event()
    receive_coro = RingStreamReceiver(device, on_video, on_audio).receive(stop)
    try:
        receiver = client.submit(receive_coro)
    except Exception as exc:  # noqa: BLE001
        receive_coro.close()
        _log.warning("could not start caps probe for %s: %s", device_name, exc)
        return None

    def receiver_done() -> bool:
        check_done = getattr(receiver, "done", None)
        return bool(check_done()) if callable(check_done) else False

    deadline = time.monotonic() + PROBE_TIMEOUT
    while not video_ready.is_set() and not receiver_done():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        video_ready.wait(timeout=min(0.1, remaining))
    if video_ready.is_set() and not done.is_set():
        audio_deadline = min(deadline, time.monotonic() + PROBE_AUDIO_GRACE)
        while not done.is_set() and not receiver_done():
            remaining = audio_deadline - time.monotonic()
            if remaining <= 0:
                break
            done.wait(timeout=min(0.1, remaining))

    stop.set()
    try:
        receiver.result(timeout=PROBE_CLEANUP_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(Exception):
            receiver.cancel()
        _log.warning("timed out stopping caps probe for %s", device_name)
        return None
    except Exception as exc:  # noqa: BLE001
        _log.warning("caps probe cleanup failed for %s: %s", device_name, exc)
        return None

    if "width" not in got:
        return None
    return {
        "width": got["width"],
        "height": got["height"],
        "achannels": got.get("achannels", 2),
        "arate": got.get("arate", 48000),
    }


def probe_encoded_media(client, device) -> EncodedMediaProfile | None:
    """Return the H.264 and Opus profile after one clean frame of each arrives."""
    video_ready = threading.Event()
    audio_ready = threading.Event()
    incompatible = threading.Event()
    stop = threading.Event()
    device_name = getattr(device, "name", "camera")
    frame_duration_ms: float | None = None

    def on_encoded_video(_data: bytes, _timestamp: int) -> None:
        video_ready.set()

    def on_encoded_audio(data: bytes, _timestamp: int) -> None:
        nonlocal frame_duration_ms
        frame_duration_ms = opus_frame_duration_ms(data)
        if frame_duration_ms is None:
            _log.warning("encoded transport received an empty Opus packet for %s", device_name)
            incompatible.set()
            return
        audio_ready.set()

    def on_incompatible(kind: str, mime_type: str) -> None:
        _log.warning(
            "encoded transport rejected %s codec for %s: %s",
            kind,
            device_name,
            mime_type,
        )
        incompatible.set()

    receive_coro = RingStreamReceiver(
        device,
        None,
        None,
        on_encoded_video=on_encoded_video,
        on_encoded_audio=on_encoded_audio,
        decode_video=False,
        decode_audio=False,
        on_encoded_incompatible=on_incompatible,
    ).receive(stop)
    try:
        receiver = client.submit(receive_coro)
    except Exception as exc:  # noqa: BLE001
        receive_coro.close()
        _log.warning("could not start encoded transport probe for %s: %s", device_name, exc)
        return None

    deadline = time.monotonic() + ENCODED_PROBE_TIMEOUT
    while not incompatible.is_set() and not (video_ready.is_set() and audio_ready.is_set()):
        check = getattr(receiver, "done", None)
        if callable(check) and check():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        incompatible.wait(timeout=min(0.1, remaining))

    stop.set()
    try:
        receiver.result(timeout=PROBE_CLEANUP_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(Exception):
            receiver.cancel()
        _log.warning("timed out stopping encoded transport probe for %s", device_name)
        return None
    except Exception as exc:  # noqa: BLE001
        _log.warning("encoded transport probe failed for %s: %s", device_name, exc)
        return None

    compatible = video_ready.is_set() and audio_ready.is_set() and not incompatible.is_set()
    if not compatible or frame_duration_ms is None:
        _log.warning(
            "encoded transport probe did not receive H.264 and Opus for %s",
            device_name,
        )
        return None
    return EncodedMediaProfile(opus_frame_duration_ms=frame_duration_ms)
