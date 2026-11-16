import copy
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_test_groups.py"
spec = importlib.util.spec_from_file_location("ci_test_groups", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ITEMS = [
    ("tests/hub/test_hub.py::test_one", "tests/hub/test_hub.py"),
    ("tests/test_account.py::test_one", "tests/test_account.py"),
    ("tests/test_homekit.py::test_one", "tests/test_homekit.py"),
    ("tests/test_media.py::test_one", "tests/test_media.py"),
    ("tests/test_web.py::test_one", "tests/test_web.py"),
]


@pytest.fixture
def reports(tmp_path):
    manifest = module.partition(ITEMS)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for name, group in manifest["groups"].items():
        (tmp_path / f"{name}.txt").write_text("\n".join(group["files"]) + "\n")
        root = ET.Element("testsuites")
        suite = ET.SubElement(
            root,
            "testsuite",
            tests=str(len(group["nodeids"])),
            errors="0",
            failures="0",
        )
        for nodeid in group["nodeids"]:
            case = ET.SubElement(suite, "testcase", name=nodeid)
            if name == "group-4":
                ET.SubElement(case, "skipped")
        ET.ElementTree(root).write(tmp_path / f"{name}.xml")
    return tmp_path


def test_valid_partition_is_disjoint_exhaustive_and_includes_hub():
    manifest = module.partition(ITEMS)
    assert manifest["case_count"] == 5
    assert [len(g["nodeids"]) for g in manifest["groups"].values()] == [2, 1, 1, 1]
    assert manifest["groups"]["group-1"]["files"][0] == "tests/hub/test_hub.py"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "tests/other_test.py",
        "tests/test_Upper.py",
        "../tests/test_alpha.py",
        "/tests/test_alpha.py",
        "other/test_alpha.py",
    ],
)
def test_rejects_unrecognized_collected_path(path):
    with pytest.raises(ValueError):
        module.group_for_file(path)


@pytest.mark.parametrize(
    "items",
    [
        ITEMS + [ITEMS[0]],
        ITEMS[:-1],
        [("tests/test_wrong.py::case", ITEMS[0][1]), *ITEMS[1:]],
    ],
)
def test_rejects_duplicate_missing_or_inconsistent_collection(items):
    with pytest.raises(ValueError):
        module.partition(items)


def test_valid_junit_reports_count_skipped_cases(reports):
    module.verify(reports)


@pytest.mark.parametrize(
    "fault",
    [
        "total",
        "cross_group",
        "manifest_files",
        "argument_file",
        "missing_report",
        "reported_count",
        "actual_count",
        "failures",
        "failure_element",
        "duplicate_suite",
    ],
)
def test_verification_refuses_incomplete_or_changed_evidence(reports, fault):
    manifest_path = reports / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    report_path = reports / "group-1.xml"
    tree = ET.parse(report_path)
    suite = tree.getroot()[0]
    if fault == "total":
        manifest["case_count"] += 1
    elif fault == "cross_group":
        manifest["groups"]["group-2"]["nodeids"].append(ITEMS[0][0])
    elif fault == "manifest_files":
        manifest["groups"]["group-1"]["files"].pop()
    elif fault == "argument_file":
        (reports / "group-1.txt").write_text("tests/test_account.py\n")
    elif fault == "missing_report":
        report_path.unlink()
    elif fault == "reported_count":
        suite.set("tests", "1")
    elif fault == "actual_count":
        suite.remove(suite[0])
    elif fault == "failures":
        suite.set("failures", "1")
    elif fault == "failure_element":
        ET.SubElement(suite[0], "failure")
    elif fault == "duplicate_suite":
        tree.getroot().append(copy.deepcopy(suite))
    manifest_path.write_text(json.dumps(manifest))
    if fault != "missing_report":
        tree.write(report_path)
    with pytest.raises((ValueError, OSError)):
        module.verify(reports)
