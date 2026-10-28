"""Immutable operating settings and private revision-checked JSON persistence."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from halo_server.private_json import MAX_REVISION as MAX_REVISION
from halo_server.private_json import PrivateJsonError, PrivateJsonStore

CONFIGURATION_VERSION = 1
MAX_CONFIGURATION_BYTES = 64 * 1024


class ConfigurationError(PrivateJsonError):
    """Configuration is invalid, unsafe, unreadable or could not be saved."""


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


def _decode_configuration(payload: dict[str, Any]) -> ServerConfiguration:
    if set(payload) != set(ServerConfiguration().as_dict()):
        raise ConfigurationError("Server configuration has an invalid format")
    return ServerConfiguration(**payload)


class ConfigurationStore(PrivateJsonStore[ServerConfiguration]):
    """Bounded private JSON, serialized by process lock and exclusive flock."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(
            Path(os.path.abspath(Path(path).expanduser())),
            snapshot_type=ServerConfiguration,
            decode=_decode_configuration,
            default=ServerConfiguration,
            max_bytes=MAX_CONFIGURATION_BYTES,
            label="Server configuration",
            noun="configuration",
            error_type=ConfigurationError,
            conflict_type=ConfigurationConflictError,
        )

    def save(
        self,
        configuration: ServerConfiguration,
        *,
        expected_revision: int | None = None,
    ) -> ServerConfiguration:
        return super().save(configuration, expected_revision=expected_revision)
