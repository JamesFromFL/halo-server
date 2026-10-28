"""Immutable operating settings and private revision-checked JSON persistence."""

from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import json
import os
import secrets
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from halo_server.paths import PathSecurityError, open_private_directory

CONFIGURATION_VERSION = 1
MAX_CONFIGURATION_BYTES = 64 * 1024
MAX_REVISION = 2**53 - 1
_locks_guard = threading.Lock()
_path_locks: dict[Path, threading.RLock] = {}


class ConfigurationError(RuntimeError):
    """Configuration is invalid, unsafe, unreadable or could not be saved."""

    def __init__(self, message: str, *, possibly_saved: bool = False) -> None:
        super().__init__(message)
        self.possibly_saved = possibly_saved


class ConfigurationConflictError(ConfigurationError):
    """Another writer superseded the revision used for this decision."""


class ServerMode(StrEnum):
    COMPATIBILITY = "compatibility"
    HOMEKIT = "homekit"
    DUAL = "dual"
    RING_ONLY = "ring-only"


def is_loopback_host(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or "%" in value:
        return False
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ServerConfiguration:
    version: int = CONFIGURATION_VERSION
    revision: int = 0
    mode: ServerMode = ServerMode.COMPATIBILITY
    plugin_host: str = "127.0.0.1"
    plugin_port: int = 18443
    rtsp_host: str = "127.0.0.1"
    rtsp_port: int = 18554

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != CONFIGURATION_VERSION:
            raise ConfigurationError("Server configuration version is unsupported")
        if type(self.revision) is not int or not 0 <= self.revision <= MAX_REVISION:
            raise ConfigurationError("Server configuration revision is invalid")
        try:
            object.__setattr__(self, "mode", ServerMode(self.mode))
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("Server configuration mode is invalid") from exc
        for host in (self.plugin_host, self.rtsp_host):
            if not is_loopback_host(host):
                raise ConfigurationError(
                    "Compatibility hosts must be literal loopback or localhost"
                )
        for port in (self.plugin_port, self.rtsp_port):
            if type(port) is not int or not 0 <= port <= 65535:
                raise ConfigurationError("Compatibility ports must be integers from 0 to 65535")

    @property
    def compatibility_enabled(self) -> bool:
        return self.mode in {ServerMode.COMPATIBILITY, ServerMode.DUAL}

    @property
    def homekit_enabled(self) -> bool:
        return self.mode in {ServerMode.HOMEKIT, ServerMode.DUAL}

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "revision": self.revision,
            "mode": self.mode.value,
            "plugin_host": self.plugin_host,
            "plugin_port": self.plugin_port,
            "rtsp_host": self.rtsp_host,
            "rtsp_port": self.rtsp_port,
        }


class ConfigurationStore:
    """Bounded private JSON, serialized by process lock and exclusive flock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        with _locks_guard:
            self._process_lock = _path_locks.setdefault(self.path, threading.RLock())

    def load(self) -> ServerConfiguration:
        with self._transaction() as directory:
            return self._read_locked(directory)

    def save(
        self,
        configuration: ServerConfiguration,
        *,
        expected_revision: int | None = None,
    ) -> ServerConfiguration:
        if not isinstance(configuration, ServerConfiguration):
            raise ConfigurationError("Server configuration must be a validated snapshot")
        # Revalidation prevents a forged/mutated frozen object from bypassing policy.
        configuration = ServerConfiguration(**configuration.as_dict())
        expected = configuration.revision if expected_revision is None else expected_revision
        if type(expected) is not int or not 0 <= expected <= MAX_REVISION:
            raise ConfigurationError("Expected configuration revision is invalid")
        with self._transaction() as directory:
            current = self._read_locked(directory)
            if current.revision != expected:
                raise ConfigurationConflictError(
                    "Server configuration changed; reload before saving"
                )
            if current.revision == MAX_REVISION:
                raise ConfigurationError("Server configuration revision limit reached")
            saved = replace(configuration, revision=current.revision + 1)
            self._write_locked(directory, saved)
            return saved

    @contextmanager
    def _transaction(self) -> Iterator[int]:
        with self._process_lock:
            directory = lock = -1
            try:
                directory = open_private_directory(self.path.parent, create=True)
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                flags |= getattr(os, "O_CLOEXEC", 0)
                lock_name = f".{self.path.name}.lock"
                try:
                    lock = os.open(
                        lock_name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
                    )
                    os.fchmod(lock, 0o600)
                except FileExistsError:
                    lock = os.open(lock_name, flags, dir_fd=directory)
                _validate_file(lock, "Configuration lock")
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield directory
            except PathSecurityError as exc:
                raise ConfigurationError("Server configuration directory is unsafe") from exc
            except OSError as exc:
                raise ConfigurationError("Cannot access private server configuration") from exc
            finally:
                if lock >= 0:
                    with contextlib.suppress(OSError):
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    os.close(lock)
                if directory >= 0:
                    os.close(directory)

    def _read_locked(self, directory: int) -> ServerConfiguration:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(self.path.name, flags, dir_fd=directory)
        except FileNotFoundError:
            return ServerConfiguration()
        try:
            _validate_file(descriptor, "Server configuration")
            if os.fstat(descriptor).st_size > MAX_CONFIGURATION_BYTES:
                raise ConfigurationError("Server configuration exceeds the size limit")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                body = stream.read(MAX_CONFIGURATION_BYTES + 1)
            if len(body) > MAX_CONFIGURATION_BYTES:
                raise ConfigurationError("Server configuration exceeds the size limit")
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(payload, dict) or set(payload) != set(
                ServerConfiguration().as_dict()
            ):
                raise ConfigurationError("Server configuration has an invalid format")
            return ServerConfiguration(**payload)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ConfigurationError("Server configuration is not valid JSON") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_locked(self, directory: int, configuration: ServerConfiguration) -> None:
        body = (
            json.dumps(
                configuration.as_dict(), allow_nan=False, separators=(",", ":"), sort_keys=True
            )
            + "\n"
        ).encode("utf-8")
        if len(body) > MAX_CONFIGURATION_BYTES:
            raise ConfigurationError("Server configuration exceeds the size limit")
        temporary = f".{self.path.name}-{secrets.token_hex(12)}.tmp"
        replaced = False
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            replaced = True
            os.fsync(directory)
        except OSError as exc:
            message = (
                "Server configuration may have been saved; reload before retrying"
                if replaced
                else "Cannot save server configuration"
            )
            raise ConfigurationError(message, possibly_saved=replaced) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not replaced:
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=directory)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("Server configuration contains duplicate fields")
        result[key] = value
    return result


def _validate_file(descriptor: int, label: str) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ConfigurationError(f"{label} must be a regular file with one link")
    if metadata.st_uid != os.getuid():
        raise ConfigurationError(f"{label} must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ConfigurationError(f"{label} permissions must be 0600")
