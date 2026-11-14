"""Offline validation of the browser bundle shipped inside an immutable release."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

MAX_ASSETS = 256
MAX_ASSET_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024


class WebBundleError(RuntimeError):
    pass


def _records(value, *, limit):
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        raise WebBundleError("Web build manifest has an invalid file count")
    result = {}
    for record in value:
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise WebBundleError("Web build manifest file record is invalid")
        name, digest = record["path"], record["sha256"]
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 512
            or "\\" in name
            or PurePosixPath(name).is_absolute()
            or str(PurePosixPath(name)) != name
            or any(part in {".", ".."} for part in name.split("/"))
            or name in result
            or not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        ):
            raise WebBundleError("Web build manifest contains an invalid file identity")
        result[name] = digest
    if list(result) != sorted(result):
        raise WebBundleError("Web build manifest is not ordered")
    return result


def _files(directory, *, source=False):
    result = {}
    if directory.is_symlink() or not directory.is_dir():
        raise WebBundleError("Web build directory is unavailable")
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if source and any(part in {"node_modules", ".git"} for part in relative.parts):
            continue
        if path.is_symlink():
            raise WebBundleError("Web build files cannot be symlinks")
        if path.is_file():
            result[relative.as_posix()] = path
        elif not path.is_dir():
            raise WebBundleError("Web build contains a special file")
    return result


def validate_bundle(directory: Path, *, source: Path | None = None) -> dict[str, str]:
    """Return the exact served asset allowlist; optionally check source freshness."""
    try:
        files = _files(Path(directory))
        manifest_path = files.pop("build-manifest.json")
        if manifest_path.stat().st_size > 512 * 1024:
            raise WebBundleError("Web build manifest is too large")
        manifest = json.loads(manifest_path.read_bytes())
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema", "source_hash", "source_files", "assets"}
            or type(manifest["schema"]) is not int
            or manifest["schema"] != 1
        ):
            raise WebBundleError("Web build manifest schema is invalid")
        assets = _records(manifest["assets"], limit=MAX_ASSETS)
        sources = _records(manifest["source_files"], limit=2048)
        canonical_sources = json.dumps(
            [{"path": name, "sha256": digest} for name, digest in sources.items()],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if hashlib.sha256(canonical_sources).hexdigest() != manifest["source_hash"]:
            raise WebBundleError("Web source manifest checksum does not match")
        if set(files) != set(assets) or not {
            "index.html",
            "halo.png",
            "THIRD_PARTY_NOTICES.txt",
        } <= set(assets):
            raise WebBundleError("Web assets are missing or unexpected")
        size = 0
        for name, path in files.items():
            length = path.stat().st_size
            size += length
            if length > MAX_ASSET_BYTES or size > MAX_BUNDLE_BYTES:
                raise WebBundleError("Web bundle exceeds its size limit")
            if hashlib.sha256(path.read_bytes()).hexdigest() != assets[name]:
                raise WebBundleError("Web asset checksum does not match")
        if source is not None:
            actual = _files(Path(source), source=True)
            if set(actual) != set(sources) or any(
                hashlib.sha256(path.read_bytes()).hexdigest() != sources[name]
                for name, path in actual.items()
            ):
                raise WebBundleError("Browser sources changed; rebuild the web interface")
        return assets
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise WebBundleError("Web bundle is absent or invalid; rebuild before packaging") from error
