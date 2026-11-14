"""Packaging fails closed on missing, stale, modified, or escaped browser assets."""

import hashlib
import json
import os
from email.utils import formatdate

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from halo_server.web.routes import _static
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


def release_bundle(root, revision):
    """Different valid releases with the same reproducible timestamp and sizes."""
    root.mkdir()
    (root / "assets").mkdir()
    values = {
        "index.html": f'<html><script src="/assets/index-{revision}.js"></script></html>'.encode(),
        "halo.png": f"image-{revision}".encode(),
        "THIRD_PARTY_NOTICES.txt": f"notice-{revision}".encode(),
        f"assets/index-{revision}.js": f"window.release = '{revision}';".encode(),
    }
    for name, value in values.items():
        path = root / name
        path.write_bytes(value)
        os.utime(path, ns=(1793145600000000000, 1793145600000000000))
    sources = [{"path": "package-lock.json", "sha256": hashlib.sha256(b"{}").hexdigest()}]
    manifest = {
        "schema": 1,
        "source_files": sources,
        "source_hash": hashlib.sha256(
            json.dumps(sources, separators=(",", ":")).encode()
        ).hexdigest(),
        "assets": [
            {"path": name, "sha256": hashlib.sha256(value).hexdigest()}
            for name, value in sorted(values.items())
        ],
    }
    (root / "build-manifest.json").write_text(json.dumps(manifest))
    return values


@pytest.mark.asyncio
async def test_upgrade_never_reuses_an_index_with_colliding_file_validators(tmp_path):
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old_values = release_bundle(old_root, "aaaaaaaa")
    new_values = release_bundle(new_root, "bbbbbbbb")
    old_stat, new_stat = (old_root / "index.html").stat(), (new_root / "index.html").stat()
    assert old_values["index.html"] != new_values["index.html"]
    assert (old_stat.st_size, old_stat.st_mtime_ns) == (new_stat.st_size, new_stat.st_mtime_ns)
    legacy = {
        "If-None-Match": f'"{old_stat.st_mtime_ns:x}-{old_stat.st_size:x}"',
        "If-Modified-Since": formatdate(old_stat.st_mtime, usegmt=True),
    }
    app = web.Application()
    _static(app, new_root)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        for path in ("/", "/index.html"):
            for headers in (
                legacy,
                {key: value for key, value in list(legacy.items())[:1]},
                {key: value for key, value in list(legacy.items())[1:]},
            ):
                response = await client.get(server.make_url(path), headers=headers)
                assert response.status == 200
                assert await response.read() == new_values["index.html"]
                assert response.headers["Cache-Control"] == "no-store"
                assert "ETag" not in response.headers and "Last-Modified" not in response.headers
            response = await client.head(server.make_url(path), headers=legacy)
            assert response.status == 200 and await response.read() == b""
            assert response.headers["Cache-Control"] == "no-store"
        response = await client.get(server.make_url("/assets/index-bbbbbbbb.js"))
        assert response.status == 200
        assert await response.read() == new_values["assets/index-bbbbbbbb.js"]
        assert "max-age=3600" in response.headers["Cache-Control"]
        response = await client.get(server.make_url("/assets/index-aaaaaaaa.js"))
        assert response.status == 404


@pytest.mark.asyncio
async def test_stable_assets_revalidate_contents_across_reproducible_releases(tmp_path):
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old_values = release_bundle(old_root, "aaaaaaaa")
    new_values = release_bundle(new_root, "bbbbbbbb")
    app = web.Application()
    _static(app, new_root)
    async with TestServer(app) as server, aiohttp.ClientSession() as client:
        for name in ("halo.png", "THIRD_PARTY_NOTICES.txt"):
            old_stat = (old_root / name).stat()
            old_digest = hashlib.sha256(old_values[name]).hexdigest()
            new_digest = hashlib.sha256(new_values[name]).hexdigest()
            old_headers = {
                "If-None-Match": f'"{old_digest}"',
                "If-Modified-Since": formatdate(old_stat.st_mtime, usegmt=True),
            }
            for headers in (
                old_headers,
                {"If-Modified-Since": old_headers["If-Modified-Since"]},
                {"If-None-Match": f'"{old_stat.st_mtime_ns:x}-{old_stat.st_size:x}"'},
            ):
                response = await client.get(server.make_url("/" + name), headers=headers)
                assert response.status == 200 and await response.read() == new_values[name]
                assert response.headers["ETag"] == f'"{new_digest}"'
                assert response.headers["Cache-Control"] == "no-cache"
                assert "Last-Modified" not in response.headers
            for validator in (f'"{new_digest}"', f'W/"{new_digest}"', "*"):
                response = await client.get(
                    server.make_url("/" + name), headers={"If-None-Match": validator}
                )
                assert response.status == 304 and await response.read() == b""
                assert response.headers["ETag"] == f'"{new_digest}"'
                assert response.headers["Cache-Control"] == "no-cache"
