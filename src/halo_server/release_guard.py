"""Pure-stdlib lifetime release gate, shared by launcher and all owned children."""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

GUARD_NAME = "release-activation.lock"
GUARD_FD_ENV = "HALO_RELEASE_GUARD_FD"
GUARD_STATE_ENV = "HALO_RELEASE_STATE_DIRECTORY"
_SOURCE_LOCK = threading.RLock()
_SOURCE_GUARD = None
_SOURCE_REFS = 0


class ReleaseGuardError(RuntimeError):
    """The canonical release gate is unsafe, unavailable, or still owned."""


def _private_directory(path: Path, *, create: bool = False) -> int:
    path = Path(os.path.abspath(path.expanduser()))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except FileNotFoundError:
                if not create:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ReleaseGuardError("Release state directory is not private")
        result, descriptor = descriptor, -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_file(descriptor: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size != 0
    ):
        raise ReleaseGuardError("Release gate is not a private owned lock")
    return metadata


@dataclass
class ReleaseGuard:
    descriptor: int
    state_directory: Path

    @classmethod
    def acquire(
        cls,
        state_directory: Path,
        *,
        exclusive: bool = False,
        nonblocking: bool = False,
        create: bool = False,
    ) -> ReleaseGuard:
        directory = descriptor = -1
        try:
            state_directory = Path(os.path.abspath(Path(state_directory).expanduser()))
            directory = _private_directory(state_directory, create=create)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            created = False
            if create:
                try:
                    descriptor = os.open(
                        GUARD_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
                    )
                    os.fchmod(descriptor, 0o600)
                    created = True
                except FileExistsError:
                    pass
            if descriptor < 0:
                descriptor = os.open(GUARD_NAME, flags, dir_fd=directory)
            _validate_file(descriptor)
            if created:
                os.fsync(descriptor)
                os.fsync(directory)
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if nonblocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(descriptor, operation)
            result = cls(descriptor, state_directory)
            descriptor = -1
            return result
        except OSError as error:
            raise ReleaseGuardError(
                "Release gate is unavailable or a process is still running"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if directory >= 0:
                os.close(directory)

    def fileno(self) -> int:
        if self.descriptor < 0:
            raise ReleaseGuardError("Release gate is closed")
        return self.descriptor

    def environment(self) -> dict[str, str]:
        return {GUARD_FD_ENV: str(self.fileno()), GUARD_STATE_ENV: str(self.state_directory)}

    def inherit(self) -> dict[str, str]:
        """Call immediately before exec; child subprocesses must also use pass_fds."""
        os.set_inheritable(self.fileno(), True)
        return self.environment()

    def close(self) -> None:
        if self.descriptor >= 0:
            # Do not LOCK_UN: inherited children share this open file description.
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> ReleaseGuard:
        return self

    def __exit__(self, *_):
        self.close()


def inherited_guard_fds(*, state_directory: Path | None = None) -> tuple[int, ...]:
    """Validate the inherited gate without duplicating or releasing its ownership."""
    value, state = os.environ.get(GUARD_FD_ENV), os.environ.get(GUARD_STATE_ENV)
    if value is None and state is None:
        with _SOURCE_LOCK:
            if _SOURCE_GUARD is None:
                return ()
            value, state = str(_SOURCE_GUARD.fileno()), str(_SOURCE_GUARD.state_directory)
    if (
        value is None
        or state is None
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 5
        or not 3 <= int(value) <= 65535
        or not Path(state).is_absolute()
    ):
        raise ReleaseGuardError("Inherited release gate is invalid")
    if state_directory is not None and Path(os.path.abspath(state_directory)) != Path(state):
        raise ReleaseGuardError("Managed release state directory changed")
    directory = current = -1
    try:
        descriptor = int(value)
        inherited = _validate_file(descriptor)
        directory = _private_directory(Path(state))
        current = os.open(GUARD_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        metadata = _validate_file(current)
        if (inherited.st_dev, inherited.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ReleaseGuardError("Inherited release gate identity changed")
        # This also refuses a forged, unlocked FD while an activation owns EX.
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return (descriptor,)
    except (OSError, ValueError) as error:
        raise ReleaseGuardError("Inherited release gate is unavailable") from error
    finally:
        if current >= 0:
            os.close(current)
        if directory >= 0:
            os.close(directory)


def inherited_guard_environment() -> dict[str, str]:
    if not inherited_guard_fds():
        return {}
    value, state = os.environ.get(GUARD_FD_ENV), os.environ.get(GUARD_STATE_ENV)
    if value is not None or state is not None:
        return {GUARD_FD_ENV: value, GUARD_STATE_ENV: state}
    with _SOURCE_LOCK:
        if _SOURCE_GUARD is None:
            raise ReleaseGuardError("Source release ownership changed")
        return _SOURCE_GUARD.environment()


class SourceGuardLease:
    """One service's reference; closing it never unlocks inherited children."""

    def __init__(self, *, source):
        self.source = source
        self.closed = False

    def close(self):
        global _SOURCE_GUARD, _SOURCE_REFS
        with _SOURCE_LOCK:
            if self.closed:
                return
            self.closed = True
            if not self.source:
                return
            if _SOURCE_GUARD is None or _SOURCE_REFS <= 0:
                raise ReleaseGuardError("Source release owner was lost")
            _SOURCE_REFS -= 1
            if _SOURCE_REFS == 0:
                owner, _SOURCE_GUARD = _SOURCE_GUARD, None
                owner.close()


def acquire_source_guard(state_directory: Path) -> SourceGuardLease:
    """Cover manually started source daemons without changing process environment.

    Managed daemons already own the launcher's actual process lifetime FD. A
    source daemon owns a process-local shared description with explicit service
    references, exported only through child bootstrap helpers.
    """
    global _SOURCE_GUARD, _SOURCE_REFS
    state = Path(os.path.abspath(Path(state_directory).expanduser()))
    with _SOURCE_LOCK:
        if _SOURCE_GUARD is not None and _SOURCE_GUARD.state_directory != state:
            raise ReleaseGuardError("Another canonical source authority is active")
        if os.environ.get(GUARD_FD_ENV) is not None or os.environ.get(GUARD_STATE_ENV) is not None:
            inherited_guard_fds(state_directory=state)
            return SourceGuardLease(source=False)
        if _SOURCE_GUARD is None:
            _SOURCE_GUARD = ReleaseGuard.acquire(state, nonblocking=True, create=True)
        else:
            inherited_guard_fds(state_directory=state)
        _SOURCE_REFS += 1
        return SourceGuardLease(source=True)
