"""Filesystem locations and private-directory validation for Halo Server."""

from __future__ import annotations

import os
import stat
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

    def prepare(self) -> None:
        ensure_private_directory(self.state_directory)
        ensure_private_directory(self.runtime_directory)


def ensure_private_directory(path: Path) -> None:
    """Create and validate an owner-only directory without following a symlink."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path_stat = path.lstat()
    except OSError as exc:
        raise PathSecurityError(f"Cannot prepare private directory: {path}") from exc
    if not stat.S_ISDIR(path_stat.st_mode):
        raise PathSecurityError(f"Private path is not a directory: {path}")
    if path_stat.st_uid != os.getuid():
        raise PathSecurityError(f"Private directory is not owned by this user: {path}")
    try:
        path.chmod(0o700)
        path_stat = path.lstat()
    except OSError as exc:
        raise PathSecurityError(f"Cannot secure private directory: {path}") from exc
    if path_stat.st_mode & 0o077:
        raise PathSecurityError(f"Private directory permits group or other access: {path}")


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
