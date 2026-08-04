import ast
from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input, Static

from halo_server.tui.app import HaloTui
from halo_server.tui.client import AdminClientError


def _status(*, authenticated=False):
    return {
        "account": {
            "authenticated": authenticated,
            "email": "person@example.com" if authenticated else None,
            "restoring": False,
            "last_error": None,
        },
        "events": {
            "connection": "connected" if authenticated else "offline",
            "recent": [
                {
                    "camera_name": "Front Door",
                    "camera_id": "42",
                    "event_id": "101",
                    "type": "motion",
                    "occurred_at": 1_700_000_000,
                    "is_update": False,
                }
            ],
        },
        "cameras": [
            {
                "id": "42",
                "name": "Front Door",
                "model": "Doorbell Pro",
                "kind": "lpd_v4",
                "online": True,
                "battery_percent": 88,
                "motion_detection": True,
            }
        ],
        "incidents": {},
        "media": {"control": "not_configured", "active_upstreams": 0},
        "recent_actions": [],
    }


class FakeAdminClient:
    def __init__(self):
        self.login_calls = []
        self.two_factor_required = False
        self.pair_calls = []
        self.revoke_calls = []
        self.clients = []

    async def status(self):
        status = _status()
        status["integrations"] = {
            "clients": list(self.clients),
            "protocol_version": 2,
            "error": None,
        }
        return status

    async def login(self, email, password, otp):
        self.login_calls.append((email, password, otp))
        if self.two_factor_required and otp is None:
            raise AdminClientError("two_factor_required", "Two factor required")
        return _status(authenticated=True)

    async def logout(self):
        return _status()

    async def pair_plugin(self, label):
        self.pair_calls.append(label)
        client = {"id": "a" * 32, "label": label, "created_at": 100}
        self.clients.append(client)
        return {"client": client, "token": f"halo1.{'a' * 32}.{'b' * 43}"}

    async def revoke_plugin(self, client_id):
        self.revoke_calls.append(client_id)
        self.clients = [client for client in self.clients if client["id"] != client_id]
        return {"client_id": client_id, "revoked": True}


@pytest.mark.asyncio
async def test_tui_renders_status_camera_and_event_tables():
    app = HaloTui(FakeAdminClient())

    async with app.run_test() as pilot:
        await pilot.pause()

        assert "signed out" in str(app.query_one("#account-status", Static).render())
        assert app.query_one("#cameras", DataTable).row_count == 1
        assert app.query_one("#events", DataTable).row_count == 1
        assert app.query_one("#logout", Button).disabled is True


@pytest.mark.asyncio
async def test_tui_two_factor_flow_retains_password_only_until_completion():
    client = FakeAdminClient()
    client.two_factor_required = True
    app = HaloTui(client)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#email", Input).value = "person@example.com"
        app.query_one("#password", Input).value = "secret"
        await app._login()

        otp = app.query_one("#otp", Input)
        assert otp.display is True
        assert app.query_one("#password", Input).value == "secret"

        otp.value = "123456"
        await app._login()

        assert client.login_calls[-1] == (
            "person@example.com",
            "secret",
            "123456",
        )
        assert app.query_one("#password", Input).value == ""
        assert otp.value == ""
        assert otp.display is False


def test_tui_modules_do_not_import_ring_session_or_media_owners():
    tui_root = Path(__file__).parents[1] / "src/halo_server/tui"
    imported = set()
    for path in tui_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

    assert not any(name.startswith("halo_server.ring") for name in imported)
    assert not any(name.startswith("halo_server.media") for name in imported)


@pytest.mark.asyncio
async def test_tui_pairs_displays_once_and_revokes_plugin_credential():
    client = FakeAdminClient()
    app = HaloTui(client)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#plugin-label", Input).value = "Scrypted"

        await app._pair_plugin()

        token = app.query_one("#plugin-token", Input)
        assert token.value == f"halo1.{'a' * 32}.{'b' * 43}"
        assert app.query_one("#integrations", DataTable).row_count == 1
        assert client.pair_calls == ["Scrypted"]

        await app._revoke_plugin()

        assert client.revoke_calls == ["a" * 32]
        assert app.query_one("#integrations", DataTable).row_count == 0
