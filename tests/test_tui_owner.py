"""Owner-visible administration, scoped setup reveal, CAS and literal UI text."""

import asyncio
import copy
from uuid import UUID

import pytest
from rich.text import Text
from test_tui import FakeAdminClient
from textual.widgets import Button, DataTable, Input, Select, Static

from halo_server.configuration import ServerConfiguration
from halo_server.tui.app import HaloTui
from halo_server.tui.client import AdminClientError
from halo_server.tui.homekit import HomeKitPanel

PROFILE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BOOT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ACCESSORY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


class OwnerClient(FakeAdminClient):
    def __init__(self):
        super().__init__()
        self.authenticated = True
        self.calls = []
        self.view = {
            "profile_id": PROFILE,
            "account_generation": 2,
            "revision": 3,
            "offset": 0,
            "total": 2,
            "cameras": [
                {
                    "camera_id": "42",
                    "accessory_id": ACCESSORY,
                    "username": "AA:BB:CC:DD:EE:FF",
                    "name": "[bold]Front[/bold]",
                    "available": True,
                    "selected": True,
                },
                {
                    "camera_id": "43",
                    "accessory_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "username": "AA:BB:CC:DD:EE:00",
                    "name": "Absent",
                    "available": False,
                    "selected": False,
                },
            ],
        }
        self.recording = {
            "profile_id": PROFILE,
            "account_generation": 2,
            "revision": 5,
            "offset": 0,
            "total": 1,
            "cameras": [
                {
                    "camera_id": "42",
                    "owner_warm": False,
                    "mains_confirmed": True,
                    "warm_state": "disabled",
                }
            ],
        }
        self.pairing = {
            "profile_id": PROFILE,
            "account_generation": 2,
            "registry_revision": 3,
            "boot_id": BOOT,
            "available": True,
            "offset": 0,
            "total": 1,
            "cameras": [
                self.view["cameras"][0]
                | {
                    "published": True,
                    "paired": False,
                    "paired_known": True,
                    "paired_clients": 0,
                    "pairing_revision": "a" * 64,
                    "pending_reset": False,
                }
            ],
        }
        saved = ServerConfiguration(mode="homekit", revision=4).as_dict()
        self.configuration = {
            "saved": saved,
            "applied": ServerConfiguration().as_dict(),
            "restart_required": True,
        }
        self.controls = {
            "profile_id": PROFILE,
            "account_generation": 2,
            "cameras": [
                {
                    "camera_id": "42",
                    "device_generation": 7,
                    "name": "[red]Control[/red]",
                    "has_light": True,
                    "has_siren": False,
                    "light": None,
                    "siren": None,
                }
            ],
        }
        self.reveal_gate = None

    async def status(self):
        value = await super().status()
        value["configuration"] = {"mode": "homekit"}
        value["homekit"] = {
            "state": "control_ready",
            "boot_id": self.pairing["boot_id"],
            "restart_count": 0,
            "registry_revision": self.view["revision"],
        }
        return value

    async def configuration_get(self):
        return copy.deepcopy(self.configuration)

    async def configuration_set(self, revision, configuration):
        self.calls.append(("configuration", revision, configuration))
        if revision != self.configuration["saved"]["revision"]:
            raise AdminClientError("configuration_conflict", "Settings changed; reload")
        self.configuration["saved"] = (
            self.configuration["saved"] | configuration | {"revision": revision + 1}
        )
        return copy.deepcopy(self.configuration)

    async def camera_controls(self):
        return copy.deepcopy(self.controls)

    async def camera_control(self, *args):
        self.calls.append(("control", *args))
        return {"status": "unknown", "code": "timeout"}

    async def homekit_cameras(self):
        return copy.deepcopy(self.view)

    async def homekit_recording_cameras(self):
        return copy.deepcopy(self.recording)

    async def homekit_pairing_list(self):
        return copy.deepcopy(self.pairing)

    async def homekit_select(self, profile, generation, ids, revision):
        self.calls.append(("select", profile, generation, ids, revision))
        assert revision == self.view["revision"]
        self.view["revision"] += 1
        self.pairing["registry_revision"] += 1
        for row in self.view["cameras"]:
            row["selected"] = row["camera_id"] in ids
        return {"saved": True, "revision": self.view["revision"]}

    async def homekit_recording_warm(self, *args):
        self.calls.append(("warm", *args))
        self.recording["revision"] += 1
        self.recording["cameras"][0]["owner_warm"] = args[-1]
        return {"saved": True, "revision": self.recording["revision"]}

    async def homekit_pairing_reveal(self, page, row):
        self.calls.append(("reveal", page["boot_id"], row["camera_id"]))
        if self.reveal_gate is not None:
            await self.reveal_gate.wait()
        return {"pin": "123-45-678", "setup_uri": "X-HM://012345678ABCD", "expires_in_ms": 60000}

    async def homekit_pairing_reset(self, page, row, operation):
        UUID(operation)
        self.calls.append(("reset", row["camera_id"], page["registry_revision"], operation))
        return {
            "operation_id": operation,
            "status": "completed",
            "code": "ok",
            "restart_required": False,
        }

    async def doctor(self):
        return {"schema": 1, "status": "ready", "checks": []}

    async def support_report(self):
        return {"schema": 1, "kind": "support", "diagnostics": {"safe": True}}


