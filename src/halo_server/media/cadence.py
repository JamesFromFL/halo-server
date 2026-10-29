"""Integer source-time phase selection without accumulating rounding error."""

SOURCE_RATE = 90000
IDR_INTERVAL = 2 * SOURCE_RATE


class SourceCadence:
    """Select at most one source frame per nearest nominal output phase.

    A half-period tie remains in the earlier phase. Small source clock jitter
    therefore does not turn every other nominal frame into a rate violation.
    This selects existing frames; it never duplicates or retimestamps them.
    """

    def __init__(self, fps: int):
        if type(fps) is not int or not 1 <= fps <= 30:
            raise ValueError("Invalid video cadence")
        self.fps = fps
        self._origin = None
        self._phase = -1

    def accept(self, timestamp: int) -> bool:
        if type(timestamp) is not int or not 0 <= timestamp < 2**53:
            raise ValueError("Invalid source video timestamp")
        if self._origin is None:
            self._origin = timestamp
        elapsed = timestamp - self._origin
        if elapsed < 0:
            return False
        phase = (elapsed * self.fps + SOURCE_RATE // 2 - 1) // SOURCE_RATE
        if phase <= self._phase:
            return False
        self._phase = phase
        return True
