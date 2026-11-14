"""Exercise an ordinary cached reload across releases with identical file metadata.

Only synthetic loopback HTTPS bundles are served. No credentials or Ring state
are used. The old server deliberately uses the pre-fix FileResponse policy.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bundle(root: Path, version: str):
    root.mkdir()
    (root / "assets").mkdir()
    (root / "index.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<script type="module" src="/assets/{version}.js"></script>'
        "</head><body><h1>Loading fixture</h1></body></html>"
    )
    (root / f"assets/{version}.js").write_text(
        f'document.querySelector("h1").textContent = "{version} release";'
    )
    (root / "halo.png").write_bytes(b"synthetic asset")
    (root / "THIRD_PARTY_NOTICES.txt").write_text("Synthetic fixture only")
    sources = [{"path": "fixture", "sha256": "0" * 64}]
    manifest = {
        "schema": 1,
        "source_files": sources,
        "source_hash": hashlib.sha256(
            json.dumps(sources, separators=(",", ":")).encode()
        ).hexdigest(),
        "assets": [
            {"path": str(p.relative_to(root)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(root.rglob("*"))
            if p.is_file()
        ],
    }
    (root / "build-manifest.json").write_text(json.dumps(manifest))
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (1793145600, 1793145600))


async def check(output: Path, *, installed=False):
    if not installed:
        sys.path.insert(0, str(ROOT / "src"))
    import halo_server

    if installed and Path(halo_server.__file__).resolve().is_relative_to(ROOT / "src"):
        raise RuntimeError("Installed upgrade check imported the source checkout")
    from aiohttp import web

    from halo_server.paths import ServerPaths
    from halo_server.web.routes import _static
    from halo_server.web.settings import SettingsStore, configure, tls_context

    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="halo-cache-upgrade-") as temporary:
        root = Path(temporary)
        before, after = root / "before", root / "after"
        bundle(before, "old")
        bundle(after, "new")
        old, new = before / "index.html", after / "index.html"
        assert old.read_bytes() != new.read_bytes()
        assert old.stat().st_size == new.stat().st_size
        assert old.stat().st_mtime_ns == new.stat().st_mtime_ns
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        paths = ServerPaths(root / "state", root / "run")
        paths.prepare()
        configure(paths, host="127.0.0.1", port=port)
        tls = tls_context(paths, SettingsStore(paths).load())
        runner = process = None
        try:

            async def legacy(request):
                name = request.match_info.get("asset") or "index.html"
                if name not in ("index.html", "assets/old.js"):
                    raise web.HTTPNotFound()
                response = web.FileResponse(before / name)
                response.headers["Cache-Control"] = (
                    "no-cache" if name == "index.html" else "public, max-age=3600"
                )
                return response

            app = web.Application()
            app.router.add_get("/{asset:.*}", legacy)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port, ssl_context=tls).start()
            environment = dict(
                os.environ,
                HALO_TEST_URL=f"https://127.0.0.1:{port}",
                HALO_BROWSER_OUTPUT=str(output),
            )
            process = await asyncio.create_subprocess_exec(
                "node",
                str(ROOT / "web-ui/scripts/upgrade-smoke.mjs"),
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async with asyncio.timeout(40):
                first = await process.stdout.readline()
            if first.strip() != b"READY_FOR_UPGRADE":
                raise RuntimeError(
                    "Browser did not cache the old release: " + first.decode(errors="replace")
                )
            await runner.cleanup()
            runner = None
            app = web.Application()
            _static(app, after)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port, ssl_context=tls).start()
            process.stdin.write(b"new release ready\n")
            await process.stdin.drain()
            async with asyncio.timeout(40):
                remainder, _ = await process.communicate()
            (output / "browser.log").write_bytes(remainder)
            if process.returncode:
                raise RuntimeError(
                    "Cached upgrade browser check failed: " + remainder.decode(errors="replace")
                )
            result = json.loads((output / "upgrade-smoke.json").read_text())
            result["same_index_size_and_mtime"] = True
            result["old_policy_was_real_file_response"] = True
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result))
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            if runner:
                await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    asyncio.run(check(args.output.resolve(), installed=args.installed))
