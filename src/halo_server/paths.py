"""Filesystem locations and private-directory validation for Halo Server."""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path


class PathSecurityError(RuntimeError):
    """Raised when a sensitive Halo Server path is not private."""


@dataclass(frozen=True)
class ServerPaths:
    """Resolved paths used by the daemon and its local admin clients."""

    state_directory: Path
    runtime_directory: Path

    @classmethod
    def from_environment(cls) -> ServerPaths:
        state_home = _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state")
        return cls(
            state_directory=state_home / "halo-server",
            runtime_directory=_runtime_directory(),
        )

    @property
    def credentials_file(self) -> Path:
        return self.state_directory / "credentials.json"

    @property
    def admin_socket(self) -> Path:
        return self.runtime_directory / "admin.sock"

    @property
    def integration_directory(self) -> Path:
        return self.state_directory / "integrations"

    @property
    def configuration_file(self) -> Path:
        return self.state_directory / "configuration.json"

    @property
    def homekit_directory(self) -> Path:
        return self.state_directory / "homekit"

    @property
    def homekit_configuration_file(self) -> Path:
        return self.homekit_directory / "config.json"

    @property
    def homekit_registry_file(self) -> Path:
        return self.homekit_directory / "registry.json"

    @property
    def homekit_audio_settings_file(self) -> Path:
        return self.homekit_directory / "audio-settings.json"

    @property
    def homekit_recording_settings_file(self) -> Path:
        return self.homekit_directory / "recording-settings.json"

    @property
    def homekit_pairing_maintenance_file(self) -> Path:
        return self.homekit_directory / "pairing-maintenance.json"

    @property
    def homekit_pairing_directory(self) -> Path:
        return self.homekit_directory / "pairings"

    @property
    def homekit_runtime_directory(self) -> Path:
        return self.runtime_directory / "homekit"

    def prepare(self) -> None:
        ensure_private_directory(self.state_directory)
        ensure_private_directory(self.runtime_directory)

    def prepare_homekit(self) -> None:
        """Prepare owner-only directories, without opening any HAP state files."""
        self.prepare()
        for path in (
            self.homekit_directory,
            self.homekit_pairing_directory,
            self.homekit_runtime_directory,
        ):
            ensure_private_directory(path)


def ensure_private_directory(path: Path) -> None:
    """Create/secure an owned directory using pinned, no-follow directory FDs."""
    descriptor = open_private_directory(path, create=True, repair_permissions=True)
    os.close(descriptor)


def open_private_directory(
    path: Path, *, create: bool = False, repair_permissions: bool = False
) -> int:
    """Return an owned FD; reject symlinks in every component of the path.

    Ancestors may be ordinary system/user directories. Only the requested private
    leaf must be current-owner/private, and only that leaf may be chmodded.
    The caller owns and must close the returned descriptor.
    """
    absolute = Path(os.path.abspath(path.expanduser()))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(absolute.anchor, flags)
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        path_stat = os.fstat(descriptor)
        if path_stat.st_uid != os.getuid():
            raise PathSecurityError("Private directory is not owned by this user")
        if repair_permissions:
            os.fchmod(descriptor, 0o700)
            path_stat = os.fstat(descriptor)
        if path_stat.st_mode & 0o077:
            raise PathSecurityError("Private directory permits group or other access")
        result, descriptor = descriptor, -1
        return result
    except OSError as exc:
        raise PathSecurityError(
            "Private path is not a directory or cannot be opened safely"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _xdg_path(variable: str, fallback: Path) -> Path:
    configured = os.environ.get(variable)
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path
    return fallback


def _runtime_directory() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        path = Path(configured)
        if path.is_absolute():
            return path / "halo-server"
    standard = Path("/run/user") / str(os.getuid())
    if standard.is_dir():
        return standard / "halo-server"
    return Path("/tmp") / f"halo-server-runtime-{os.getuid()}"
