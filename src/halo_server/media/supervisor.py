"""Bounded restart supervision for one active Ring media receiver."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from functools import partial

from halo_server.media.ring_media import (
    EncodedFrameAdapterError,
    MediaCompatibilityError,
    RingStreamReceiver,
)

_log = logging.getLogger(__name__)


@dataclass
class _AttemptState:
    active: bool = True
    received_video: bool = False
    ended: bool = False


def _end_attempt(callback, state: _AttemptState, attempt: int, error) -> None:
    if state.ended:
        return
    state.active = False
    if callback is not None:
        callback(attempt, error)
    state.ended = True


def _track_media(callback, state: _AttemptState, stop: threading.Event, *, video=False):
    if callback is None:
        return None

    def wrapped(*args, **kwargs):
        if not state.active or stop.is_set():
            return False
        accepted = callback(*args, **kwargs)
        if video and accepted is not False:
            state.received_video = True
        return accepted

    return wrapped


async def supervise_receiver(
    device,
    on_video,
    on_audio,
    stop: threading.Event,
    *,
    receiver_factory=RingStreamReceiver,
    receiver_kwargs: dict | None = None,
    retry_min: float = 1.0,
    retry_max: float = 30.0,
) -> None:
    """Keep one Ring receiver alive while its owning session remains active."""
    device_name = getattr(device, "name", "camera")
    backoff = retry_min
    attempt = 0
    base_kwargs = dict(receiver_kwargs or {})
    on_attempt = base_kwargs.pop("on_attempt", None)
    on_attempt_end = base_kwargs.pop("on_attempt_end", None)
    while not stop.is_set():
        attempt += 1
        failed = False
        state = _AttemptState()
        attempt_error = None

        attempt_kwargs = dict(base_kwargs)
        if on_attempt_end is not None:
            attempt_kwargs["on_terminating"] = partial(_end_attempt, on_attempt_end, state, attempt)
        for callback_name in (
            "on_encoded_video",
            "on_encoded_audio",
            "on_encoded_incompatible",
            "on_source_clock",
        ):
            if callback_name in attempt_kwargs:
                attempt_kwargs[callback_name] = _track_media(
                    attempt_kwargs[callback_name],
                    state,
                    stop,
                    video=callback_name == "on_encoded_video",
                )

        try:
            if on_attempt is not None:
                on_attempt(attempt)
            await receiver_factory(
                device,
                _track_media(on_video, state, stop, video=True),
                _track_media(on_audio, state, stop),
                **attempt_kwargs,
            ).receive(stop)
        except asyncio.CancelledError:
            raise
        except (MediaCompatibilityError, EncodedFrameAdapterError) as exc:
            attempt_error = exc
            raise
        except Exception as exc:  # noqa: BLE001
            attempt_error = exc
            failed = True
            _log.warning("Ring stream failed for %s: %s", device_name, exc)
        finally:
            _end_attempt(on_attempt_end, state, attempt, attempt_error)

        if stop.is_set():
            return

        delay = backoff if failed and not state.received_video else retry_min
        _log.warning("Ring stream ended for %s; retrying in %.1fs", device_name, delay)
        elapsed = 0.0
        while elapsed < delay and not stop.is_set():
            interval = min(0.1, delay - elapsed)
            await asyncio.sleep(interval)
            elapsed += interval
        if failed and not state.received_video:
            backoff = min(max(retry_min, backoff * 2), retry_max)
        else:
            backoff = retry_min
