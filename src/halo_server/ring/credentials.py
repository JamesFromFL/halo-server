"""Private transactional storage for Halo Server Ring credentials."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILE_STORE_VERSION = 1
_UNCHANGED = object()
_path_locks_guard = threading.Lock()
_path_locks: dict[Path, threading.RLock] = {}


class CredentialStoreError(RuntimeError):
    """Raised when the Halo Server credential store cannot be used safely."""


class CredentialSupersededError(CredentialStoreError):
    """Raised when another process changed credentials before this writer."""


@dataclass(frozen=True)
class StoredCredentials:
    ring_token: dict[str, Any] | None
    account_email: str | None
    revision: int


class CredentialStore:
    """Owner-only, revision-checked Ring credential file."""

    def __init__(self, path: str | Path) -> None:
        configured = Path(path).expanduser()
        if not configured.is_absolute():
            configured = configured.resolve()
        if configured.parent.exists() or configured.parent.is_symlink():
            _validate_private_directory(configured.parent)
        if configured.exists() or configured.is_symlink():
            _validate_private_regular_file(configured)
        self.path = configured
        self._revision: int | None = None
        with _path_locks_guard:
            self._process_lock = _path_locks.setdefault(configured, threading.RLock())

    def load(self) -> StoredCredentials:
        with self._transaction():
            payload = self._read_locked()
            self._revision = payload["revision"]
            token = payload["ring_token"]
            return StoredCredentials(
                ring_token=dict(token) if token is not None else None,
                account_email=payload["account_email"],
                revision=payload["revision"],
            )

    def load_token(self) -> dict[str, Any] | None:
        return self.load().ring_token

    def load_account_email(self) -> str | None:
        return self.load().account_email

    def save_token(self, token: dict[str, Any]) -> None:
        if not isinstance(token, dict):
            raise CredentialStoreError("Ring token must be a JSON object")
        self._update(ring_token=dict(token))

    def save_account_email(self, email: str) -> None:
        if not isinstance(email, str):
            raise CredentialStoreError("Ring account email must be a string")
        self._update(account_email=email)

    def save_session(self, token: dict[str, Any], email: str) -> None:
        """Atomically store a validated token and its account identity."""
        if not isinstance(token, dict):
            raise CredentialStoreError("Ring token must be a JSON object")
        if not isinstance(email, str):
            raise CredentialStoreError("Ring account email must be a string")
        self._update(ring_token=dict(token), account_email=email)

    def clear_token(self) -> None:
        self._update(ring_token=None)

    def clear_account_email(self) -> None:
        self._update(account_email=None)

    def clear(self) -> None:
        """Atomically tombstone all stored Ring credentials."""
        self._update(ring_token=None, account_email=None)

    def assert_revision(self, expected: int) -> None:
        """Reject a decision based on credentials changed by another process."""
        with self._transaction():
            actual = self._read_locked()["revision"]
            if actual != expected:
                raise CredentialSupersededError(
                    "Ring credentials changed in another process; refusing stale state"
                )

    def _update(
        self,
        *,
        ring_token: dict[str, Any] | None | object = _UNCHANGED,
        account_email: str | None | object = _UNCHANGED,
    ) -> None:
        with self._transaction():
            payload = self._read_locked()
            if self._revision is not None and payload["revision"] != self._revision:
                raise CredentialSupersededError(
                    "Ring credentials changed in another process; refusing a stale update"
                )
            if ring_token is not _UNCHANGED:
                payload["ring_token"] = ring_token
            if account_email is not _UNCHANGED:
                payload["account_email"] = account_email
            payload["revision"] += 1
            self._write_locked(payload)
            self._revision = payload["revision"]

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._process_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise CredentialStoreError(
                    f"Cannot create credential directory: {self.path.parent}"
                ) from exc
            _validate_private_directory(self.path.parent)
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            open_flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(lock_path, open_flags, 0o600)
            except OSError as exc:
                raise CredentialStoreError(
                    f"Cannot lock Ring credential file: {self.path}"
                ) from exc
            try:
                lock_stat = os.fstat(descriptor)
                if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid():
                    raise CredentialStoreError(
                        "Ring credential lock must be a regular file owned by the current user"
                    )
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "a+b") as lock_file:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                raise

    def _read_locked(self) -> dict[str, Any]:
        if not self.path.exists() and not self.path.is_symlink():
            return {
                "version": FILE_STORE_VERSION,
                "revision": 0,
                "ring_token": None,
                "account_email": None,
            }

        open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        open_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, open_flags)
        except OSError as exc:
            raise CredentialStoreError(
                "Stored Ring credential file is unreadable or invalid"
            ) from exc
        try:
            _validate_private_descriptor(descriptor, "Ring credential file")
            with os.fdopen(descriptor, encoding="utf-8") as credential_file:
                descriptor = -1
                payload = json.load(credential_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError(
                "Stored Ring credential file is unreadable or invalid"
            ) from exc
        finally:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

        if (
            not isinstance(payload, dict)
            or payload.get("version") != FILE_STORE_VERSION
            or type(payload.get("revision")) is not int
            or payload["revision"] < 0
            or (
                payload.get("ring_token") is not None
                and not isinstance(payload.get("ring_token"), dict)
            )
            or (
                payload.get("account_email") is not None
                and not isinstance(payload.get("account_email"), str)
            )
        ):
            raise CredentialStoreError("Stored Ring credential file has an invalid format")
        return {
            "version": FILE_STORE_VERSION,
            "revision": payload["revision"],
            "ring_token": payload.get("ring_token"),
            "account_email": payload.get("account_email"),
        }

    def _write_locked(self, payload: dict[str, Any]) -> None:
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}-",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                os.chmod(tmp_path, 0o600)
                json.dump(payload, tmp, separators=(",", ":"), sort_keys=True)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.path)
            tmp_path = None
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(self.path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, TypeError, ValueError) as exc:
            raise CredentialStoreError(f"Cannot write Ring credential file: {self.path}") from exc
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()


def _validate_private_descriptor(descriptor: int, label: str) -> None:
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode):
        raise CredentialStoreError(f"{label} must be a regular file")
    if file_stat.st_uid != os.getuid():
        raise CredentialStoreError(f"{label} must be owned by the current user")
    if file_stat.st_mode & 0o077:
        raise CredentialStoreError(f"{label} permissions must not allow group or other access")


def _validate_private_regular_file(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise CredentialStoreError(f"Cannot inspect Ring credential file: {path}") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise CredentialStoreError("Ring credential file must be a regular file, not a symlink")
    if file_stat.st_uid != os.getuid():
        raise CredentialStoreError("Ring credential file must be owned by the current user")
    if file_stat.st_mode & 0o077:
        raise CredentialStoreError(
            "Ring credential file permissions must not allow group or other access"
        )


def _validate_private_directory(path: Path) -> None:
    try:
        directory_stat = path.lstat()
    except OSError as exc:
        raise CredentialStoreError(f"Cannot inspect Ring credential directory: {path}") from exc
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise CredentialStoreError("Ring credential directory must be a directory, not a symlink")
    if directory_stat.st_uid != os.getuid():
        raise CredentialStoreError("Ring credential directory must be owned by the current user")
    if directory_stat.st_mode & 0o077:
        raise CredentialStoreError(
            "Ring credential directory permissions must not allow group or other access"
        )
