"""Two fixed managed units, preserving independent owner startup intent.

The release operator owns the canonical maintenance lock around these calls.
Neither this controller nor the web companion releases a live process lease.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import UUID

from halo_server.api.admin import AdminProtocolError
from halo_server.media.manager import owned_thread
from halo_server.private_json import PrivateJsonError, PrivateJsonStore
from halo_server.release_activation import ActivationError, ReleaseReference
from halo_server.release_legacy import capture_owner, verify_stopped
from halo_server.release_service import (
    HEALTH_SECONDS,
    UNIT_NAME,
    WEB_UNIT_NAME,
    _prepare_unit,
    _systemctl,
    _unit_directory,
    _unit_metadata,
    local_release_healthy,
    render_user_unit,
    render_web_unit,
)
from halo_server.tui.client import AdminClientError

UNIT_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "UnitFileState",
    "FragmentPath",
    "DropInPaths",
    "NeedDaemonReload",
    "InvocationID",
    "MainPID",
)


async def unit_command(unit, *arguments):
    return await _systemctl(*arguments, unit=unit)


@dataclass(frozen=True)
class UnitState:
    exists: bool
    active: bool
    enabled: bool
    invocation: str = ""
    pid: int = 0


def validate_unit_file(config_directory, prefix, state_directory, unit):
    """Check exact owner-installed bytes without changing files or permissions."""
    if unit not in {UNIT_NAME, WEB_UNIT_NAME}:
        raise ActivationError("Unknown managed user unit", code="bad_parameters")
    render = render_user_unit if unit == UNIT_NAME else render_web_unit
    expected = render(prefix, state_directory=state_directory).encode()
    directory = _unit_directory(Path(config_directory) / "systemd" / "user", create=False)
    descriptor = -1
    try:
        descriptor = os.open(
            unit, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory
        )
        metadata = _unit_metadata(descriptor)
        if metadata.st_size != len(expected) or os.read(descriptor, len(expected) + 1) != expected:
            raise ActivationError("A different user unit is installed", code="unit_conflict")
    except OSError as error:
        raise ActivationError("Managed unit cannot be verified", code="unit_conflict") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


async def inspect_unit(unit, prefix, paths, config_directory, *, command=unit_command):
    if unit not in {UNIT_NAME, WEB_UNIT_NAME}:
        raise ActivationError("Unknown managed user unit", code="bad_parameters")
    raw = await command(unit, "show", "--property=" + ",".join(UNIT_PROPERTIES))
    try:
        if not isinstance(raw, bytes) or len(raw) > 4096:
            raise ValueError
        rows = {}
        for line in raw.decode("utf-8").splitlines():
            name, value = line.split("=", 1)
            if name in rows or name not in UNIT_PROPERTIES:
                raise ValueError
            rows[name] = value
        if set(rows) != set(UNIT_PROPERTIES):
            raise ValueError
        if rows["LoadState"] == "not-found":
            if rows["ActiveState"] != "inactive" or rows["FragmentPath"] or rows["DropInPaths"]:
                raise ValueError
            return UnitState(False, False, False)
        expected_path = Path(config_directory) / "systemd" / "user" / unit
        if (
            rows["LoadState"] != "loaded"
            or rows["FragmentPath"] != str(expected_path)
            or rows["DropInPaths"]
            or rows["NeedDaemonReload"] != "no"
            or rows["UnitFileState"] not in {"enabled", "disabled"}
        ):
            raise ActivationError("Managed unit is customized or unavailable", code="unit_conflict")
        if rows["ActiveState"] not in {"active", "inactive", "failed"}:
            raise ActivationError("Managed unit is changing state", code="busy")
        invocation = rows["InvocationID"]
        if invocation and re.fullmatch(r"[a-f0-9]{32}", invocation) is None:
            raise ValueError
        if not rows["MainPID"].isascii() or not rows["MainPID"].isdecimal():
            raise ValueError
        pid = int(rows["MainPID"])
        if rows["ActiveState"] == "active" and (pid <= 0 or not invocation):
            raise ValueError
    except (ValueError, UnicodeError, TypeError) as error:
        raise ActivationError("Managed unit state is unavailable", code="service_failed") from error
    await owned_thread(validate_unit_file, config_directory, prefix, paths.state_directory, unit)
    return UnitState(
        True, rows["ActiveState"] == "active", rows["UnitFileState"] == "enabled", invocation, pid
    )


@dataclass(frozen=True)
class WebStartup:
    version: int = 1
    revision: int = 0
    suppressed: bool = False
    active: bool = False
    enabled: bool = False

    def __post_init__(self):
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.revision) is not int
            or not 0 <= self.revision < 2**53
            or any(
                type(value) is not bool for value in (self.suppressed, self.active, self.enabled)
            )
        ):
            raise PrivateJsonError("Web startup intent is invalid")

    def as_dict(self):
        return asdict(self)


def _decode_startup(value):
    if set(value) != set(WebStartup().as_dict()):
        raise PrivateJsonError("Web startup intent is invalid")
    return WebStartup(**value)


def startup_store(state):
    return PrivateJsonStore(
        Path(state) / "web-startup.json",
        snapshot_type=WebStartup,
        decode=_decode_startup,
        default=WebStartup,
        max_bytes=4096,
        label="Web startup intent",
        noun="web startup intent",
        error_type=PrivateJsonError,
        conflict_type=PrivateJsonError,
    )


def supports_web(verified):
    """A capability from verified immutable file records, never target imports."""
    payload = getattr(verified, "payload", {})
    files = {row.get("path") for row in payload.get("files", ()) if isinstance(row, dict)}
    return {
        "python/site-packages/halo_server/web/__init__.py",
        "python/site-packages/halo_server/web/server.py",
    } <= files


def web_release_healthy(status, reference):
    if not isinstance(status, dict) or not isinstance(reference, ReleaseReference):
        return False
    identity, web = status.get("release"), status.get("web")
    try:
        boot = status["boot_id"]
        valid_boot = isinstance(boot, str) and str(UUID(boot)) == boot
    except (KeyError, ValueError, AttributeError):
        valid_boot = False
    return (
        valid_boot
        and status.get("service") == "running"
        and isinstance(identity, dict)
        and identity.get("release_id") == reference.release_id
        and identity.get("manifest_sha256") == reference.manifest_sha256
        and isinstance(web, dict)
        and web.get("ready") is True
        and status.get("accepting_operations") is True
    )


class ManagedServicesController:
    """Restore active separately from enabled, including an unsupported web rollback."""

    def __init__(
        self,
        client,
        web_client,
        prefix,
        paths,
        *,
        config_directory=None,
        command=unit_command,
        install_web=False,
    ):
        self.client, self.web_client = client, web_client
        self.prefix, self.paths, self.command = Path(prefix), paths, command
        self.config_directory = Path(
            config_directory or os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        )
        if not self.prefix.is_absolute() or not self.config_directory.is_absolute():
            raise ActivationError("Managed paths must be absolute", code="bad_parameters")
        self.install_web = install_web
        self.startup = startup_store(paths.state_directory)
        self.original = self.desired = None
        self.target_web = False
        self.first_install = False

    async def _inspect(self, unit):
        return await inspect_unit(
            unit, self.prefix, self.paths, self.config_directory, command=self.command
        )

    async def configure(self, verified, *, first_install=False):
        self.target_web = supports_web(verified)
        if self.original is None and self.install_web and not self.target_web:
            raise ActivationError("Target release has no web companion", code="unsupported")
        self.first_install = first_install
        if self.original is not None:
            return
        self.original = {unit: await self._inspect(unit) for unit in (UNIT_NAME, WEB_UNIT_NAME)}
        self.desired = dict(self.original)
        saved = await owned_thread(self.startup.load)
        if saved.suppressed:
            self.desired[WEB_UNIT_NAME] = UnitState(True, saved.active, saved.enabled)
        if first_install:
            self.desired[UNIT_NAME] = replace(self.desired[UNIT_NAME], active=True)

    async def before_select(self, verified):
        """Suppress unsupported startup before the selector can name old code."""
        self.target_web = supports_web(verified)
        if not self.target_web:
            web = await self._inspect(WEB_UNIT_NAME)
            desired = self.desired[WEB_UNIT_NAME]
            if web.exists:
                await owned_thread(
                    self.startup.update,
                    lambda row: replace(
                        row,
                        suppressed=True,
                        active=desired.active,
                        enabled=desired.enabled,
                    ),
                )
                if web.enabled:
                    await self.command(WEB_UNIT_NAME, "disable")

    async def prepare(self, prefix):
        if Path(prefix) != self.prefix:
            raise ActivationError("Managed prefix changed", code="bad_parameters")
        await owned_thread(
            _prepare_unit, self.config_directory, self.prefix, self.paths.state_directory
        )
        if self.install_web:
            await owned_thread(
                _prepare_unit,
                self.config_directory,
                self.prefix,
                self.paths.state_directory,
                unit=WEB_UNIT_NAME,
            )
        await self.command(UNIT_NAME, "daemon-reload")

    async def _capture_intent(self):
        # Uninstall has no target release, but still must retire both owners.
        if self.original is None:
            self.original = {unit: await self._inspect(unit) for unit in (UNIT_NAME, WEB_UNIT_NAME)}
            self.desired = dict(self.original)

    async def stop(self):
        await self._capture_intent()
        current = {unit: await self._inspect(unit) for unit in (UNIT_NAME, WEB_UNIT_NAME)}
        clients = {UNIT_NAME: self.client, WEB_UNIT_NAME: self.web_client}
        owners = {}
        try:
            # Capture both peers before the first externally visible mutation.
            for unit, client in clients.items():
                owners[unit] = await capture_owner(client)
            if current[WEB_UNIT_NAME].active:
                try:
                    async with asyncio.timeout(25):
                        drained = await self.web_client.request("drain")
                    if not isinstance(drained, dict) or drained.get("drained") is not True:
                        raise ActivationError("Web operations have not drained", code="busy")
                except AdminClientError as error:
                    if error.code != "daemon_unavailable":
                        raise ActivationError(
                            "Web drain is unverified", code="stop_unverified"
                        ) from error
            for unit in (WEB_UNIT_NAME, UNIT_NAME):
                if current[unit].exists:
                    await self.command(unit, "stop")
                await verify_stopped(clients[unit], owners[unit])
        finally:
            for owner in owners.values():
                if owner is not None:
                    owner.close()

    async def disable(self):
        for unit in (WEB_UNIT_NAME, UNIT_NAME):
            current = await self._inspect(unit)
            if current.exists and current.enabled:
                await self.command(unit, "disable")
        await owned_thread(
            self.startup.update,
            lambda row: replace(row, suppressed=False, active=False, enabled=False),
        )

    async def _poll(self, client, reference, predicate):
        deadline = asyncio.get_running_loop().time() + HEALTH_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with asyncio.timeout(min(2, deadline - asyncio.get_running_loop().time())):
                    observed = await client.status()
                if predicate(observed, reference):
                    return True
            except (AdminClientError, AdminProtocolError, OSError, TimeoutError, ValueError):
                pass
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
        return False

    async def start(self, reference):
        if not isinstance(reference, ReleaseReference) or self.desired is None:
            raise ActivationError("Managed start has no recorded intent", code="bad_parameters")
        web = await self._inspect(WEB_UNIT_NAME)
        desired = self.desired[WEB_UNIT_NAME]
        if self.target_web:
            saved = await owned_thread(self.startup.load)
            if saved.suppressed:
                if not web.exists and (desired.active or desired.enabled):
                    raise ActivationError(
                        "Companion unit must be restored explicitly", code="unit_missing"
                    )
                if web.exists and web.enabled != desired.enabled:
                    await self.command(WEB_UNIT_NAME, "enable" if desired.enabled else "disable")
                await owned_thread(self.startup.update, lambda row: replace(row, suppressed=False))
        if self.desired[UNIT_NAME].active:
            daemon = await self._inspect(UNIT_NAME)
            if not daemon.exists:
                raise ActivationError(
                    "Daemon unit must be installed explicitly", code="unit_missing"
                )
            await self.command(UNIT_NAME, "start")
            if not await self._poll(self.client, reference, local_release_healthy):
                raise ActivationError("Daemon local health failed", code="local_health_failed")
        if self.target_web and desired.active:
            if not web.exists:
                raise ActivationError("Companion unit is missing", code="unit_missing")
            await self.command(WEB_UNIT_NAME, "start")

    async def healthy(self, reference):
        for unit, client, wanted, predicate in (
            (UNIT_NAME, self.client, self.desired[UNIT_NAME].active, local_release_healthy),
            (
                WEB_UNIT_NAME,
                self.web_client,
                self.target_web and self.desired[WEB_UNIT_NAME].active,
                web_release_healthy,
            ),
        ):
            if wanted:
                if not await self._poll(client, reference, predicate):
                    return False
            else:
                if (await self._inspect(unit)).active:
                    return False
                try:
                    await verify_stopped(client, None)
                except ActivationError:
                    return False
        return True
