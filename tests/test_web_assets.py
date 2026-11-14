"""Packaging fails closed on missing, stale, modified, or escaped browser assets."""

import hashlib
import json

import pytest

from halo_server.web_assets import WebBundleError, validate_bundle


@pytest.fixture
def bundle(tmp_path):
    static, source = tmp_path / "static", tmp_path / "web-ui"
    static.mkdir()
    source.mkdir()
    (source / "package-lock.json").write_text("{}")
    for name in ("index.html", "halo.png", "THIRD_PARTY_NOTICES.txt"):
        (static / name).write_bytes(b"fixture")

    def records(root):
        return [
            {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(root.iterdir())
        ]

    sources = records(source)
    manifest = {
        "schema": 1,
        "source_files": sources,
        "source_hash": hashlib.sha256(
            json.dumps(sources, separators=(",", ":")).encode()
        ).hexdigest(),
        "assets": records(static),
    }
    (static / "build-manifest.json").write_text(json.dumps(manifest))
    return static, source


def test_installed_assets_and_exact_build_sources(bundle):
    static, source = bundle
    assert len(validate_bundle(static, source=source)) == 3
    (source / "package-lock.json").write_text('{"changed":true}')
    with pytest.raises(WebBundleError, match="rebuild"):
        validate_bundle(static, source=source)
    assert len(validate_bundle(static)) == 3


@pytest.mark.parametrize("change", ["extra", "missing", "modified", "symlink", "escape"])
def test_invalid_static_bundle_is_never_served(bundle, change):
    static, _ = bundle
    if change == "extra":
        (static / "secret.json").write_text("private fixture")
    elif change == "missing":
        (static / "index.html").unlink()
    elif change == "modified":
        (static / "index.html").write_text("injected")
    elif change == "symlink":
        (static / "index.html").unlink()
        (static / "index.html").symlink_to(static / "halo.png")
    else:
        path = static / "build-manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["assets"][0]["path"] = "../outside"
        path.write_text(json.dumps(manifest))
    with pytest.raises(WebBundleError):
        validate_bundle(static)
