"""Pure-stdlib installed release verification, before importing release code.

This module is also copied into the stable launcher's libexec directory. It must
not import Halo, a site package, or any code inside the selected release.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

MANIFEST_VERSION = 1
LAUNCHER_VERSION = 1
READER_VERSION = 16
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILES = 16384
MAX_RELEASE_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
ENTRIES = {
    "node": "node/bin/node",
    "adapter": "adapter/dist/src/main.js",
    "reset_helper": "adapter/dist/src/pairing-reset.js",
    "bootstrap": "bootstrap.py",
    "site_packages": "python/site-packages",
}
ENVIRONMENT = ("HALO_RELEASE_ROOT", "HALO_RELEASE_MANIFEST_SHA256", "HALO_RELEASE_MINIMUM_READER")
_CONTEXT = None


class ReleaseError(RuntimeError):
    def __init__(self, code="invalid_release"):
        self.code = code
        super().__init__(f"Managed Halo release unavailable ({code})")


def canonical(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ReleaseError()
        result[key] = value
    return result


def _json(body):
    try:
        result = json.loads(
            body,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ReleaseError()),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ReleaseError() from error
    if not isinstance(result, dict):
        raise ReleaseError()
    return result


def _keys(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ReleaseError()


def _number(value, maximum=2**53 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ReleaseError()
    return value


def _hex(value):
    if not isinstance(value, str) or HEX.fullmatch(value) is None:
        raise ReleaseError()
    return value


def _relative(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ReleaseError()
    path = Path(value)
    if path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in path.parts):
        raise ReleaseError()
    if any(ord(char) < 32 or ord(char) >= 127 for char in value):
        raise ReleaseError()
    return path


def _open_directory(path: Path, *, immutable=False):
    if not path.is_absolute():
        raise ReleaseError("unsafe_path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            if part in {".", ".."}:
                raise ReleaseError("unsafe_path")
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022 and not sticky_root:
                raise ReleaseError("unsafe_path")
        if immutable:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o555:
                raise ReleaseError("unsafe_release")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_at(directory, name, *, limit, mode=None, host=False):
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > limit
            or before.st_uid not in ({0, os.getuid()} if host else {os.getuid()})
            or (not host and before.st_nlink != 1)
            or before.st_mode & 0o022
            or mode is not None
            and stat.S_IMODE(before.st_mode) != mode
        ):
            raise ReleaseError("unsafe_file")
        chunks, remaining = [], limit + 1
        while remaining:
            body = os.read(descriptor, min(remaining, 1024 * 1024))
            if not body:
                break
            chunks.append(body)
            remaining -= len(body)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ReleaseError("changed_file")
        body = b"".join(chunks)
        if len(body) > limit or len(body) != before.st_size:
            raise ReleaseError("file_limit")
        return body, before
    finally:
        os.close(descriptor)


def _read_relative(root_fd, path, *, limit, mode=None):
    parent = os.dup(root_fd)
    try:
        for part in path.parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
            )
            os.close(parent)
            parent = child
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o555:
                raise ReleaseError("unsafe_release")
        return _read_at(parent, path.name, limit=limit, mode=mode)
    finally:
        os.close(parent)


def host_qualification():
    return {
        "platform": sys.platform,
        "machine": platform.machine(),
        "python_abi": sysconfig.get_config_var("SOABI"),
        "python_cache_tag": sys.implementation.cache_tag,
        "libc": list(platform.libc_ver()),
    }


@dataclass(frozen=True)
class VerifiedRelease:
    root: Path
    release_id: str
    manifest_sha256: str
    reader_version: int
    interpreter: Path
    payload: dict

    @property
    def build_id(self):
        return self.release_id

    @property
    def node(self):
        return self.root / ENTRIES["node"]

    @property
    def entry(self):
        return self.root / ENTRIES["adapter"]

    @property
    def reset_helper(self):
        return self.root / ENTRIES["reset_helper"]

    @property
    def bootstrap(self):
        return self.root / ENTRIES["bootstrap"]

    @property
    def site_packages(self):
        return self.root / ENTRIES["site_packages"]


def verify_release(
    root: Path, expected_manifest_sha256: str, *, minimum_reader=0
) -> VerifiedRelease:
    """Verify every byte and exact host qualification, never executing release code."""
    try:
        return _verify_release(Path(root), expected_manifest_sha256, minimum_reader)
    except ReleaseError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ReleaseError() from error


def _verify_release(root, expected, minimum_reader):
    _hex(expected)
    _number(minimum_reader)
    directory = _open_directory(root, immutable=True)
    try:
        body, _ = _read_at(directory, "manifest.json", limit=MAX_MANIFEST_BYTES, mode=0o444)
        if hashlib.sha256(body).hexdigest() != expected:
            raise ReleaseError("manifest_mismatch")
        manifest = _json(body)
        _keys(manifest, {"schema", "release_id", "payload"})
        payload = manifest["payload"]
        _keys(
            payload,
            {
                "reader_version",
                "launcher_version",
                "qualification",
                "interpreter",
                "external_files",
                "entries",
                "files",
                "provenance",
            },
        )
        if manifest["schema"] != MANIFEST_VERSION or type(manifest["schema"]) is not int:
            raise ReleaseError("unsupported_manifest")
        identity = _hex(manifest["release_id"])
        if hashlib.sha256(canonical(payload)).hexdigest() != identity or root.name != identity:
            raise ReleaseError("release_identity")
        if _number(payload["reader_version"]) < minimum_reader:
            raise ReleaseError("reader_too_old")
        if _number(payload["launcher_version"]) > LAUNCHER_VERSION:
            raise ReleaseError("launcher_too_old")
        if payload["qualification"] != host_qualification():
            raise ReleaseError("host_mismatch")
        if payload["entries"] != ENTRIES or not isinstance(payload["provenance"], dict):
            raise ReleaseError()
        files = payload["files"]
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise ReleaseError("file_limit")
        expected_files, total = set(), 0
        for record in files:
            _keys(record, {"path", "size", "mode", "sha256"})
            path = _relative(record["path"])
            if str(path) in expected_files or str(path) == "manifest.json":
                raise ReleaseError()
            expected_files.add(str(path))
            total += _number(record["size"], MAX_FILE_BYTES)
            if total > MAX_RELEASE_BYTES or record["mode"] not in {0o444, 0o555}:
                raise ReleaseError("file_limit")
            raw, info = _read_relative(directory, path, limit=record["size"], mode=record["mode"])
            if info.st_size != record["size"] or hashlib.sha256(raw).hexdigest() != _hex(
                record["sha256"]
            ):
                raise ReleaseError("file_mismatch")
            if str(path) == ENTRIES["node"] and record["mode"] != 0o555:
                raise ReleaseError("node_not_executable")
        actual = set()
        directory_count = 0

        def walk(fd, prefix="", depth=0):
            nonlocal directory_count
            directory_count += 1
            if depth > 32 or directory_count > MAX_FILES:
                raise ReleaseError("directory_limit")
            for entry in os.scandir(fd):
                name = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o555:
                        raise ReleaseError("unsafe_release")
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        walk(child, name + "/", depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    actual.add(name)
                else:
                    raise ReleaseError("unsafe_release")
                if len(actual) > MAX_FILES + 1:
                    raise ReleaseError("file_limit")

        walk(directory)
        if actual != expected_files | {"manifest.json"}:
            raise ReleaseError("unlisted_file")
        if not set(ENTRIES.values()) - {ENTRIES["site_packages"]} <= expected_files:
            raise ReleaseError("missing_entry")
        if not any(path.startswith(ENTRIES["site_packages"] + "/") for path in expected_files):
            raise ReleaseError("missing_python")
        external = payload["external_files"]
        if not isinstance(external, list) or len(external) > 256:
            raise ReleaseError()
        interpreter = payload["interpreter"]
        _keys(interpreter, {"path", "sha256"})
        records = [interpreter, *external]
        for record in records:
            _keys(record, {"path", "sha256"})
            path = Path(record["path"])
            parent = _open_directory(path.parent)
            try:
                raw, _ = _read_at(parent, path.name, limit=512 * 1024 * 1024, host=True)
            finally:
                os.close(parent)
            if hashlib.sha256(raw).hexdigest() != _hex(record["sha256"]):
                raise ReleaseError("host_file_mismatch")
        if Path(os.path.realpath(sys.executable)) != Path(interpreter["path"]):
            raise ReleaseError("interpreter_mismatch")
        return VerifiedRelease(
            root, identity, expected, payload["reader_version"], Path(interpreter["path"]), payload
        )
    finally:
        os.close(directory)


def from_environment() -> VerifiedRelease | None:
    values = [os.environ.get(key) for key in ENVIRONMENT]
    if not any(value is not None for value in values):
        return None
    if not all(values):
        raise ReleaseError("missing_environment")
    try:
        minimum = int(values[2])
    except ValueError as error:
        raise ReleaseError() from error
    return verify_release(Path(values[0]), values[1], minimum_reader=minimum)


def _guard_module():
    try:
        from . import release_guard
    except ImportError:
        import release_guard
    return release_guard


def configure_verified_release(release: VerifiedRelease) -> None:
    """Internal startup hook, only after verifying the selected artifact."""
    global _CONTEXT
    if not isinstance(release, VerifiedRelease):
        raise ReleaseError("invalid_context")
    _CONTEXT = release


def managed_release() -> VerifiedRelease | None:
    """Use startup-verified immutable context; worker admission never rehashes it."""
    values = [os.environ.get(name) for name in ENVIRONMENT]
    if not any(value is not None for value in values):
        if _CONTEXT is not None:
            raise ReleaseError("lost_context")
        return None
    state = os.environ.get("HALO_RELEASE_STATE_DIRECTORY")
    if not state or not _guard_module().inherited_guard_fds(state_directory=Path(state)):
        raise ReleaseError("missing_guard")
    release = _CONTEXT
    if release is None:
        release = from_environment()
        if release is None:
            raise ReleaseError("missing_context")
        configure_verified_release(release)
    if (
        values[0] != str(release.root)
        or values[1] != release.manifest_sha256
        or os.environ.get("HALO_RELEASE_ID") != release.release_id
        or not values[2]
        or not values[2].isascii()
        or not values[2].isdecimal()
        or int(values[2]) > release.reader_version
    ):
        raise ReleaseError("changed_context")
    return release


def read_protected_json(path: Path):
    try:
        directory = _open_directory(path.parent)
        try:
            body, _ = _read_at(directory, path.name, limit=65536, mode=0o600)
        finally:
            os.close(directory)
        return _json(body)
    except (OSError, ValueError) as error:
        raise ReleaseError("unsafe_selection") from error


def selected_release(prefix: Path, state_directory: Path) -> VerifiedRelease:
    selection = read_protected_json(prefix / "selection.json")
    _keys(selection, {"version", "revision", "active", "previous"})
    if type(selection["version"]) is not int or selection["version"] != 1:
        raise ReleaseError()
    _number(selection["revision"])
    for record in (selection["active"], selection["previous"]):
        if record is not None:
            _keys(record, {"release_id", "manifest_sha256"})
            _hex(record["release_id"])
            _hex(record["manifest_sha256"])
    if selection["active"] is None:
        raise ReleaseError("no_release")
    policy = read_protected_json(state_directory / "release-policy.json")
    _keys(policy, {"version", "revision", "minimum_reader"})
    if type(policy["version"]) is not int or policy["version"] != 1:
        raise ReleaseError()
    _number(policy["revision"])
    floor = _number(policy["minimum_reader"])
    if floor < 14:
        raise ReleaseError("invalid_reader_floor")
    active = selection["active"]
    return verify_release(
        prefix / "releases" / active["release_id"], active["manifest_sha256"], minimum_reader=floor
    )


def launch_selected(prefix: Path, state_directory: Path, arguments: list[str], *, operator=False):
    if any(arg == "--state-directory" or arg.startswith("--state-directory=") for arg in arguments):
        raise ReleaseError("state_override")
    guard = _guard_module().ReleaseGuard.acquire(state_directory, nonblocking=True)
    try:
        release = selected_release(prefix, state_directory)
        environment = dict(os.environ)
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "NODE_OPTIONS",
            "NODE_PATH",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
        ):
            environment.pop(name, None)
        environment.update(
            zip(
                ENVIRONMENT,
                (
                    str(release.root),
                    release.manifest_sha256,
                    str(
                        read_protected_json(state_directory / "release-policy.json")[
                            "minimum_reader"
                        ]
                    ),
                ),
                strict=True,
            )
        )
        os.set_inheritable(guard.fileno(), True)
        environment.update(guard.environment())
        environment["HALO_RELEASE_ID"] = release.release_id
        environment["HALO_RELEASE_OPERATOR"] = "1" if operator else "0"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        os.execve(
            release.interpreter,
            [str(release.interpreter), "-I", "-S", "-B", str(release.bootstrap), *arguments],
            environment,
        )
    finally:
        guard.close()
