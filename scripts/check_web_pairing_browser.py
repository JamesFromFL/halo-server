"""Real HTTPS/session/SSE regression with synthetic pairing metadata only.

No Ring account, HAP listener, camera stream, or real pairing credential is used.
The optional static root permits replaying this check against a retained bundle.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import secrets
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PairingOwner:
    """Small owner RPC fixture; transport/authentication/state-feed remain real."""

    def __init__(self):
        self.reveals = 0
        self.observations = 0
        self.scope = {"profile_id": "11111111-1111-4111-8111-111111111111", "account_generation": 1}
        self.identity = self.scope | {"boot_id": "fixture-adapter", "registry_revision": 1}
        self.cameras = [
            {
                "id": key,
                "camera_id": key,
                "name": name,
                "source": "ring",
                "kind": "camera",
                "model": "Synthetic camera",
                "scope": self.scope | {"device_generation": 1},
                "capabilities": ["video"],
                "observed": {
                    "source_available": True,
                    "battery_percent": None,
                    "light": None,
                    "siren": None,
                },
                "homekit": {
                    "selected": True,
                    "published": True,
                    "paired": None
                    if key == "fixture-camera-unknown"
                    else key == "fixture-camera-paired",
                    "owner_warm": False,
                    "recording_active": False,
                    "recording_audio_active": False,
                },
            }
            for key, name in (
                ("fixture-camera-a", "Fixture front door"),
                ("fixture-camera-b", "Fixture garden"),
                ("fixture-camera-paired", "Fixture paired camera"),
                ("fixture-camera-unknown", "Fixture unknown camera"),
            )
        ]

    async def owner_state(self):
        self.observations += 1
        return {
            "schema": 1,
            "epoch": "fixture-account",
            "observed_at": time.time(),
            "scope": self.scope,
            "daemon": {
                "boot_id": "fixture-daemon",
                "version": "fixture",
                "release": {"managed": False},
            },
            "ring": {
                "authenticated": True,
                "restoring": False,
                "events": "connected",
                "inventory_complete": True,
            },
            "homekit": {
                "enabled": True,
                "adapter_boot_id": self.identity["boot_id"],
                "registry_revision": 1,
                "state": "published",
                "selected_count": len(self.cameras),
                "published_count": len(self.cameras),
                "recording_revision": 1,
            },
            "devices": copy.deepcopy(self.cameras),
            "activity": {
                "epoch": "fixture-activity",
                "cursor": 0,
                "capacity": 256,
                "entries": [],
                "truncated": False,
            },
            "truncated": False,
        }

    async def homekit_pairing_list(self):
        return self.identity | {
            "revision": 1,
            "total": len(self.cameras),
            "cameras": [
                {
                    "camera_id": camera["camera_id"],
                    "accessory_id": "accessory-" + camera["camera_id"],
                    "name": camera["name"],
                    "selected": True,
                    "available": True,
                    "published": True,
                    "paired_known": camera["homekit"]["paired"] is not None,
                    "paired": camera["homekit"]["paired"] is True,
                    "pairing_revision": 1,
                    "pending_reset": False,
                }
                for camera in self.cameras
            ],
        }

    async def homekit_pairing_reveal(self, page, camera):
        assert camera["paired_known"] and not camera["paired"], "Only unpaired cameras can reveal"
        self.reveals += 1
        return {
            "camera_id": camera["camera_id"],
            "pin": "123-45-678",
            "setup_uri": "X-HM://FIXTURE",
            "expires_in_ms": 60000,
        }

    async def homekit_cameras(self):
        return await self.homekit_pairing_list()

    async def homekit_recording_cameras(self):
        return await self.homekit_pairing_list()

    async def camera_controls(self):
        return await self.homekit_pairing_list()

    async def camera_privacy_list(self):
        return self.scope | {
            "available": True,
            "cameras": [
                {
                    "camera_id": camera["camera_id"],
                    "revision": 0,
                    "mode": "ring",
                    "source_fresh": True,
                    "ring_live_allowed": True,
                    "ring_recording_allowed": True,
                    "ring_snapshot_allowed": True,
                    "effective_live_allowed": True,
                    "effective_recording_allowed": True,
                    "effective_snapshot_allowed": True,
                    "reason": None,
                    "ring_audio_allowed": True,
                    "effective_audio_allowed": True,
                }
                for camera in self.cameras
            ],
        }

    async def configuration_get(self):
        return {"saved": {}, "applied": {}, "restart_required": False}


async def check(output, *, installed=False, static_root=None):
    if not installed:
        sys.path.insert(0, str(ROOT / "src"))
    from aiohttp import web

    import halo_server
    from halo_server.paths import ServerPaths
    from halo_server.web.auth import issue_setup
    from halo_server.web.routes import WebConsole, create_application
    from halo_server.web.settings import SettingsStore, configure, tls_context

    package = Path(halo_server.__file__).resolve().parent
    if installed and package == ROOT / "src/halo_server":
        raise RuntimeError("Installed pairing check imported the source checkout")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="halo-pairing-browser-") as directory:
        root = Path(directory)
        paths = ServerPaths(root / "state", root / "run")
        paths.prepare()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        configure(paths, host="127.0.0.1", port=port)
        enrollment = issue_setup(paths)
        settings = SettingsStore(paths).load()
        owner = PairingOwner()
        console = WebConsole(paths, settings, client=owner)
        runner = web.AppRunner(create_application(console, static_root=static_root))
        browser = None
        await runner.setup()
        try:
            await web.TCPSite(
                runner, "127.0.0.1", port, ssl_context=tls_context(paths, settings)
            ).start()
            environment = os.environ.copy()
            environment.update(
                HALO_TEST_URL=f"https://127.0.0.1:{port}",
                HALO_TEST_PASSWORD=secrets.token_urlsafe(32),
                HALO_SETUP_CODE=enrollment["setup_code"],
                HALO_BROWSER_OUTPUT=str(output),
            )
            browser = await asyncio.create_subprocess_exec(
                "node", "scripts/pairing-smoke.mjs", cwd=ROOT / "web-ui", env=environment
            )
            async with asyncio.timeout(180):
                result = await browser.wait()
            if result:
                raise RuntimeError(f"Pairing browser regression failed with exit status {result}")
        finally:
            if browser is not None and browser.returncode is None:
                browser.terminate()
                await browser.wait()
            await runner.cleanup()
            (output / "fixture.json").write_text(
                json.dumps(
                    {
                        "scope": "real HTTPS/authentication/SSE; synthetic owner pairing only",
                        "installed_package": installed,
                        "owner_observations": owner.observations,
                        "synthetic_reveals": owner.reveals,
                        "media_sessions": 0,
                        "hap_listeners": 0,
                    },
                    indent=2,
                )
                + "\n"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--static-root", type=Path)
    options = parser.parse_args()
    asyncio.run(
        check(
            options.output.resolve(), installed=options.installed, static_root=options.static_root
        )
    )


if __name__ == "__main__":
    main()
