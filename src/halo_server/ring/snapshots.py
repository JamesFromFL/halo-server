"""Bounded validation for Ring JPEG snapshots."""

from __future__ import annotations

from typing import Any

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


def validated_snapshot_bytes(value: Any) -> bytes | None:
    """Return one bounded JPEG byte string or ``None`` for invalid input."""
    if not isinstance(value, bytes):
        return None
    image = value
    if (
        not image
        or len(image) > MAX_SNAPSHOT_BYTES
        or not image.startswith(b"\xff\xd8")
        or not image.endswith(b"\xff\xd9")
    ):
        return None
    return image
