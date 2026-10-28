"""Bounded owner-only JSON snapshots with revision checks and durable replacement.

Callers supply validated immutable dataclasses. No credentials, account authority,
or file contents are included in failures. A post-replace fsync failure explicitly
requires reload rather than pretending that the previous revision survived.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from halo_server.paths import PathSecurityError, open_private_directory

MAX_REVISION = 2**53 - 1
_locks_guard = threading.Lock()
_path_locks: dict[Path, threading.RLock] = {}


class RevisionedSnapshot(Protocol):
    revision: int

    def as_dict(self) -> dict[str, Any]: ...


Snapshot = TypeVar("Snapshot", bound=RevisionedSnapshot)


class PrivateJsonError(RuntimeError):
    """A private snapshot could not be read, validated, or safely committed."""

    def __init__(self, message: str, *, possibly_saved: bool = False) -> None:
        super().__init__(message)
        self.possibly_saved = possibly_saved


class PrivateJsonStore(Generic[Snapshot]):
    """Single-file dataclass transactions using pinned paths and exclusive locks."""

    def __init__(
        self,
        path: str | Path,
        *,
        snapshot_type: type[Snapshot],
        decode: Callable[[dict[str, Any]], Snapshot],
        default: Callable[[], Snapshot],
        max_bytes: int,
        label: str,
        noun: str,
        error_type: type[PrivateJsonError],
        conflict_type: type[PrivateJsonError],
    ) -> None:
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self._snapshot_type = snapshot_type
        self._decode = decode
        self._default = default
        self._max_bytes = max_bytes
        self._label = label
        self._noun = noun
        self._error = error_type
        self._conflict = conflict_type
        with _locks_guard:
            self._process_lock = _path_locks.setdefault(self.path, threading.RLock())

    def load(self) -> Snapshot:
        with self._transaction() as directory:
            return self._read_locked(directory)

    def _validated(self, snapshot: Snapshot) -> Snapshot:
        if not isinstance(snapshot, self._snapshot_type):
            raise self._error(f"{self._label} must be a validated snapshot")
        # Reconstruct nested dataclasses too; frozen objects can still be forged.
        return self._decode(snapshot.as_dict())

    def _expected(self, expected: int) -> None:
        if type(expected) is not int or not 0 <= expected <= MAX_REVISION:
            raise self._error(f"Expected {self._noun} revision is invalid")

    def _check_revision(self, current: Snapshot, expected: int) -> None:
        if current.revision != expected:
            raise self._conflict(f"{self._label} changed; reload before saving")

    def save(self, snapshot: Snapshot, *, expected_revision: int | None = None) -> Snapshot:
        snapshot = self._validated(snapshot)
        expected = snapshot.revision if expected_revision is None else expected_revision
        self._expected(expected)
        with self._transaction() as directory:
            current = self._read_locked(directory)
            self._check_revision(current, expected)
            return self._save_locked(directory, current, snapshot)

    def update(
        self,
        transform: Callable[[Snapshot], Snapshot],
        *,
        expected_revision: int | None = None,
    ) -> Snapshot:
        """Apply a trusted synchronous transform inside one CAS transaction.

        Unchanged snapshots avoid unnecessary writes. The optional expected
        revision is still checked, including when a transform would do nothing.
        """
        if expected_revision is not None:
            self._expected(expected_revision)
        with self._transaction() as directory:
            current = self._read_locked(directory)
            if expected_revision is not None:
                self._check_revision(current, expected_revision)
            snapshot = self._validated(transform(current))
            if snapshot.revision != current.revision:
                raise self._error(f"{self._label} transform changed its revision")
            if snapshot == current:
                return current
            return self._save_locked(directory, current, snapshot)

    def _save_locked(self, directory: int, current: Snapshot, snapshot: Snapshot) -> Snapshot:
        if current.revision == MAX_REVISION:
            raise self._error(f"{self._label} revision limit reached")
        saved = replace(snapshot, revision=current.revision + 1)
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
                self._validate_file(lock, f"{self._noun.capitalize()} lock")
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield directory
            except PathSecurityError as exc:
                raise self._error(f"{self._label} directory is unsafe") from exc
            except OSError as exc:
                raise self._error(f"Cannot access private {self._label.lower()}") from exc
            finally:
                if lock >= 0:
                    with contextlib.suppress(OSError):
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    os.close(lock)
                if directory >= 0:
                    os.close(directory)

    def _read_locked(self, directory: int) -> Snapshot:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(self.path.name, flags, dir_fd=directory)
        except FileNotFoundError:
            return self._default()
        try:
            self._validate_file(descriptor, self._label)
            if os.fstat(descriptor).st_size > self._max_bytes:
                raise self._error(f"{self._label} exceeds the size limit")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                body = stream.read(self._max_bytes + 1)
            if len(body) > self._max_bytes:
                raise self._error(f"{self._label} exceeds the size limit")
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=self._unique_object,
                parse_constant=self._invalid_constant,
            )
            if not isinstance(payload, dict):
                raise self._error(f"{self._label} has an invalid format")
            return self._decode(payload)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise self._error(f"{self._label} is not valid JSON") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_locked(self, directory: int, snapshot: Snapshot) -> None:
        body = (
            json.dumps(
                snapshot.as_dict(),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(body) > self._max_bytes:
            raise self._error(f"{self._label} exceeds the size limit")
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
                f"{self._label} may have been saved; reload before retrying"
                if replaced
                else f"Cannot save {self._label.lower()}"
            )
            raise self._error(message, possibly_saved=replaced) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not replaced:
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=directory)

    def _unique_object(self, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise self._error(f"{self._label} contains duplicate fields")
            result[key] = value
        return result

    def _invalid_constant(self, _value: str) -> None:
        raise self._error(f"{self._label} contains a non-finite number")

    def _validate_file(self, descriptor: int, label: str) -> None:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise self._error(f"{label} must be a regular file with one link")
        if metadata.st_uid != os.getuid():
            raise self._error(f"{label} must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise self._error(f"{label} permissions must be 0600")
