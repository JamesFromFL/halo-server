"""Release assembly refuses stale camera-only builds before dependency work."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "device_release_builder", Path(__file__).parents[1] / "tools/build_release.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize("damage", ["missing", "empty", "symlink", "directory"])
def test_device_entry_is_required_before_any_packaging(tmp_path, damage):
    tree = tmp_path / "adapter" / "dist" / "src"
    tree.mkdir(parents=True)
    for name in ("main.js", "pairing-reset.js"):
        (tree / name).write_text("// qualified source")
    child = tree / "device-main.js"
    if damage == "empty":
        child.touch()
    elif damage == "symlink":
        child.symlink_to(tree / "main.js")
    elif damage == "directory":
        child.mkdir()
    with pytest.raises(ValueError, match="required production entry"):
        builder.install_adapter(
            tmp_path / "adapter", tmp_path / "absent-npm", tmp_path / "cache", tmp_path / "out"
        )
    assert not (tmp_path / "out").exists()


def test_both_adapter_entry_points_pass_preflight(tmp_path):
    tree = tmp_path / "dist/src"
    tree.mkdir(parents=True)
    for name in ("main.js", "device-main.js", "pairing-reset.js"):
        (tree / name).write_text("// qualified source")
    builder.validate_adapter_entries(tmp_path)
