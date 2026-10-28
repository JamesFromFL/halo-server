"""Fixed Linux child CPU limits, without changing the service's affinity."""

from __future__ import annotations

import os
import threading

_ENV_CPU = "HALO_NATIVE_WORKER_CPU"
_LOCK = threading.Lock()
_NEXT = 0


def worker_environment() -> dict[str, str]:
    """Select one currently allowed CPU round-robin for a new isolated child."""
    global _NEXT
    allowed = tuple(sorted(os.sched_getaffinity(0)))
    if not allowed:
        raise RuntimeError("No native worker CPU is available")
    with _LOCK:
        selected = allowed[_NEXT % len(allowed)]
        _NEXT += 1
    return {
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        _ENV_CPU: str(selected),
    }


def apply_child_cpu_limit() -> int:
    """Call only in the isolated child, before importing native media libraries."""
    allowed = os.sched_getaffinity(0)
    value = os.environ.get(_ENV_CPU)
    if value is None:
        # Direct isolated native probes still obey the same one-CPU contract.
        if not allowed:
            raise RuntimeError("No native worker CPU is available")
        selected = min(allowed)
    else:
        if not value.isascii() or not value.isdecimal() or len(value) > 10:
            raise RuntimeError("Invalid native worker CPU")
        selected = int(value)
        if selected not in allowed:
            raise RuntimeError("Native worker CPU is no longer allowed")
    os.sched_setaffinity(0, {selected})
    if os.sched_getaffinity(0) != {selected}:
        raise RuntimeError("Native worker CPU limit was not applied")
    return selected
