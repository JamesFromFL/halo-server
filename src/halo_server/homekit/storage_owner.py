"""Exclusive parent ownership and a pinned inherited HAP pairing directory."""

from __future__ import annotations

import fcntl
import os
import stat
import sys
from pathlib import Path

from halo_server.paths import PathSecurityError, open_private_directory


class PairingStorageError(RuntimeError):
    """Pairing storage could not be owned without weakening its path boundary."""


class PairingDirectoryOwner:
    def __init__(self, parent_fd: int, lock_fd: int, directory_fd: int):
        self._parent_fd, self._lock_fd, self.directory_fd = parent_fd, lock_fd, directory_fd

    @classmethod
    def acquire(cls, directory: Path) -> PairingDirectoryOwner:
        if sys.platform != "linux" or not Path("/proc/self/fd").is_dir():
            raise PairingStorageError("Native pairing directory handles require qualified Linux")
        parent_fd = lock_fd = directory_fd = -1
        acquired = False
        try:
            parent_fd = open_private_directory(directory.parent)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
            try:
                lock_fd = os.open(
                    ".pairings.owner.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd
                )
            except FileExistsError:
                lock_fd = os.open(".pairings.owner.lock", flags, dir_fd=parent_fd)
            else:
                os.fchmod(lock_fd, 0o600)
            info = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise PairingStorageError("Pairing ownership lock is not private")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            directory_fd = os.open(
                directory.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
            info = os.fstat(directory_fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise PairingStorageError("Pairing directory is not private")
            owner = cls(parent_fd, lock_fd, directory_fd)
            acquired = True
            return owner
        except (OSError, ValueError, PathSecurityError) as exc:
            raise PairingStorageError("Pairing storage is unavailable or already owned") from exc
        finally:
            if not acquired:
                for descriptor in (directory_fd, lock_fd, parent_fd):
                    if descriptor >= 0:
                        os.close(descriptor)

    @property
    def lock_fd(self) -> int:
        return self._lock_fd

    def close(self) -> None:
        for name in ("directory_fd", "_lock_fd", "_parent_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)
