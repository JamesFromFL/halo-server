"""Partition the collected suite and verify every segment's JUnit case count."""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

GROUPS = ("group-1", "group-2", "group-3", "group-4")


def group_for_file(filename: str) -> str:
    path = PurePosixPath(filename)
    match = re.fullmatch(r"test_([a-z]).*\.py", path.name)
    if not match or path.is_absolute() or ".." in path.parts or path.parts[0] != "tests":
        raise ValueError(f"Unrecognized collected test file: {filename}")
    letter = match[1]
    if path.parts[:2] == ("tests", "hub") or letter <= "g":
        return GROUPS[0]
    if letter == "h":
        return GROUPS[1]
    if letter <= "q":
        return GROUPS[2]
    return GROUPS[3]


def partition(items: list[tuple[str, str]]) -> dict:
    groups = {name: {"files": [], "nodeids": []} for name in GROUPS}
    seen = set()
    for nodeid, filename in items:
        if nodeid in seen or nodeid.partition("::")[0] != filename:
            raise ValueError(f"Duplicate or inconsistent collected case: {nodeid}")
        seen.add(nodeid)
        group = groups[group_for_file(filename)]
        if filename not in group["files"]:
            group["files"].append(filename)
        group["nodeids"].append(nodeid)
    flattened = [nodeid for group in groups.values() for nodeid in group["nodeids"]]
    if len(flattened) != len(items) or set(flattened) != seen:
        raise ValueError("Test groups do not cover the collected suite exactly")
    if any(not group["nodeids"] for group in groups.values()):
        raise ValueError("Every CI test group must contain collected cases")
    return {"schema": 1, "case_count": len(items), "groups": groups}


def collect(output: Path) -> None:
    import pytest

    class Collector:
        def __init__(self):
            self.items: list[tuple[str, str]] = []

        def pytest_collection_finish(self, session):
            root = session.config.rootpath
            self.items = [
                (item.nodeid, item.path.relative_to(root).as_posix()) for item in session.items
            ]

    collector = Collector()
    result = pytest.main(
        [
            "--collect-only",
            "-q",
            "-p",
            "pytest_asyncio.plugin",
            "-p",
            "no:cacheprovider",
        ],
        plugins=[collector],
    )
    if result:
        raise SystemExit(result)
    manifest = partition(collector.items)
    output.mkdir(parents=True, exist_ok=True)
    for name, group in manifest["groups"].items():
        (output / f"{name}.txt").write_text("\n".join(group["files"]) + "\n")
        print(f"{name}: {len(group['files'])} files, {len(group['nodeids'])} cases")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def verify(output: Path) -> None:
    manifest = json.loads((output / "manifest.json").read_text())
    items = [
        (nodeid, nodeid.partition("::")[0])
        for group in manifest["groups"].values()
        for nodeid in group["nodeids"]
    ]
    if manifest != partition(items):
        raise ValueError("Collected test manifest is inconsistent")
    total = 0
    for name, group in manifest["groups"].items():
        expected = len(group["nodeids"])
        paths = (output / f"{name}.txt").read_text().splitlines()
        if paths != group["files"]:
            raise ValueError(f"{name} file selection changed after collection")
        root = ET.parse(output / f"{name}.xml").getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        if root.tag not in {"testsuite", "testsuites"} or len(suites) != 1:
            raise ValueError(f"{name} has an unexpected JUnit report structure")
        suite = suites[0]
        cases = suite.findall("testcase")
        if suite.tag != "testsuite" or int(suite.get("tests", "-1")) != expected:
            raise ValueError(f"{name} did not report all {expected} collected cases")
        if len(cases) != expected:
            raise ValueError(f"{name} JUnit case count does not match collection")
        if int(suite.get("errors", "0")) or int(suite.get("failures", "0")):
            raise ValueError(f"{name} contains test errors or failures")
        if any(
            case.find("error") is not None or case.find("failure") is not None for case in cases
        ):
            raise ValueError(f"{name} contains a failed test case")
        total += expected
    if total != manifest["case_count"]:
        raise ValueError("Executed case count does not match the collected suite")
    print(f"Verified all {total} collected cases across {len(GROUPS)} test groups")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "verify"))
    parser.add_argument("--output-dir", type=Path, default=Path("test-evidence"))
    args = parser.parse_args()
    try:
        {"collect": collect, "verify": verify}[args.command](args.output_dir)
    except (KeyError, OSError, TypeError, ValueError, ET.ParseError) as error:
        parser.exit(1, f"CI test coverage check failed: {error}\n")


if __name__ == "__main__":
    main()
