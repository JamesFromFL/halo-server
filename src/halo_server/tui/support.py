"""Bounded owner-requested support export; never follows an existing output path."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MAX_SUPPORT_BYTES = 1024 * 1024


def write_support_report(path: Path, report: dict[str, Any]) -> None:
    """Create one private new file, including under restrictive or permissive umasks."""
    payload = (
        json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    if len(payload) > MAX_SUPPORT_BYTES:
        raise ValueError("Support report exceeds its size limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        # This process exclusively created the file. An incomplete export never
        # becomes a reusable report; never replace or unlink a preexisting file.
        os.close(descriptor)
        descriptor = -1
        path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
