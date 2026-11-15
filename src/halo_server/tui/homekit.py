"""Owner HomeKit/settings view. All effects go through the local admin client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from halo_server.configuration import ServerMode
from halo_server.tui.client import AdminClient, AdminClientError
from halo_server.tui.support import write_support_report

PAIRING_REVEAL_SECONDS = 60.0


class HomeKitPanel(Vertical):
    """Show saved/applied truth and camera-scoped setup secrets for a finite time."""

    DEFAULT_CSS = """
    HomeKitPanel { height: auto; }
    HomeKitPanel Horizontal { height: auto; margin-bottom: 1; }
    HomeKitPanel Button { margin-right: 1; min-width: 12; }
    HomeKitPanel Input, HomeKitPanel Select { width: 1fr; margin-right: 1; }
    #homekit-summary, #mode-summary, #owner-diagnostics { height: auto; }
    """

    def __init__(self, client: AdminClient):
        super().__init__(id="homekit-panel")
        self.client = client
        self.view: dict[str, Any] | None = None
        self.recording: dict[str, Any] | None = None
        self.pairing: dict[str, Any] | None = None
        self.controls: dict[str, Any] | None = None
        self.configuration: dict[str, Any] | None = None
        self._busy = False
        self._status_identity = None
        self._camera_id = None
        self._secret_identity = None
        self._secret_timer = None
        self._generation = 0
        self._revealing = False

    def compose(self) -> ComposeResult:
        yield Label("Operating mode and settings", classes="section-title")
        yield Static("Saved/applied settings: unavailable", id="mode-summary")
        with Horizontal():
            yield Select(
                [(mode.value, mode.value) for mode in ServerMode],
                allow_blank=False,
                id="server-mode",
            )
            yield Button("Save settings", id="save-configuration")
            yield Button("Reload settings", id="reload-configuration")
        with Horizontal():
            yield Input(placeholder="Plugin loopback host", id="plugin-host")
            yield Input(placeholder="Plugin port", id="plugin-port", type="integer")
        with Horizontal():
            yield Input(placeholder="RTSP loopback host", id="rtsp-host")
            yield Input(placeholder="RTSP port", id="rtsp-port", type="integer")
        yield Label("Direct HomeKit cameras", classes="section-title")
        yield Static("Native HomeKit: unavailable", id="homekit-summary")
        yield DataTable(id="homekit-cameras", cursor_type="row")
        with Horizontal():
            yield Button("Select", id="homekit-select")
            yield Button("Deselect", id="homekit-deselect")
        yield Static("Recording: select a camera", id="homekit-recording-summary")
        yield Static(
            "Enabling HKSV support may keep a continuous Ring connection for pre-event footage. "
            "For battery-powered cameras this can drain the battery more quickly, including with "
            "wired or solar assistance. Choose recording and audio separately in Apple Home."
        )
        with Horizontal():
            yield Button("Enable HKSV", id="homekit-warm-on")
            yield Button("Disable HKSV", id="homekit-warm-off")
        with Horizontal():
            yield Button("Reveal setup", id="homekit-reveal")
            yield Button("Hide setup", id="homekit-hide")
        with Horizontal():
            yield Input(
                placeholder="Setup PIN (hidden until Reveal)", id="homekit-pin", disabled=True
            )
            yield Input(placeholder="Setup URI", id="homekit-uri", disabled=True)
        with Horizontal():
            yield Input(
                placeholder="Type RESET to remove this camera's pairing and restart adapter",
                id="homekit-reset-confirm",
            )
            yield Button("Reset pairing", id="homekit-reset", variant="error")
        yield Label("Camera controls", classes="section-title")
        yield Static("Choose a camera below. Siren on uses the existing fixed 30-second limit.")
        yield Select([], prompt="Control camera", id="control-camera")
        with Horizontal():
            yield Button("Light on", id="light-on")
            yield Button("Light off", id="light-off")
            yield Button("Siren on", id="siren-on")
            yield Button("Siren off", id="siren-off")
        with Horizontal():
            yield Button("Doctor", id="owner-doctor")
            yield Input(placeholder="New support report file path", id="support-output")
            yield Button("Export support", id="owner-support")
        yield Static("", id="owner-diagnostics")

    def on_mount(self):
        self.query_one("#homekit-cameras", DataTable).add_columns(
            "Camera", "ID", "Selected", "Present", "HKSV allowed", "Video", "Buffer", "Paired"
        )
        self._buttons()

    def on_unmount(self):
        self.clear_secret()

    def observe_status(self, status):
        account = status.get("account") if isinstance(status.get("account"), dict) else {}
        native = status.get("homekit") if isinstance(status.get("homekit"), dict) else {}
        identity = (
            account.get("authenticated"),
            account.get("email"),
            native.get("boot_id"),
            native.get("restart_count"),
            native.get("state"),
            native.get("registry_revision"),
        )
        if identity != self._status_identity:
            self.clear_secret()
        self._status_identity = identity
        self.query_one("#homekit-summary", Static).update(
            Text(
                f"Native: {native.get('state', 'unavailable')} | {native.get('reason') or ''} | "
                f"warm ready: {native.get('recording_warm_ready', 0)} | "
                f"cleanup: {native.get('recording_cleanup_failures', 0)}. "
                "Recording permission is configured separately for each camera."
            )
        )

    async def refresh_owner(self):
        configuration = await self.client.configuration_get()
        if self.configuration is None or not self._draft_changed():
            self.configuration = configuration
            self._render_configuration()
        self.controls = await self.client.camera_controls()
        self.query_one("#control-camera", Select).set_options(
            [
                (Text(str(row.get("name") or row["camera_id"])), row["camera_id"])
                for row in self.controls["cameras"]
            ]
        )
        try:
            view = await self.client.homekit_cameras()
            recording = await self.client.homekit_recording_cameras()
            pairing = await self.client.homekit_pairing_list()
        except AdminClientError as error:
            self.clear_secret()
            self.view = self.recording = self.pairing = None
            self.query_one("#homekit-cameras", DataTable).clear()
            self._buttons()
            if error.code != "homekit_disabled":
                raise
            return
        if (
            (view["profile_id"], view["account_generation"])
            != (pairing["profile_id"], pairing["account_generation"])
            or (view["profile_id"], view["account_generation"])
            != (recording["profile_id"], recording["account_generation"])
            or view["revision"] != pairing["registry_revision"]
        ):
            self.clear_secret()
            raise AdminClientError("configuration_conflict", "Camera view changed; refresh")
        secret_identity = self._current_secret_identity(view, pairing)
        if secret_identity != self._current_secret_identity():
            self.clear_secret()
        self.view, self.recording, self.pairing = view, recording, pairing
        self._render_cameras()
        self._buttons()

    def unavailable(self):
        self.clear_secret()
        self.view = self.recording = self.pairing = self.controls = self.configuration = None
        self.query_one("#homekit-cameras", DataTable).clear()
        self.query_one("#control-camera", Select).set_options([])
        self.query_one("#mode-summary", Static).update(Text("Saved/applied settings: unavailable"))
        self._buttons()

    def _current_secret_identity(self, view=None, pairing=None):
        view, pairing = view or self.view, pairing or self.pairing
        if view is None or pairing is None or self._camera_id is None:
            return None
        row = next((r for r in pairing["cameras"] if r["camera_id"] == self._camera_id), None)
        selected = next((r for r in view["cameras"] if r["camera_id"] == self._camera_id), None)
        if row is None or selected is None or selected.get("selected") is not True:
            return None
        return (
            pairing["profile_id"],
            pairing["account_generation"],
            pairing["boot_id"],
            pairing["registry_revision"],
            row["camera_id"],
            row["accessory_id"],
            row["pairing_revision"],
            row.get("device_generation"),
            row.get("published"),
            row.get("paired_known"),
            selected.get("available"),
            pairing.get("available"),
        )

    def _render_configuration(self):
        value = self.configuration
        self.query_one("#mode-summary", Static).update(
            Text(
                f"Saved: {value['saved']['mode']} (revision {value['saved']['revision']}) | "
                f"Applied: {value['applied']['mode']} | "
                f"Restart required: {'yes' if value['restart_required'] else 'no'}. "
                "Saving does not restart the daemon."
            )
        )
        self.query_one("#server-mode", Select).value = value["saved"]["mode"]
        for name in ("plugin_host", "plugin_port", "rtsp_host", "rtsp_port"):
            self.query_one("#" + name.replace("_", "-"), Input).value = str(value["saved"][name])

    def _render_cameras(self):
        table = self.query_one("#homekit-cameras", DataTable)
        previous = self._camera_id
        table.clear()
        warm = {r["camera_id"]: r for r in self.recording["cameras"]}
        pairings = {r["camera_id"]: r for r in self.pairing["cameras"]}
        for row in self.view["cameras"]:
            state, pair = warm.get(row["camera_id"], {}), pairings.get(row["camera_id"], {})
            table.add_row(
                Text(str(row.get("name") or row["camera_id"])),
                Text(row["camera_id"]),
                _flag(row.get("selected")),
                _flag(row.get("available")),
                _flag(state.get("owner_warm")),
                _flag(state.get("recording_supported")),
                Text(_recording_state(state.get("warm_state"))),
                _flag(pair.get("paired")) if pair.get("paired_known") is True else "Unknown",
                key=row["camera_id"],
            )
        ids = [r["camera_id"] for r in self.view["cameras"]]
        if previous in ids:
            table.move_cursor(row=ids.index(previous))
        elif ids:
            self._camera_id = ids[0]
        else:
            self._camera_id = None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        if event.data_table.id != "homekit-cameras":
            return
        identity = str(event.row_key.value)
        if identity != self._camera_id:
            self.clear_secret()
            self._camera_id = identity
        self._buttons()

    async def on_button_pressed(self, event: Button.Pressed):
        action = event.button.id
        if action not in {
            "save-configuration",
            "reload-configuration",
            "homekit-select",
            "homekit-deselect",
            "homekit-warm-on",
            "homekit-warm-off",
            "homekit-reveal",
            "homekit-hide",
            "homekit-reset",
            "light-on",
            "light-off",
            "siren-on",
            "siren-off",
            "owner-doctor",
            "owner-support",
        }:
            return
        event.stop()
        if action == "homekit-hide":
            self.clear_secret()
            return
        if self._busy or getattr(self.app, "_busy", False):
            return
        self._busy = True
        self._buttons()
        try:
            await self._action(action)
        except (AdminClientError, OSError, ValueError) as error:
            self.clear_secret()
            self.app._message(str(error), error=True)
        finally:
            self._busy = False
            self._buttons()

    async def _action(self, action):
        if action == "reload-configuration":
            self.configuration = await self.client.configuration_get()
            self._render_configuration()
            return
        if action == "save-configuration":
            if self.configuration is None:
                raise AdminClientError("not_ready", "Refresh settings before saving")
            changes = {"mode": self.query_one("#server-mode", Select).value}
            for name in ("plugin_host", "plugin_port", "rtsp_host", "rtsp_port"):
                raw = self.query_one("#" + name.replace("_", "-"), Input).value
                changes[name] = int(raw) if name.endswith("port") else raw
            self.configuration = await self.client.configuration_set(
                self.configuration["saved"]["revision"], changes
            )
            self._render_configuration()
            self.app._message("Settings saved. Restart the daemon to apply changed settings.")
            return
        if action == "owner-doctor":
            report = await self.client.doctor()
            self.query_one("#owner-diagnostics", Static).update(Text(json.dumps(report, indent=2)))
            return
        if action == "owner-support":
            output = self.query_one("#support-output", Input).value.strip()
            if not output:
                raise ValueError("Choose a new support report file path")
            write_support_report(Path(output).expanduser(), await self.client.support_report())
            self.app._message("Sanitized support report saved in a private new file")
            return
        if action in {"light-on", "light-off", "siren-on", "siren-off"}:
            camera_id = self.query_one("#control-camera", Select).value
            row = _row(self.controls, camera_id)
            result = await self.client.camera_control(
                self.controls["profile_id"],
                self.controls["account_generation"],
                row["camera_id"],
                row["device_generation"],
                str(uuid4()),
                action.split("-")[0],
                action.endswith("-on"),
            )
            self.app._message(
                f"Control result: {result['status']} ({result.get('code', '')}). "
                "Unknown results are not retried.",
                error=result["status"] != "accepted",
            )
            return
        row = _row(self.view, self._camera_id)
        if action in {"homekit-select", "homekit-deselect"}:
            self.clear_secret()
            ids = [r["camera_id"] for r in self.view["cameras"] if r.get("selected") is True]
            ids = (
                list(dict.fromkeys(ids + [row["camera_id"]]))
                if action == "homekit-select"
                else [i for i in ids if i != row["camera_id"]]
            )
            await self.client.homekit_select(
                self.view["profile_id"], self.view["account_generation"], ids, self.view["revision"]
            )
        elif action in {"homekit-warm-on", "homekit-warm-off"}:
            warm = _row(self.recording, row["camera_id"])
            if action == "homekit-warm-on" and (
                row.get("selected") is not True
                or self.recording.get("available") is not True
                or warm.get("recording_supported") is not True
            ):
                raise ValueError("Select an available video-capable camera before enabling HKSV")
            await self.client.homekit_recording_warm(
                self.recording["profile_id"],
                self.recording["account_generation"],
                row["camera_id"],
                self.recording["revision"],
                action == "homekit-warm-on",
            )
        elif action == "homekit-reveal":
            await self.reveal()
            return
        elif action == "homekit-reset":
            confirmation = self.query_one("#homekit-reset-confirm", Input).value
            self.clear_secret()
            if confirmation != "RESET":
                raise ValueError(
                    "Type RESET to confirm this camera's pairing reset and adapter restart"
                )
            self.query_one("#homekit-reset-confirm", Input).value = ""
            pairing = _row(self.pairing, self._camera_id)
            operation = pairing.get("reset_operation_id") or str(uuid4())
            result = await self.client.homekit_pairing_reset(self.pairing, pairing, operation)
            self.app._message(
                f"Pairing reset: {result['status']} ({result.get('code', '')}). "
                "Re-pair a completed reset in Apple Home."
            )
        await self.refresh_owner()

    async def reveal(self):
        self.clear_secret()
        identity, generation = self._current_secret_identity(), self._generation
        if identity is None:
            raise AdminClientError(
                "not_ready", "Select a current HomeKit camera before revealing setup"
            )
        self._revealing = True
        try:
            result = await self.client.homekit_pairing_reveal(
                self.pairing, _row(self.pairing, self._camera_id)
            )
            await self.refresh_owner()
            if generation != self._generation or identity != self._current_secret_identity():
                raise AdminClientError(
                    "account_changed", "Camera authority changed; setup remains hidden"
                )
            self.query_one("#homekit-pin", Input).value = result["pin"]
            self.query_one("#homekit-uri", Input).value = result["setup_uri"]
            self._secret_identity = identity
            self._secret_timer = self.set_timer(PAIRING_REVEAL_SECONDS, self.clear_secret)
        finally:
            self._revealing = False

    def clear_secret(self):
        self._generation += 1
        self._secret_identity = None
        if self._secret_timer is not None:
            self._secret_timer.stop()
            self._secret_timer = None
        for field in self.query("#homekit-pin, #homekit-uri, #homekit-reset-confirm").results(
            Input
        ):
            field.value = ""

    def _draft_changed(self):
        if self.configuration is None:
            return False
        saved = self.configuration["saved"]
        return self.query_one("#server-mode", Select).value != saved["mode"] or any(
            self.query_one("#" + name.replace("_", "-"), Input).value != str(saved[name])
            for name in ("plugin_host", "plugin_port", "rtsp_host", "rtsp_port")
        )

    def _buttons(self):
        if not self.is_mounted:
            return
        busy = self._busy or getattr(self.app, "_busy", False)
        for button in self.query(Button):
            button.disabled = busy
        self.query_one("#save-configuration", Button).disabled = busy or self.configuration is None
        for suffix in ("select", "deselect", "warm-on", "warm-off", "reveal", "reset"):
            self.query_one("#homekit-" + suffix, Button).disabled = (
                busy or self.view is None or self._camera_id is None
            )
        row = next(
            (r for r in (self.view or {}).get("cameras", []) if r["camera_id"] == self._camera_id),
            {},
        )
        warm = next(
            (
                r
                for r in (self.recording or {}).get("cameras", [])
                if r["camera_id"] == self._camera_id
            ),
            {},
        )
        paired = next(
            (
                r
                for r in (self.pairing or {}).get("cameras", [])
                if r["camera_id"] == self._camera_id
            ),
            {},
        )
        self.query_one("#homekit-select", Button).disabled |= row.get("selected") is True
        self.query_one("#homekit-deselect", Button).disabled |= row.get("selected") is not True
        self.query_one("#homekit-warm-on", Button).disabled |= (
            row.get("selected") is not True
            or (self.recording or {}).get("available") is not True
            or warm.get("recording_supported") is not True
            or warm.get("owner_warm") is True
        )
        self.query_one("#homekit-warm-off", Button).disabled |= warm.get("owner_warm") is not True
        budget = (self.recording or {}).get("warm_budget") or {}
        reserved = budget.get("reserved")
        usage = (
            f"{reserved} sources reserved; no configured camera cap"
            if type(reserved) is int
            and reserved >= 0
            and "capacity" in budget
            and budget["capacity"] is None
            else "source usage unavailable"
        )
        self.query_one("#homekit-recording-summary", Static).update(
            Text(
                f"HKSV allowed: {_flag(warm.get('owner_warm'))} | "
                f"Pre-event buffer: {_recording_state(warm.get('warm_state'))} | {usage}. "
                "Last owner refresh; buffer readiness does not confirm a saved Apple Home event."
            )
        )
        self.query_one("#homekit-reveal", Button).disabled |= (
            row.get("selected") is not True
            or paired.get("published") is not True
            or paired.get("pairing_revision") is None
        )
        self.query_one("#homekit-reset", Button).disabled |= row.get("selected") is not True
        self.query_one("#homekit-reset", Button).label = (
            "Resume reset" if paired.get("pending_reset") else "Reset pairing"
        )
        for control in ("light", "siren"):
            value = self.query_one("#control-camera", Select).value
            row = next(
                (r for r in (self.controls or {}).get("cameras", []) if r["camera_id"] == value), {}
            )
            for suffix in ("on", "off"):
                self.query_one(f"#{control}-{suffix}", Button).disabled = (
                    busy or row.get("has_" + control) is not True
                )

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == "control-camera":
            self._buttons()


def _row(page, camera_id):
    if page is not None:
        for row in page["cameras"]:
            if row.get("camera_id") == camera_id:
                return row
    raise AdminClientError("camera_unavailable", "Select a camera from the current view")


def _flag(value):
    return "Yes" if value is True else "No" if value is False else "Unknown"


def _recording_state(value):
    return {
        "disabled": "Not buffering",
        "warming": "Preparing",
        "ready": "Ready",
        "capacity": "Waiting for capacity",
        "cleanup_pending": "Waiting for cleanup",
    }.get(value, "Unknown")
