"""Bounded restart supervision for one active Ring media receiver."""

from __future__ import annotations

import asyncio
import logging
import threading

from halo_server.media.ring_media import RingStreamReceiver

_log = logging.getLogger(__name__)


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
    while not stop.is_set():
        failed = False
        received_media = False

        def track_media(callback):
            if callback is None:
                return None

            def wrapped(*args, **kwargs):
                nonlocal received_media
                received_media = True
                return callback(*args, **kwargs)

            return wrapped

        attempt_kwargs = dict(receiver_kwargs or {})
        for callback_name in ("on_encoded_video", "on_encoded_audio"):
            if callback_name in attempt_kwargs:
                attempt_kwargs[callback_name] = track_media(attempt_kwargs[callback_name])

        try:
            await receiver_factory(
                device,
                track_media(on_video),
                track_media(on_audio),
                **attempt_kwargs,
            ).receive(stop)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            failed = True
            _log.warning("Ring stream failed for %s: %s", device_name, exc)

        if stop.is_set():
            return

        delay = backoff if failed and not received_media else retry_min
        _log.warning("Ring stream ended for %s; retrying in %.1fs", device_name, delay)
        elapsed = 0.0
        while elapsed < delay and not stop.is_set():
            interval = min(0.1, delay - elapsed)
            await asyncio.sleep(interval)
            elapsed += interval
        if failed and not received_media:
            backoff = min(max(retry_min, backoff * 2), retry_max)
        else:
            backoff = retry_min
