"""Textual administration client for the Halo Server daemon."""

from __future__ import annotations

import math
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from halo_server.tui.client import AdminClient, AdminClientError
from halo_server.tui.homekit import HomeKitPanel

TOKEN_REVEAL_SECONDS = 60.0


class HaloTui(App):
    """Manage Halo Server exclusively through its local admin protocol."""

    TITLE = "Halo Server"
    SUB_TITLE = "Administration"
    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        padding: 1 2;
    }

    .section-title {
        text-style: bold;
        margin-top: 1;
    }

    #summary {
        height: 3;
    }

    #account-status, #event-status {
        width: 1fr;
        padding: 0 1;
    }

    #login-fields {
        height: auto;
    }

    #login-fields Input {
        width: 1fr;
        margin-right: 1;
    }

    #account-actions {
        height: 3;
        margin-top: 1;
    }

    #account-actions Button {
        margin-right: 1;
    }

    #integration-fields {
        height: auto;
    }

    #plugin-label, #plugin-token {
        width: 1fr;
        margin-right: 1;
    }

    #integration-actions {
        height: 3;
        margin-top: 1;
    }

    #integration-actions Button {
        margin-right: 1;
    }

    DataTable {
        height: 12;
        margin-bottom: 1;
    }

    #message {
        min-height: 1;
    }
    """
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, client: AdminClient) -> None:
        super().__init__()
        self._client = client
        self._busy = False
        self._authenticated = False
        self._available = False
        self._account_identity: tuple[bool, str | None] | None = None
        self._plugin_client_ids: list[str] = []
        self._revealed_client_id: str | None = None
        self._token_timer = None

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="body"):
            with Horizontal(id="summary"):
                yield Static("Account: checking", id="account-status")
                yield Static("Events: checking", id="event-status")
            yield Label("Ring Account", classes="section-title")
            with Horizontal(id="login-fields"):
                yield Input(placeholder="Email", id="email")
                yield Input(placeholder="Password", password=True, id="password")
                yield Input(placeholder="Two-factor code", id="otp")
            with Horizontal(id="account-actions"):
                yield Button("Sign In", id="login", variant="primary")
                yield Button("Sign Out", id="logout")
                yield Button("Refresh", id="refresh")
            yield Static("", id="message")
            yield Label("Cameras", classes="section-title")
            yield DataTable(id="cameras")
            yield HomeKitPanel(self._client)
            yield Label("Recent Ring Events", classes="section-title")
            yield DataTable(id="events")
            yield Label("Compatibility integrations", classes="section-title")
            with Horizontal(id="integration-fields"):
                yield Input(placeholder="Integration name", id="plugin-label")
                yield Input(
                    placeholder="New token",
                    id="plugin-token",
                )
            with Horizontal(id="integration-actions"):
                yield Button("Pair", id="pair-plugin", variant="primary")
                yield Button("Revoke", id="revoke-plugin")
                yield Button("Clear Token", id="clear-plugin-token")
            yield DataTable(id="integrations")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#otp", Input).display = False
        camera_table = self.query_one("#cameras", DataTable)
        camera_table.add_columns("Camera", "Model", "Online", "Battery", "Motion")
        event_table = self.query_one("#events", DataTable)
        event_table.add_columns("Camera", "Event", "Update", "Ring time")
        integration_table = self.query_one("#integrations", DataTable)
        integration_table.add_columns("Name", "Client ID", "Created")
        await self._refresh()
        self.set_interval(3, self._refresh)

    async def action_refresh(self) -> None:
        await self._refresh()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            await self._refresh()
        elif event.button.id == "login":
            await self._login()
        elif event.button.id == "logout":
            await self._logout()
        elif event.button.id == "pair-plugin":
            await self._pair_plugin()
        elif event.button.id == "revoke-plugin":
            await self._revoke_plugin()
        elif event.button.id == "clear-plugin-token":
            self._clear_token()

    async def _refresh(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        try:
            status = await self._client.status()
            self._apply_status(status)
            panel = self.query_one(HomeKitPanel)
            self._message("")
            if "configuration" in status and not panel._busy:
                try:
                    await panel.refresh_owner()
                except AdminClientError as error:
                    if error.code in {"daemon_unavailable", "request_failed"}:
                        raise
                    panel.unavailable()
                    self._message(str(error), error=True)
        except AdminClientError as exc:
            self._mark_unavailable()
            self._message(str(exc), error=True)
        finally:
            self._set_busy(False)

    async def _login(self) -> None:
        if self._busy:
            return
        email = self.query_one("#email", Input).value.strip()
        password_input = self.query_one("#password", Input)
        password = password_input.value
        otp_input = self.query_one("#otp", Input)
        otp = otp_input.value.strip() or None
        if not email or not password:
            self._message("Email and password are required", error=True)
            return

        self._set_busy(True)
        self._clear_token()
        self.query_one(HomeKitPanel).clear_secret()
        try:
            status = await self._client.login(email, password, otp)
        except AdminClientError as exc:
            if exc.code == "two_factor_required":
                otp_input.display = True
                otp_input.focus()
                self._message("Ring requires a two-factor code")
            else:
                self._mark_unavailable()
                password_input.value = ""
                otp_input.value = ""
                self._message(str(exc), error=True)
        else:
            password_input.value = ""
            otp_input.value = ""
            otp_input.display = False
            self._apply_status(status)
            self._message("Signed in")
        finally:
            self._set_busy(False)

    async def _logout(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._clear_token()
        self.query_one(HomeKitPanel).clear_secret()
        self.query_one("#password", Input).value = ""
        self.query_one("#otp", Input).value = ""
        self.query_one("#otp", Input).display = False
        try:
            status = await self._client.logout()
            self._apply_status(status)
            self._message("Signed out")
        except AdminClientError as exc:
            self._mark_unavailable()
            self._message(str(exc), error=True)
        finally:
            self._set_busy(False)

    async def _pair_plugin(self) -> None:
        if self._busy:
            return
        label = self.query_one("#plugin-label", Input).value.strip()
        if not label:
            self._message("Integration name is required", error=True)
            return
        self._set_busy(True)
        self._clear_token()
        try:
            pairing = await self._client.pair_plugin(label)
            token = pairing.get("token")
            if not isinstance(token, str):
                raise AdminClientError(
                    "bad_response",
                    "Daemon returned an invalid pairing token",
                )
            self._apply_status(await self._client.status())
            client = pairing.get("client")
            self._revealed_client_id = client.get("id") if isinstance(client, dict) else None
            self.query_one("#plugin-token", Input).value = token
            self._token_timer = self.set_timer(TOKEN_REVEAL_SECONDS, self._clear_token)
            self.query_one("#plugin-label", Input).value = ""
            self._message("Plugin credential created")
        except AdminClientError as exc:
            self._mark_unavailable()
            self._message(str(exc), error=True)
        finally:
            self._set_busy(False)

    async def _revoke_plugin(self) -> None:
        if self._busy:
            return
        table = self.query_one("#integrations", DataTable)
        row = table.cursor_row
        if row < 0 or row >= len(self._plugin_client_ids):
            self._message("Select an integration to revoke", error=True)
            return
        client_id = self._plugin_client_ids[row]
        self._set_busy(True)
        self._clear_token()
        try:
            await self._client.revoke_plugin(client_id)
            self._apply_status(await self._client.status())
            self._message("Plugin credential revoked")
        except AdminClientError as exc:
            self._mark_unavailable()
            self._message(str(exc), error=True)
        finally:
            self._set_busy(False)

    def _apply_status(self, status: dict[str, Any]) -> None:
        self.query_one(HomeKitPanel).observe_status(status)
        account = _mapping(status.get("account"))
        authenticated = account.get("authenticated") is True
        email = account.get("email")
        identity = authenticated, email if isinstance(email, str) else None
        if self._account_identity is not None and identity != self._account_identity:
            self._clear_token()
        self._account_identity = identity
        self._available = True
        self._authenticated = authenticated
        if authenticated:
            account_text = f"Account: {email or 'signed in'}"
        elif account.get("restoring"):
            account_text = "Account: restoring"
        else:
            account_text = "Account: signed out"
        self.query_one("#account-status", Static).update(Text(account_text))

        events = _mapping(status.get("events"))
        connection = str(events.get("connection") or "offline")
        self.query_one("#event-status", Static).update(Text(f"Events: {connection}"))
        self.query_one("#login", Button).disabled = authenticated
        self.query_one("#logout", Button).disabled = not authenticated

        camera_table = self.query_one("#cameras", DataTable)
        camera_table.clear()
        for camera in _records(status.get("cameras")) if authenticated else []:
            camera_table.add_row(
                Text(str(camera.get("name") or camera.get("id") or "Unknown")),
                Text(str(camera.get("model") or camera.get("kind") or "")),
                _status_bool(camera.get("online")),
                _battery(camera.get("battery_percent")),
                _status_bool(camera.get("motion_detection")),
            )

        event_table = self.query_one("#events", DataTable)
        event_table.clear()
        for ring_event in reversed(_records(events.get("recent"))) if authenticated else []:
            event_table.add_row(
                Text(str(ring_event.get("camera_name") or ring_event.get("camera_id") or "")),
                Text(str(ring_event.get("type") or "")),
                "Yes" if ring_event.get("is_update") else "No",
                _timestamp(ring_event.get("occurred_at")),
            )

        integration_table = self.query_one("#integrations", DataTable)
        integration_table.clear()
        integrations = _mapping(status.get("integrations"))
        clients = _records(integrations.get("clients"))
        self._plugin_client_ids = []
        for client in clients:
            client_id = str(client.get("id") or "")
            self._plugin_client_ids.append(client_id)
            integration_table.add_row(
                Text(str(client.get("label") or "")),
                Text(client_id),
                _timestamp(client.get("created_at")),
            )
        self.query_one("#revoke-plugin", Button).disabled = not self._plugin_client_ids
        if self._revealed_client_id not in self._plugin_client_ids:
            self._clear_token()

    def _clear_token(self) -> None:
        if self._token_timer is not None:
            self._token_timer.stop()
            self._token_timer = None
        self.query_one("#plugin-token", Input).value = ""
        self._revealed_client_id = None

    def _mark_unavailable(self) -> None:
        self._available = False
        self._authenticated = False
        self._account_identity = None
        self._clear_token()
        self.query_one(HomeKitPanel).unavailable()
        self._plugin_client_ids = []
        for table_id in ("cameras", "events", "integrations"):
            self.query_one(f"#{table_id}", DataTable).clear()
        self.query_one("#account-status", Static).update("Account: unavailable")
        self.query_one("#event-status", Static).update("Events: unavailable")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one("#login", Button).disabled = busy or self._authenticated
        self.query_one("#logout", Button).disabled = busy or not self._authenticated
        self.query_one("#refresh", Button).disabled = busy
        self.query_one("#pair-plugin", Button).disabled = busy or not self._available
        self.query_one("#revoke-plugin", Button).disabled = busy or not self._plugin_client_ids
        self.query_one("#clear-plugin-token", Button).disabled = busy
        self.query_one(HomeKitPanel)._buttons()

    def _message(self, text: str, *, error: bool = False) -> None:
        message = self.query_one("#message", Static)
        message.update(Text(text))
        message.set_class(error, "error")


def _status_bool(value: Any) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "-"


def _battery(value: Any) -> str:
    return f"{value}%" if isinstance(value, int) and not isinstance(value, bool) else "-"


def _timestamp(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    try:
        if not math.isfinite(value):
            return ""
    except OverflowError:
        return ""
    from datetime import datetime

    try:
        return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    return (
        [record for record in value if isinstance(record, dict)] if isinstance(value, list) else []
    )