@pytest.mark.asyncio
async def test_owner_TUI_selection_warm_controls_settings_and_literal_rows():
    client = OwnerClient()
    app = HaloTui(client)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        panel = app.query_one(HomeKitPanel)
        table = panel.query_one("#homekit-cameras", DataTable)
        assert table.row_count == 2 and isinstance(table.get_row_at(0)[0], Text)
        assert str(table.get_row_at(0)[0]) == "[bold]Front[/bold]"
        assert "Saved: homekit" in str(panel.query_one("#mode-summary", Static).render())
        assert "Applied: compatibility" in str(panel.query_one("#mode-summary", Static).render())
        await panel._action("homekit-warm-on")
        assert ("warm", PROFILE, 2, "42", 5, True) in client.calls
        await panel._action("homekit-deselect")
        assert ("select", PROFILE, 2, [], 3) in client.calls
        panel.query_one("#control-camera", Select).value = "42"
        await pilot.pause()
        assert panel.query_one("#siren-on", Button).disabled
        await panel._action("light-on")
        control = next(call for call in client.calls if call[0] == "control")
        assert control[1:5] == (PROFILE, 2, "42", 7) and control[-2:] == ("light", True)
        assert "unknown" in str(app.query_one("#message", Static).render())


@pytest.mark.asyncio
async def test_setup_reveal_is_explicit_and_expires_without_reset(monkeypatch):
    monkeypatch.setattr("halo_server.tui.homekit.PAIRING_REVEAL_SECONDS", 0.05)
    client = OwnerClient()
    app = HaloTui(client)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        panel = app.query_one(HomeKitPanel)
        assert panel.query_one("#homekit-pin", Input).value == ""
        assert not any(call[0] == "reveal" for call in client.calls)
        await panel.reveal()
        assert panel.query_one("#homekit-pin", Input).value == "123-45-678"
        await pilot.pause(0.1)
        assert panel.query_one("#homekit-pin", Input).value == ""
        assert panel.query_one("#homekit-uri", Input).value == ""
        assert not any(call[0] == "reset" for call in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["camera", "boot", "account", "deselection", "disconnect", "logout", "pairing", "source"],
)
async def test_setup_secret_clears_on_every_owner_identity_boundary(change):
    client = OwnerClient()
    app = HaloTui(client)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        panel = app.query_one(HomeKitPanel)
        await panel.reveal()
        assert panel.query_one("#homekit-pin", Input).value
        if change == "camera":
            panel.query_one("#homekit-cameras", DataTable).move_cursor(row=1)
            await pilot.pause()
        elif change == "boot":
            client.pairing["boot_id"] = str(UUID(int=55))
            await app._refresh()
        elif change == "account":
            value = await client.status()
            value["account"]["email"] = "replacement@example.com"
            app._apply_status(value)
        elif change == "deselection":
            client.view["cameras"][0]["selected"] = False
            await panel.refresh_owner()
        elif change == "disconnect":
            app._mark_unavailable()
        elif change == "pairing":
            client.pairing["cameras"][0]["pairing_revision"] = "b" * 64
            await panel.refresh_owner()
        elif change == "source":
            client.pairing["cameras"][0]["device_generation"] = 8
            await panel.refresh_owner()
        else:
            await app._logout()
        assert panel.query_one("#homekit-pin", Input).value == ""
        assert panel.query_one("#homekit-uri", Input).value == ""


