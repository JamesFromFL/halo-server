#!/usr/bin/env python3
"""Explicit online preparation of hash-locked wheel inputs; never a startup step.

The release builder consumes the resulting directory entirely offline. Only
wheel URLs/hashes already present in uv.lock are accepted; no sdists/builds.
Requires packaging in the build environment, not in the installed launcher.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import tomllib
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from packaging.markers import Marker
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

MAX_WHEEL = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024


def select_wheels(lock: dict, built_wheels: Path | None = None) -> list[dict]:
    packages = defaultdict(list)
    for package in lock["package"]:
        packages[package["name"]].append(package)
    project = packages["halo-server"]
    if len(project) != 1:
        raise ValueError("ambiguous project lock")
    selected, processed = {}, set()
    pending = list(project[0]["dependencies"])
    while pending:
        dependency = pending.pop()
        if dependency.get("marker") and not Marker(dependency["marker"]).evaluate():
            continue
        choices = [
            p
            for p in packages[dependency["name"]]
            if ("version" not in dependency or p["version"] == dependency["version"])
            and (
                not p.get("resolution-markers")
                or any(Marker(marker).evaluate() for marker in p["resolution-markers"])
            )
        ]
        if len(choices) != 1:
            raise ValueError("ambiguous runtime lock")
        package = choices[0]
        extras = dependency.get("extra", [])
        key = (package["name"], tuple(sorted(extras)))
        if key in processed:
            continue
        processed.add(key)
        selected[package["name"]] = package
        pending.extend(package.get("dependencies", []))
        for extra in extras:
            pending.extend(package.get("optional-dependencies", {}).get(extra, []))
    tags = {tag: order for order, tag in enumerate(sys_tags())}
    result = []
    for name, package in sorted(selected.items()):
        candidates = []
        for wheel in package.get("wheels", []):
            filename = urllib.parse.unquote(
                urllib.parse.urlsplit(wheel["url"]).path.rsplit("/", 1)[-1]
            )
            wheel_name, version, _, compatible = parse_wheel_filename(filename)
            matches = compatible & tags.keys()
            if matches and str(version) == package["version"] and str(wheel_name) == name:
                candidates.append((min(tags[tag] for tag in matches), filename, wheel))
        if not candidates and built_wheels is not None:
            for path in built_wheels.glob("*.whl"):
                wheel_name, version, _, compatible = parse_wheel_filename(path.name)
                if (
                    str(wheel_name) != name
                    or str(version) != package["version"]
                    or not compatible & tags.keys()
                ):
                    continue
                receipt = built_wheels / f"{str(wheel_name).replace('-', '_')}-{version}.build.json"
                provenance = json.loads(receipt.read_text())
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if (
                    provenance.get("source_sha256")
                    != package.get("sdist", {}).get("hash", "").removeprefix("sha256:")
                    or provenance.get("source_url") != package["sdist"]["url"]
                    or provenance.get("wheel_sha256") != digest
                    or provenance.get("reproduced_identical_twice") is not True
                ):
                    raise ValueError("unqualified built wheel")
                selected_wheel = {
                    "name": name,
                    "version": package["version"],
                    "filename": path.name,
                    "url": "built://" + path.name,
                    "sha256": digest,
                    "size": path.stat().st_size,
                    "source_build": provenance,
                    "_path": path,
                }
                result.append(selected_wheel)
                break
            else:
                raise ValueError(f"no qualified built wheel for {name}")
            continue
        if not candidates:
            raise ValueError(f"no locked binary wheel for {name}")
        _, filename, wheel = min(candidates, key=lambda item: item[:2])
        if not wheel["hash"].startswith("sha256:") or not 0 < wheel["size"] <= MAX_WHEEL:
            raise ValueError("invalid locked wheel")
        result.append(
            {
                "name": name,
                "version": package["version"],
                "filename": filename,
                "url": wheel["url"],
                "sha256": wheel["hash"].split(":")[1],
                "size": wheel["size"],
            }
        )
    if sum(item["size"] for item in result) > MAX_TOTAL:
        raise ValueError("wheelhouse limit")
    return result


def download(record, destination):
    path = destination / record["filename"]
    if path.exists():
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("existing wheel differs")
        return
    temporary = path.with_suffix(".part")
    digest, length = hashlib.sha256(), 0
    try:
        source = (
            record["_path"].open("rb")
            if "_path" in record
            else urllib.request.urlopen(record["url"], timeout=30)
        )
        with source, temporary.open("xb") as output:
            while body := source.read(1024 * 1024):
                length += len(body)
                if length > record["size"]:
                    raise ValueError("wheel exceeds locked size")
                digest.update(body)
                output.write(body)
        if length != record["size"] or digest.hexdigest() != record["sha256"]:
            raise ValueError("wheel differs from lock")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--built-wheels", type=Path)
    args = parser.parse_args()
    raw = args.lock.read_bytes()
    records = select_wheels(tomllib.loads(raw.decode()), args.built_wheels)
    args.destination.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda record: download(record, args.destination), records))
    records = [
        {key: value for key, value in record.items() if not key.startswith("_")}
        for record in records
    ]
    (args.destination / "runtime-requirements.txt").write_text(
        "".join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in records)
    )
    (args.destination / "wheel-inputs.json").write_text(
        json.dumps(
            {"version": 1, "uv_lock_sha256": hashlib.sha256(raw).hexdigest(), "wheels": records},
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"wheels": len(records), "bytes": sum(r["size"] for r in records)}))


if __name__ == "__main__":
    main()
