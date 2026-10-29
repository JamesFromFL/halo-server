"""Fixed-schema, constant-space counters; never payloads or synchronous log I/O."""

from __future__ import annotations

import bisect
import json
import math
import re
import threading

_LIMIT = 2**53 - 1
_BOUNDS_US = (1000, 5000, 10000, 20000, 40000, 60000, 100000, 250000, 500000, 1000000, 5000000)
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class StreamMetrics:
    """A schema is declared by code, never expanded by incoming media/feedback.

    Updates do not emit logs, call clocks, or acquire media ownership locks.
    Snapshots contain only bounded numbers, fixed field names, and histogram
    bucket boundaries. Timers are seconds at the API and microseconds on wire.
    """

    def __init__(self, *, counters=(), maxima=(), timings=()):
        groups = tuple(tuple(group) for group in (counters, maxima, timings))
        names = tuple(name for group in groups for name in group)
        if len(names) > 160 or any(
            type(name) is not str or not _NAME.fullmatch(name) for name in names
        ):
            raise ValueError("Invalid stream metric schema")
        if len(set(names)) != len(names):
            raise ValueError("Duplicate stream metric name")
        self._lock = threading.Lock()
        self._counters = dict.fromkeys(groups[0], 0)
        self._maxima = dict.fromkeys(groups[1], 0)
        self._timings = {name: [0, 0, 0, [0] * (len(_BOUNDS_US) + 1)] for name in groups[2]}
        self._overflow = False

    @staticmethod
    def _integer(value):
        return type(value) is int and value >= 0

    def _bounded(self, value):
        if value > _LIMIT:
            self._overflow = True
            return _LIMIT
        return value

    def count(self, name, amount=1):
        if not self._integer(amount):
            return
        with self._lock:
            if name in self._counters:
                self._counters[name] = self._bounded(self._counters[name] + amount)

    def maximum(self, name, value):
        if not self._integer(value):
            return
        with self._lock:
            if name in self._maxima:
                self._maxima[name] = self._bounded(max(value, self._maxima[name]))

    def observe(self, name, seconds):
        if type(seconds) not in (int, float) or seconds < 0:
            return
        if type(seconds) is float and not math.isfinite(seconds):
            return
        # Clamp before conversion, including large finite adversarial values.
        micros = _LIMIT + 1 if seconds > _LIMIT / 1_000_000 else round(seconds * 1_000_000)
        with self._lock:
            if name not in self._timings:
                return
            value = self._timings[name]
            value[0] = self._bounded(value[0] + 1)
            value[1] = self._bounded(value[1] + micros)
            value[2] = self._bounded(max(value[2], micros))
            bucket = bisect.bisect_left(_BOUNDS_US, micros)
            value[3][bucket] = self._bounded(value[3][bucket] + 1)

    def snapshot(self):
        with self._lock:
            return {
                "counters": dict(self._counters),
                "maxima": dict(self._maxima),
                "timings": {
                    name: {
                        "count": value[0],
                        "sum_us": value[1],
                        "max_us": value[2],
                        "buckets": list(value[3]),
                    }
                    for name, value in self._timings.items()
                },
                "bucket_bounds_us": list(_BOUNDS_US),
                "overflow": self._overflow,
            }


def safe_diagnostics(owner):
    """Optional diagnostic failures must not break trusted owner status."""
    try:
        getter = getattr(owner, "diagnostics", None)
        value = getter() if callable(getter) else None
        return value if isinstance(value, dict) else None
    except Exception:
        return None


class DiagnosticBudget:
    """Bound optional trusted-owner diagnostics within one local status response.

    Core session rows remain available when detailed metrics are omitted. The
    getter call cap also bounds snapshot work, independently of serialized size.
    This is a serialization budget, not a sanitizer for untrusted data.
    """

    MAX_OWNERS = 16
    MAX_BYTES = 128 * 1024

    def __init__(self):
        self._attempted = self._included = self._omitted = self._unavailable = self._bytes = 0

    def get(self, owner):
        if self._attempted >= self.MAX_OWNERS or self._bytes >= self.MAX_BYTES:
            self._omitted += 1
            return None
        self._attempted += 1
        value = safe_diagnostics(owner)
        if value is None:
            self._unavailable += 1
            return None
        try:
            size = len(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())
        except (TypeError, ValueError, OverflowError, RecursionError):
            self._unavailable += 1
            return None
        if self._bytes + size > self.MAX_BYTES:
            self._omitted += 1
            return None
        self._bytes += size
        self._included += 1
        return value

    def report(self):
        return {
            "included": self._included,
            "omitted": self._omitted,
            "unavailable": self._unavailable,
            "bytes": self._bytes,
            "maximum_owners": self.MAX_OWNERS,
            "maximum_bytes": self.MAX_BYTES,
        }