@pytest.mark.asyncio
async def test_late_setup_result_cannot_reveal_after_revocation():
    client = OwnerClient()
    client.reveal_gate = asyncio.Event()
    app = HaloTui(client)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        panel = app.query_one(HomeKitPanel)
        request = asyncio.create_task(panel.reveal())
        await asyncio.sleep(0)
        panel.clear_secret()
        client.reveal_gate.set()
        with pytest.raises(AdminClientError, match="authority changed"):
            await request
        assert panel.query_one("#homekit-pin", Input).value == ""


@pytest.mark.asyncio
async def test_reset_requires_camera_specific_confirmation_and_clears_setup():
    client = OwnerClient()
    app = HaloTui(client)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        panel = app.query_one(HomeKitPanel)
        await panel.reveal()
        with pytest.raises(ValueError, match="Type RESET"):
            await panel._action("homekit-reset")
        assert not any(call[0] == "reset" for call in client.calls)
        panel.query_one("#homekit-reset-confirm", Input).value = "RESET"
        await panel._action("homekit-reset")
        assert next(call for call in client.calls if call[0] == "reset")[1:3] == ("42", 3)
        assert panel.query_one("#homekit-pin", Input).value == ""


@pytest.mark.asyncio
async def test_automatic_refresh_preserves_unsaved_fields_and_original_CAS_token():
    client = OwnerClient()
    app = HaloTui(client)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        panel = app.query_one(HomeKitPanel)
        panel.query_one("#plugin-port", Input).value = "22222"
        client.configuration["saved"]["revision"] += 1
        client.configuration["saved"]["plugin_port"] = 33333
        await app._refresh()
        assert panel.query_one("#plugin-port", Input).value == "22222"
        assert panel.configuration["saved"]["revision"] == 4
        with pytest.raises(AdminClientError, match="changed"):
            await panel._action("save-configuration")
        await panel._action("reload-configuration")
        assert panel.query_one("#plugin-port", Input).value == "33333"
        assert panel.configuration["saved"]["revision"] == 5


@pytest.mark.asyncio
async def test_reset_confirmation_cannot_be_rebased_to_a_new_pairing_generation():
    client = OwnerClient()
    app = HaloTui(client)
    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        panel = app.query_one(HomeKitPanel)
        panel.query_one("#homekit-reset-confirm", Input).value = "RESET"
        client.pairing["cameras"][0]["pairing_revision"] = "b" * 64
        await panel.refresh_owner()
        assert panel.query_one("#homekit-reset-confirm", Input).value == ""
        with pytest.raises(ValueError, match="Type RESET"):
            await panel._action("homekit-reset")
        assert not any(call[0] == "reset" for call in client.calls)


@pytest.mark.asyncio
async def test_pending_reset_resumes_exact_durable_operation_after_explicit_confirmation():
    client = OwnerClient()
    operation = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    client.pairing["boot_id"] = None
    client.pairing["cameras"][0].update(
        pending_reset=True, reset_operation_id=operation, published=False, paired_known=False
    )
    app = HaloTui(client)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        panel = app.query_one(HomeKitPanel)
        assert str(panel.query_one("#homekit-reset", Button).label) == "Resume reset"
        panel.query_one("#homekit-reset-confirm", Input).value = "RESET"
        await panel._action("homekit-reset")
        assert next(call for call in client.calls if call[0] == "reset")[-1] == operation
