#!/usr/bin/env python3
"""Reproduce the narrow HAP transport dependency from an immutable official commit.

Only this builder downloads source/development dependencies. Runtime npm ci uses
the vendored tarball and its lock integrity, never Git or an install-time patch.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

UPSTREAM = "https://github.com/homebridge/HAP-NodeJS.git"
COMMIT = "bd7a01b889cf1c74cbfcc4a4e5efdfa53fc3532b"
VERSION = "2.3.0-halo.2"
ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "homekit-adapter" / "vendor"
PATCH = VENDOR / f"hap-nodejs-{VERSION}.patch"


def run(arguments, *, cwd=None):
    subprocess.run(arguments, cwd=cwd, check=True)


def output(arguments, *, cwd=None):
    return subprocess.check_output(arguments, cwd=cwd)


def package(source: Path, destination: Path):
    files = [source / "package.json", source / "LICENSE", source / "README.md"]
    files.extend(p for p in (source / "dist").rglob("*") if p.is_file())
    result = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=result, mode="wb", filename="", mtime=0, compresslevel=9) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive,
    ):
        for path in sorted(files, key=lambda item: item.relative_to(source).as_posix()):
            if path.is_symlink():
                raise SystemExit("Refusing symlink in dependency output")
            content = path.read_bytes()
            info = tarfile.TarInfo("package/" + path.relative_to(source).as_posix())
            info.size, info.mode, info.mtime = len(content), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(result.getvalue())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, help="Existing official Git clone containing the pinned commit"
    )
    parser.add_argument(
        "--fetch", action="store_true", help="Allow fetching official source into temporary storage"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run the complete upstream Vitest suite before packaging",
    )
    parser.add_argument("--output", type=Path, default=VENDOR / f"hap-nodejs-{VERSION}.tgz")
    args = parser.parse_args()
    provenance_path = VENDOR / f"hap-nodejs-{VERSION}.provenance.json"
    expected = json.loads(provenance_path.read_text()) if provenance_path.exists() else None
    if expected and hashlib.sha256(PATCH.read_bytes()).hexdigest() != expected["patch"]["sha256"]:
        raise SystemExit(
            "Patch differs from the reviewed version; use a new version and provenance"
        )
    if (
        output(["node", "--version"]).strip() != b"v26.12.1"
        or output(["npm", "--version"]).strip() != b"12.2.1"
    ):
        raise SystemExit("Use the qualified Node 26.12.1 / npm 12.2.1 toolchain")
    with tempfile.TemporaryDirectory(prefix="halo-hap-build-") as temporary:
        work = Path(temporary)
        source = args.source
        if source is None:
            if not args.fetch:
                raise SystemExit("Supply --source or explicitly allow --fetch")
            source = work / "official"
            run(["git", "clone", "--filter=blob:none", "--no-checkout", UPSTREAM, str(source)])
        if (
            output(["git", "rev-parse", f"{COMMIT}^{{commit}}"], cwd=source).strip().decode()
            != COMMIT
        ):
            raise SystemExit("Pinned upstream commit unavailable")
        # Archive the immutable commit, ignoring every local/uncommitted source modification.
        pristine = output(["git", "archive", "--format=tar", COMMIT], cwd=source)
        build = work / "build"
        build.mkdir()
        with tarfile.open(fileobj=io.BytesIO(pristine)) as archive:
            archive.extractall(build, filter="data")
        run(["git", "apply", "--check", str(PATCH)], cwd=build)
        run(["git", "apply", str(PATCH)], cwd=build)
        run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=build)
        run(["npm", "run", "build"], cwd=build)
        if args.test:
            run(["npm", "test"], cwd=build)
        candidate = work / "candidate.tgz"
        package(build, candidate)
        if (
            expected
            and hashlib.sha256(candidate.read_bytes()).hexdigest() != expected["archive"]["sha256"]
        ):
            raise SystemExit("Build differs from reviewed archive; existing output was preserved")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(candidate.read_bytes())
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "upstream_commit": COMMIT,
                    "patch_sha256": hashlib.sha256(PATCH.read_bytes()).hexdigest(),
                    "archive_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                    "archive": str(args.output),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
