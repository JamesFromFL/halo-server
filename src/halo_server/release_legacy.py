"""Bounded legacy-daemon retirement evidence without killing unmanaged processes.

New releases retain a shared lifetime gate in every owner. A pre-gate daemon can
only provide its private Unix peer PID and actual parent-process exit evidence;
that cannot prove retirement of already orphaned legacy descendants.
"""

from __future__ import annotations

import asyncio
import errno
import os
import select
import socket
import stat
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from halo_server.paths import PathSecurityError, open_private_directory
from halo_server.release_activation import ActivationError
from halo_server.tui.client import AdminClientError

PROBE_SECONDS = 2.0
EXIT_SECONDS = 2.0


@dataclass
class LegacyOwner:
    """A pidfd remains tied to its observed process even after PID reuse."""

    pid: int
    descriptor: int

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor, self.descriptor = self.descriptor, -1
            os.close(descriptor)


def _unverified() -> ActivationError:
    return ActivationError("Legacy daemon retirement could not be verified", code="stop_unverified")


async def capture_owner(client) -> LegacyOwner | None:
    """Capture the owner of the pinned private admin endpoint before service stop.

    A fake client without a path has no OS owner evidence, but still receives the
    strict post-stop status check. Missing/refused endpoints are not live owners.
    Unsafe paths, unsupported peer credentials and ambiguous failures refuse the
    operation before it can stop a service or mutate release state.
    """
    path = getattr(client, "socket_path", None)
    if path is None:
        return None
    if not isinstance(path, (str, Path)):
        raise _unverified()
    path = Path(path)
    if not path.is_absolute() or sys.platform != "linux" or not hasattr(os, "pidfd_open"):
        raise _unverified()
    directory = -1
    peer = None
    try:
        try:
            directory = open_private_directory(path.parent)
        except PathSecurityError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return None
            raise _unverified() from error
        try:
            metadata = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise _unverified()
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.setblocking(False)
        try:
            async with asyncio.timeout(PROBE_SECONDS):
                await asyncio.get_running_loop().sock_connect(
                    peer, f"/proc/self/fd/{directory}/{path.name}"
                )
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                return None
            raise _unverified() from error
        credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", credentials)
        if uid != os.getuid() or pid <= 0 or pid == os.getpid():
            raise _unverified()
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            # The originally observed peer already exited. PID reuse during
            # pidfd_open can only make a later wait conservatively refuse; it
            # cannot make an original still-live process disappear.
            return None
        try:
            os.set_inheritable(descriptor, False)
            return LegacyOwner(pid, descriptor)
        except BaseException:
            os.close(descriptor)
            raise
    except (OSError, TimeoutError, AttributeError, ValueError, struct.error) as error:
        raise _unverified() from error
    finally:
        if peer is not None:
            peer.close()
        if directory >= 0:
            os.close(directory)


async def verify_stopped(client, owner: LegacyOwner | None) -> None:
    """Require unavailable admin plus captured parent exit; retain caller ownership."""
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            await client.status()
    except AdminClientError as error:
        if error.code != "daemon_unavailable":
            raise _unverified() from error
    except (OSError, TimeoutError, RuntimeError, ValueError) as error:
        raise _unverified() from error
    else:
        raise ActivationError("An unmanaged daemon is still responding", code="active_unmanaged")
    if owner is None:
        return
    if not isinstance(owner, LegacyOwner) or owner.descriptor < 0:
        raise _unverified()
    loop = asyncio.get_running_loop()
    exited = loop.create_future()

    def ready():
        if not exited.done():
            exited.set_result(None)

    registered = False
    try:
        if select.select([owner.descriptor], [], [], 0)[0]:
            return
        loop.add_reader(owner.descriptor, ready)
        registered = True
        try:
            async with asyncio.timeout(EXIT_SECONDS):
                await exited
        except TimeoutError as error:
            raise ActivationError(
                "The previous daemon process is still alive", code="active_unmanaged"
            ) from error
    except (OSError, ValueError) as error:
        raise _unverified() from error
    finally:
        if registered:
            loop.remove_reader(owner.descriptor)
