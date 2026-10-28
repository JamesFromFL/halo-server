"""Preparation fails closed on unavailable binary wheels and unqualified builds."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "release_input_fixture", Path(__file__).parents[1] / "tools/prepare_release_wheels.py"
)
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)


def locked_demo(*, wheels=None):
    return {
        "package": [
            {
                "name": "halo-server",
                "dependencies": [
                    {"name": "demo"},
                    {"name": "not-on-linux", "marker": "sys_platform == 'win32'"},
                ],
            },
            {
                "name": "demo",
                "version": "1.0.0",
                "wheels": wheels or [],
                "sdist": {
                    "url": "https://example.invalid/demo.tar.gz",
                    "hash": "sha256:" + "a" * 64,
                },
            },
        ]
    }


def test_preparation_selects_only_locked_runtime_binary_compatible_wheel():
    wheel = {
        "url": "https://example.invalid/demo-1.0.0-py3-none-any.whl",
        "hash": "sha256:" + "b" * 64,
        "size": 25,
    }
    records = inputs.select_wheels(locked_demo(wheels=[wheel]))
    assert len(records) == 1 and records[0]["sha256"] == "b" * 64
    assert records[0]["filename"] == "demo-1.0.0-py3-none-any.whl"


def test_preparation_never_silently_builds_source_only_dependency():
    with pytest.raises(ValueError, match="no locked binary wheel"):
        inputs.select_wheels(locked_demo())


@pytest.mark.parametrize("damage", ["source_hash", "wheel_hash", "not_reproduced"])
def test_preparation_refuses_unqualified_source_build_receipt(tmp_path, damage):
    wheel = tmp_path / "demo-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"qualified build fixture")
    receipt = {
        "source_url": "https://example.invalid/demo.tar.gz",
        "source_sha256": "a" * 64,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "reproduced_identical_twice": True,
    }
    receipt[
        {
            "source_hash": "source_sha256",
            "wheel_hash": "wheel_sha256",
            "not_reproduced": "reproduced_identical_twice",
        }[damage]
    ] = False
    (tmp_path / "demo-1.0.0.build.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="unqualified built wheel"):
        inputs.select_wheels(locked_demo(), tmp_path)


def test_oversize_download_never_publishes_partial_wheel(tmp_path, monkeypatch):
    monkeypatch.setattr(inputs.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"too big"))
    record = {
        "filename": "demo.whl",
        "url": "https://example.invalid/demo.whl",
        "size": 2,
        "sha256": "0" * 64,
    }
    with pytest.raises(ValueError, match="exceeds locked size"):
        inputs.download(record, tmp_path)
    assert not list(tmp_path.iterdir())
