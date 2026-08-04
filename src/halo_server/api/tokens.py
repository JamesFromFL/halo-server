"""Persistent hashed credentials for Halo Server plugin clients."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from halo_server.paths import ensure_private_directory

TOKEN_STORE_VERSION = 1
TOKEN_PATTERN = re.compile(r"^halo1\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$")
CLIENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_PLUGIN_IDENTITIES = 32


class PluginTokenStoreError(RuntimeError):
    """Plugin credentials could not be stored or validated safely."""


@dataclass(frozen=True)
class PluginIdentity:
    id: str
    label: str
    created_at: float

    def as_dict(self) -> dict[str, str | float]:
        return {
            "id": self.id,
            "label": self.label,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class IssuedPluginToken:
    identity: PluginIdentity
    token: str


class PluginTokenStore:
    """Store one SHA-256 token verifier per plugin identity."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.RLock()
        if directory.exists() or directory.is_symlink():
            try:
                ensure_private_directory(directory)
            except Exception as exc:
                raise PluginTokenStoreError("Plugin credential directory is not safe") from exc

    def create(
        self,
        label: str,
        *,
        now: float | None = None,
    ) -> IssuedPluginToken:
        normalized_label = _validate_label(label)
        created_at = time.time() if now is None else now
        if (
            not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
            or not math.isfinite(created_at)
            or created_at < 0
        ):
            raise ValueError("plugin creation time must be finite and non-negative")
        with self._lock:
            self._prepare_directory()
            if len(self.list_identities()) >= MAX_PLUGIN_IDENTITIES:
                raise PluginTokenStoreError("Plugin credential limit reached")
            client_id = uuid.uuid4().hex
            token = f"halo1.{client_id}.{secrets.token_urlsafe(32)}"
            payload = {
                "version": TOKEN_STORE_VERSION,
                "id": client_id,
                "label": normalized_label,
                "created_at": float(created_at),
                "token_hash": _token_hash(token),
            }
            self._write_record(client_id, payload)
            return IssuedPluginToken(
                PluginIdentity(client_id, normalized_label, float(created_at)),
                token,
            )

    def verify(self, token: str) -> PluginIdentity | None:
        parsed = TOKEN_PATTERN.fullmatch(token) if isinstance(token, str) else None
        if parsed is None:
            return None
        client_id = parsed.group(1)
        with self._lock:
            try:
                payload = self._read_record(client_id)
            except FileNotFoundError:
                return None
            if not hmac.compare_digest(payload["token_hash"], _token_hash(token)):
                return None
            return PluginIdentity(
                id=payload["id"],
                label=payload["label"],
                created_at=payload["created_at"],
            )

    def list_identities(self) -> list[PluginIdentity]:
        with self._lock:
            if not self.directory.exists() and not self.directory.is_symlink():
                return []
            self._prepare_directory()
            identities = []
            for path in sorted(self.directory.glob("*.json")):
                client_id = path.stem
                if CLIENT_ID_PATTERN.fullmatch(client_id) is None:
                    raise PluginTokenStoreError(
                        "Plugin credential directory contains an invalid record"
                    )
                payload = self._read_record(client_id)
                identities.append(
                    PluginIdentity(
                        id=payload["id"],
                        label=payload["label"],
                        created_at=payload["created_at"],
                    )
                )
            return identities

    def revoke(self, client_id: str) -> bool:
        if not isinstance(client_id, str) or CLIENT_ID_PATTERN.fullmatch(client_id) is None:
            raise ValueError("plugin client id is invalid")
        with self._lock:
            self._prepare_directory()
            path = self.directory / f"{client_id}.json"
            try:
                _validate_record_path(path)
                path.unlink()
                _fsync_directory(self.directory)
                return True
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise PluginTokenStoreError("Plugin credential could not be revoked") from exc

    def _prepare_directory(self) -> None:
        try:
            ensure_private_directory(self.directory)
        except Exception as exc:
            raise PluginTokenStoreError("Plugin credential directory is not safe") from exc

    def _read_record(self, client_id: str) -> dict:
        path = self.directory / f"{client_id}.json"
        open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        open_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, open_flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise PluginTokenStoreError("Plugin credential record is unsafe") from exc
        try:
            file_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.getuid()
                or file_stat.st_mode & 0o077
            ):
                raise PluginTokenStoreError("Plugin credential record is unsafe")
            with os.fdopen(descriptor, encoding="utf-8") as record_file:
                descriptor = -1
                payload = json.load(record_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PluginTokenStoreError("Plugin credential record is invalid") from exc
        finally:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        _validate_payload(payload, expected_id=client_id)
        return payload

    def _write_record(self, client_id: str, payload: dict) -> None:
        tmp_path: Path | None = None
        destination = self.directory / f"{client_id}.json"
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.directory,
                prefix=f".{client_id}-",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                os.chmod(tmp_path, 0o600)
                json.dump(payload, tmp, separators=(",", ":"), sort_keys=True)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, destination)
            tmp_path = None
            _fsync_directory(self.directory)
        except (OSError, TypeError, ValueError) as exc:
            raise PluginTokenStoreError("Plugin credential could not be stored") from exc
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()


def _validate_payload(payload: object, *, expected_id: str) -> None:
    if (
        not isinstance(payload, dict)
        or payload.get("version") != TOKEN_STORE_VERSION
        or payload.get("id") != expected_id
        or not isinstance(payload.get("label"), str)
        or _safe_label(payload.get("label")) != payload.get("label")
        or not isinstance(payload.get("created_at"), (int, float))
        or isinstance(payload.get("created_at"), bool)
        or not math.isfinite(payload["created_at"])
        or payload["created_at"] < 0
        or not isinstance(payload.get("token_hash"), str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["token_hash"]) is None
    ):
        raise PluginTokenStoreError("Plugin credential record is invalid")


def _validate_record_path(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PluginTokenStoreError("Plugin credential record is unsafe") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.getuid()
        or file_stat.st_mode & 0o077
    ):
        raise PluginTokenStoreError("Plugin credential record is unsafe")


def _validate_label(label: str) -> str:
    normalized = _safe_label(label)
    if normalized is None:
        raise ValueError("plugin label must be 1 to 80 printable characters")
    return normalized


def _safe_label(label: object) -> str | None:
    if not isinstance(label, str):
        return None
    normalized = label.strip()
    if (
        not normalized
        or len(normalized) > 80
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        return None
    return normalized


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
