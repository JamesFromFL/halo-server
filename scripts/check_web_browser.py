"""Exercise the real HTTPS console with isolated, synthetic owner state.

The browser receives only test credentials. No Ring login or real camera is used.
Run after building web-ui, with Playwright's pinned Chromium installed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class Camera:
    family = "stickup_cams"
    kind = "stickup_cam_mini"
    model = "Synthetic camera"
    subscribed = True
    battery_life = None

    def __init__(self, camera_id):
        self.id, self.name = camera_id, camera_id
        self.light_values = []
        self.snapshot_calls = 0

    def has_capability(self, capability):
        return capability in {"video", "light"}

    async def async_set_light(self, enabled):
        self.light_values.append(enabled)

    async def async_get_snapshot(self, **kwargs):
        self.snapshot_calls += 1
        raise AssertionError("The management UI must not request camera snapshots")

    async def generate_async_webrtc_stream(self):
        raise AssertionError("The management UI must not open a Ring stream")

    async def on_webrtc_candidate(self): ...
    async def close_webrtc_stream(self): ...
    async def get_ice_servers(self): ...


class Client:
    is_authenticated = True

    def __init__(self, cameras):
        self.all_devices, self.submissions = cameras, 0

    def submit(self, coroutine):
        self.submissions += 1
        return asyncio.run_coroutine_threadsafe(coroutine, asyncio.get_running_loop())


class Sessions:
    account_email = "fixture@example.invalid"

    def __init__(self, client, scope):
        self.active_client, self.active_scope = client, scope

    @property
    def active_context(self):
        return (self.active_client, self.active_scope) if self.active_client else None

    def logout(self):
        self.active_client = None

    shutdown = logout


def forbid_media(*args, **kwargs):
    raise AssertionError("The management UI must not construct a media session")


async def check(output, *, installed=False):
    if not installed:
        sys.path.insert(0, str(ROOT / "src"))
    import halo_server
    from halo_server.account_scope import AccountScope
    from halo_server.configuration import ConfigurationStore, ServerConfiguration, ServerMode
    from halo_server.owner_client import AdminClient, AdminClientError
    from halo_server.paths import ServerPaths
    from halo_server.service import HaloService
    from halo_server.web import auth, server, settings
    from halo_server.web_assets import validate_bundle

    package = Path(halo_server.__file__).resolve().parent
    if installed and package == ROOT / "src/halo_server":
        raise RuntimeError("Installed browser check unexpectedly imported the source checkout")
    validate_bundle(package / "web/static", source=None if installed else ROOT / "web-ui")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="halo-web-browser-") as directory:
        root = Path(directory)
        cameras = [Camera(str(8000 + index)) for index in range(7)]
        for camera, name in zip(
            cameras,
            ("Front door", "Garden floodlight", "Garage", "Hallway", "Patio", "Kitchen", "Porch"),
            strict=True,
        ):
            camera.name = name
        client = Client(cameras)
        sessions = Sessions(client, AccountScope(str(uuid4()), 1))
        service = HaloService(
            ServerPaths(root / "state", root / "run"),
            session_manager=sessions,
            configuration=ServerConfiguration(mode=ServerMode.HOMEKIT),
            media_session_factory=forbid_media,
        )
        service._media_client, service._media_scope = client, sessions.active_scope
        service._account_integrations_ready = True
        service._homekit_bridge._publication_enabled = False
        ConfigurationStore(service.paths.configuration_file).save(service.configuration)
        page = await service.handle_admin("homekit_list", {})
        await service.handle_admin(
            "homekit_select",
            {
                "profile_id": page["profile_id"],
                "account_generation": page["account_generation"],
                "expected_revision": page["revision"],
                "camera_ids": [row["camera_id"] for row in page["cameras"]],
            },
        )
        service.paths.prepare()
        await service._admin.start()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings.configure(service.paths, host="127.0.0.1", port=port)
        enrollment = auth.issue_setup(service.paths)
        stop = asyncio.Event()
        companion = asyncio.create_task(server.serve(service.paths, stop_event=stop))
        browser = None
        try:
            health = AdminClient(service.paths.runtime_directory / "web-admin.sock", timeout=1)
            async with asyncio.timeout(15):
                while True:
                    if companion.done():
                        await companion
                        raise RuntimeError("Test companion stopped before becoming ready")
                    try:
                        if (await health.request("status"))["web"]["ready"]:
                            break
                    except AdminClientError:
                        pass
                    await asyncio.sleep(0.05)
            environment = os.environ.copy()
            environment.update(
                HALO_TEST_URL=f"https://127.0.0.1:{port}",
                HALO_TEST_PASSWORD=secrets.token_urlsafe(32),
                HALO_SETUP_CODE=enrollment["setup_code"],
                HALO_BROWSER_OUTPUT=str(output),
            )
            browser = await asyncio.create_subprocess_exec(
                "node", "scripts/browser-smoke.mjs", cwd=ROOT / "web-ui", env=environment
            )
            async with asyncio.timeout(240):
                result = await browser.wait()
            if result:
                raise RuntimeError(f"Browser acceptance failed with exit status {result}")
        finally:
            if browser is not None and browser.returncode is None:
                browser.terminate()
                await browser.wait()
            stop.set()
            await companion
            await service._admin.stop()
            await service._owner_admin.close()
            if service._owner_tasks:
                await asyncio.gather(*service._owner_tasks)
            if service._homekit_bridge:
                await service._homekit_bridge.stop()
            metrics = {
                "scope": "isolated real HTTPS companion with synthetic Ring devices",
                "installed_package": installed,
                "sdk_control_submissions": client.submissions,
                "snapshot_requests": sum(camera.snapshot_calls for camera in cameras),
                "active_media_sessions": len(service._media_sessions),
                "light_commands": [value for camera in cameras for value in camera.light_values],
            }
            (output / "fixture-media.json").write_text(json.dumps(metrics, indent=2) + "\n")
            assert metrics["snapshot_requests"] == 0
            assert metrics["active_media_sessions"] == 0
            assert client.submissions == len(metrics["light_commands"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    options = parser.parse_args()
    asyncio.run(check(options.output.resolve(), installed=options.installed))


if __name__ == "__main__":
    main()
