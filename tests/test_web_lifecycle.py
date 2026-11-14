"""Two-service release and restart behavior without modifying real user units."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from halo_server.configuration import ConfigurationStore, ServerConfiguration, ServerMode
from halo_server.paths import ServerPaths
from halo_server.release_activation import (
    ActivationError,
    InstallLock,
    ReleaseActivator,
    ReleaseReference,
    SelectionStore,
)
from halo_server.release_service import UNIT_NAME, WEB_UNIT_NAME, _prepare_unit, render_web_unit
from halo_server.release_services import ManagedServicesController, inspect_unit, startup_store
from halo_server.tui.client import AdminClientError
from halo_server.web_restart import ManagedDaemonRestart, RestartRecord

A = ReleaseReference("a" * 64, "1" * 64)
B = ReleaseReference("b" * 64, "2" * 64)


def release(reference=A, *, web=True):
    return SimpleNamespace(
        reader_version=14,
        payload={
            "files": [
                {"path": "python/site-packages/halo_server/web/" + name}
                for name in ("__init__.py", "server.py")
            ]
            if web
            else []
        },
    )


class FakeUnits:
    def __init__(self, root):
        self.prefix = root / "prefix"
        self.config = root / "config"
        self.paths = ServerPaths(root / "state", root / "runtime")
        self.rows = {
            unit: {
                "active": True,
                "enabled": True,
                "invocation": str(index) * 32,
                "pid": 400 + index,
            }
            for index, unit in enumerate((UNIT_NAME, WEB_UNIT_NAME), 1)
        }
        self.calls = []
        self.reference = A
        self.block_restart = self.release_restart = None
        for unit in self.rows:
            _prepare_unit(self.config, self.prefix, self.paths.state_directory, unit=unit)

    async def command(self, unit, *arguments):
        row = self.rows[unit]
        if arguments[0] == "show":
            data = {
                "LoadState": "loaded",
                "ActiveState": "active" if row["active"] else "inactive",
                "UnitFileState": "enabled" if row["enabled"] else "disabled",
                "FragmentPath": str(self.config / "systemd/user" / unit),
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "InvocationID": row["invocation"],
                "MainPID": str(row["pid"] if row["active"] else 0),
            }
            return "".join(f"{key}={value}\n" for key, value in data.items()).encode()
        self.calls.append((unit, arguments[0]))
        if arguments[0] in {"start", "restart"}:
            if arguments[0] == "restart" and self.block_restart is not None:
                self.block_restart.set()
                await self.release_restart.wait()
            row["active"] = True
            row["pid"] += 1
            row["invocation"] = uuid4().hex
        elif arguments[0] == "stop":
            row["active"] = False
        elif arguments[0] in {"enable", "disable"}:
            row["enabled"] = arguments[0] == "enable"
        return b""

    def client(self, unit):
        parent = self

        class Client:
            async def request(self, method):
                assert method == "drain"
                parent.calls.append((unit, "drain"))
                return {"drained": True, "pending_operations": 0}

            async def status(self):
                if not parent.rows[unit]["active"]:
                    raise AdminClientError("daemon_unavailable", "closed")
                if unit == WEB_UNIT_NAME:
                    return {
                        "service": "running",
                        "boot_id": str(uuid4()),
                        "release": parent.reference.as_dict(),
                        "web": {"ready": True},
                        "accepting_operations": True,
                    }
                return {
                    "release": parent.reference.as_dict() | {"minimum_reader": 14},
                    "service": "running",
                    "shutdown_complete": False,
                    "configuration": {"mode": "ring-only"},
                }

        return Client()

    def controller(self):
        return ManagedServicesController(
            self.client(UNIT_NAME),
            self.client(WEB_UNIT_NAME),
            self.prefix,
            self.paths,
            config_directory=self.config,
            command=self.command,
        )

    def restart(self):
        context = SimpleNamespace(
            root=self.prefix / "releases" / A.release_id,
            release_id=A.release_id,
            manifest_sha256=A.manifest_sha256,
            interpreter=Path("/verified/python"),
        )
        return ManagedDaemonRestart(
            self.prefix,
            self.paths,
            self.client(UNIT_NAME),
            command=self.command,
            config_directory=self.config,
            context=context,
            verify_launcher=lambda *a, **k: None,
        )


def test_web_unit_is_independent_and_uses_same_verified_launcher(tmp_path):
    body = render_web_unit(tmp_path / "prefix with spaces", state_directory=tmp_path / "state")
    assert '" web\n' in body and "KillMode=control-group" in body
    assert all(key not in body for key in ("BindsTo", "PartOf", "Requires=", "Wants=", "ExecStop="))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "daemon_active,daemon_enabled,web_active,web_enabled",
    [
        (True, False, False, True),
        (False, True, True, False),
        (True, True, True, True),
    ],
)
async def test_preserves_active_and_enabled_independently(
    tmp_path, daemon_active, daemon_enabled, web_active, web_enabled
):
    units = FakeUnits(tmp_path)
    units.rows[UNIT_NAME].update(active=daemon_active, enabled=daemon_enabled)
    units.rows[WEB_UNIT_NAME].update(active=web_active, enabled=web_enabled)
    controller = units.controller()
    await controller.configure(release())
    await controller.stop()
    assert not any(row["active"] for row in units.rows.values())
    await controller.before_select(release())
    await controller.start(A)
    assert await controller.healthy(A)
    assert units.rows[UNIT_NAME]["active"] == daemon_active
    assert units.rows[UNIT_NAME]["enabled"] == daemon_enabled
    assert units.rows[WEB_UNIT_NAME]["active"] == web_active
    assert units.rows[WEB_UNIT_NAME]["enabled"] == web_enabled
    assert not any(action in {"enable", "disable"} for _, action in units.calls)
    if daemon_active and web_active:
        assert units.calls.index((UNIT_NAME, "start")) < units.calls.index((WEB_UNIT_NAME, "start"))


@pytest.mark.asyncio
async def test_preweb_suppression_precedes_selection_and_survives_operator_restart(tmp_path):
    units = FakeUnits(tmp_path)
    controller = units.controller()
    await controller.configure(release(B, web=False))
    await controller.stop()
    await controller.before_select(release(B, web=False))
    saved = startup_store(units.paths.state_directory).load()
    assert saved.suppressed and saved.enabled and saved.active
    assert not units.rows[WEB_UNIT_NAME]["enabled"]
    units.reference = B
    await controller.start(B)
    assert await controller.healthy(B)
    assert not units.rows[WEB_UNIT_NAME]["active"]
    # New operator process learns original intent from private durable state.
    newer = units.controller()
    await newer.configure(release(A))
    await newer.stop()
    await newer.before_select(release(A))
    units.reference = A
    await newer.start(A)
    assert await newer.healthy(A)
    assert units.rows[WEB_UNIT_NAME]["active"] and units.rows[WEB_UNIT_NAME]["enabled"]
    assert not startup_store(units.paths.state_directory).load().suppressed


@pytest.mark.asyncio
async def test_custom_companion_refused_before_daemon_stops(tmp_path):
    units = FakeUnits(tmp_path)
    path = units.config / "systemd/user" / WEB_UNIT_NAME
    path.write_text(path.read_text() + "Environment=UNREVIEWED=1\n")
    with pytest.raises(ActivationError, match="different user unit"):
        await units.controller().configure(release())
    assert units.calls == [] and units.rows[UNIT_NAME]["active"]


@pytest.mark.asyncio
async def test_dropins_and_stale_loaded_units_are_refused(tmp_path):
    units = FakeUnits(tmp_path)

    async def command(unit, *args):
        return (await units.command(unit, *args)).replace(
            b"DropInPaths=\n", b"DropInPaths=/other.conf\n"
        )

    with pytest.raises(ActivationError) as error:
        await inspect_unit(UNIT_NAME, units.prefix, units.paths, units.config, command=command)
    assert error.value.code == "unit_conflict" and units.calls == []


@pytest.mark.asyncio
async def test_busy_web_drain_does_not_stop_any_service(tmp_path):
    units = FakeUnits(tmp_path)
    controller = units.controller()

    async def request(method):
        return {"drained": False, "pending_operations": 1}

    controller.web_client.request = request
    await controller.configure(release())
    with pytest.raises(ActivationError) as error:
        await controller.stop()
    assert error.value.code == "busy"
    assert not units.calls and all(row["active"] for row in units.rows.values())
    ConfigurationStore(units.paths.configuration_file).save(
        ServerConfiguration(mode=ServerMode.RING_ONLY)
    )
    selected = SelectionStore(units.prefix).activate(A, expected_revision=0)
    activator = ReleaseActivator(
        units.prefix, units.paths.state_directory, controller, verify=lambda *args: release(B)
    )
    with pytest.raises(ActivationError) as blocked:
        await activator.activate(B, expected_revision=selected.revision)
    assert blocked.value.code == "busy"
    assert activator.selection.load() == selected
    assert not units.calls
    assert all(row["active"] and row["enabled"] for row in units.rows.values())


@pytest.mark.asyncio
async def test_interrupted_web_drain_exposes_pending_without_canceling_write(tmp_path, monkeypatch):
    from halo_server.web import routes
    from halo_server.web.routes import WebConsole
    from halo_server.web.server import WebHealth

    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    console = WebConsole(paths, SimpleNamespace())
    health = WebHealth(console)
    health.ready = True
    entered, finish = asyncio.Event(), asyncio.Event()
    cancelled = False

    async def write():
        nonlocal cancelled
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return {"saved": True}

    await console.operations.start()
    identifier = str(uuid4())
    task = asyncio.create_task(console.operations.run(identifier, "configuration", {}, write))
    try:
        await entered.wait()
        initial = await console.state.get()
        assert not initial["web"]["draining"]
        monkeypatch.setattr(routes, "DRAIN_SECONDS", 0.01)
        result = await console.drain()
        assert result == {"drained": False, "pending_operations": 1}
        assert not cancelled and not task.done()
        observed = await console.state.get()  # Cached daemon read, fresh web lifecycle.
        assert observed["web"]["draining"] and observed["web"]["pending_operations"] == 1
        local = await health.handle("status", {})
        assert local["web"]["draining"] and not local["accepting_operations"]
        finish.set()
        assert (await task)["status"] == "completed"
        assert console.operations.get(identifier)["status"] == "completed"
        assert console.pending_operations == 0 and console.draining
        assert (await health.handle("status", {}))["pending_operations"] == 0
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        await console.close()


@pytest.mark.asyncio
async def test_release_activator_suppresses_web_before_selector_write(tmp_path):
    units = FakeUnits(tmp_path)
    ConfigurationStore(units.paths.configuration_file).save(
        ServerConfiguration(mode=ServerMode.RING_ONLY)
    )
    selection = SelectionStore(units.prefix).activate(A, expected_revision=0)
    controller = units.controller()
    activator = ReleaseActivator(
        units.prefix,
        units.paths.state_directory,
        controller,
        verify=lambda *a: release(B, web=False),
    )
    original = activator.selection.activate

    def activate(*args, **kwargs):
        assert not any(row["active"] for row in units.rows.values())
        assert not units.rows[WEB_UNIT_NAME]["enabled"]
        assert startup_store(units.paths.state_directory).load().suppressed
        units.reference = B
        return original(*args, **kwargs)

    activator.selection.activate = activate
    result = await activator.activate(B, expected_revision=selection.revision)
    assert result.status == "active" and units.rows[UNIT_NAME]["active"]
    assert not units.rows[WEB_UNIT_NAME]["active"]


@pytest.fixture
def owner_proof(monkeypatch):
    import halo_server.web_restart as module

    holder = {}

    async def capture(client):
        return SimpleNamespace(pid=holder["units"].rows[UNIT_NAME]["pid"], close=lambda: None)

    monkeypatch.setattr(module, "capture_owner", capture)
    return holder


@pytest.mark.asyncio
async def test_restart_sends_once_preserves_enablement_and_idempotency(tmp_path, owner_proof):
    units = FakeUnits(tmp_path)
    owner_proof["units"] = units
    units.rows[UNIT_NAME]["enabled"] = False
    selection = SelectionStore(units.prefix).activate(A, expected_revision=0)
    restart = units.restart()
    identifier = str(uuid4())
    assert (await restart.info())["selection_revision"] == selection.revision
    assert (await restart.start(identifier, selection.revision))["status"] == "pending"
    await restart.tasks[identifier]
    completed = await restart.get(identifier)
    assert completed["status"] == "completed" and completed["final_invocation"]
    assert await restart.start(identifier, selection.revision) == completed
    assert units.calls == [(UNIT_NAME, "restart")]
    assert units.rows[UNIT_NAME]["enabled"] is False and units.rows[WEB_UNIT_NAME]["active"]
    await restart.close()


@pytest.mark.asyncio
async def test_restart_holds_canonical_lock_and_cancellation_does_not_repeat(tmp_path, owner_proof):
    units = FakeUnits(tmp_path)
    owner_proof["units"] = units
    selection = SelectionStore(units.prefix).activate(A, expected_revision=0)
    units.block_restart, units.release_restart = asyncio.Event(), asyncio.Event()
    restart = units.restart()
    identifier = str(uuid4())
    await restart.start(identifier, selection.revision)
    await units.block_restart.wait()
    with pytest.raises(ActivationError) as error:
        InstallLock(units.paths.state_directory, name="release-install.lock")
    assert error.value.code == "busy"
    second = units.restart()
    assert (await second.get(identifier))["status"] == "pending"
    with pytest.raises(ActivationError) as error:
        await second.start(str(uuid4()), selection.revision)
    assert error.value.code == "busy"
    units.release_restart.set()
    await restart.tasks[identifier]
    assert (await second.get(identifier))["status"] == "completed"
    assert units.calls == [(UNIT_NAME, "restart")]


@pytest.mark.asyncio
async def test_restart_interrupted_before_send_is_unknown_and_never_replayed(tmp_path):
    units = FakeUnits(tmp_path)
    selection = SelectionStore(units.prefix).activate(A, expected_revision=0)
    restart = units.restart()
    record = RestartRecord(
        str(uuid4()),
        selection.revision,
        A.release_id,
        A.manifest_sha256,
        units.rows[UNIT_NAME]["invocation"],
    )
    restart.store.update(lambda journal: replace(journal, operations=(record,)))
    assert (await restart.get(record.operation_id))["status"] == "unknown"
    assert (await restart.start(record.operation_id, selection.revision))["status"] == "unknown"
    assert units.calls == []


@pytest.mark.asyncio
async def test_restart_rejects_stale_selection_and_custom_unit_before_write(tmp_path):
    units = FakeUnits(tmp_path)
    selection = SelectionStore(units.prefix).activate(A, expected_revision=0)
    restart = units.restart()
    with pytest.raises(ActivationError) as error:
        await restart.start(str(uuid4()), selection.revision - 1)
    assert error.value.code == "configuration_conflict"
    path = units.config / "systemd/user" / UNIT_NAME
    path.write_text("unmanaged unit")
    with pytest.raises(ActivationError) as error:
        await restart.start(str(uuid4()), selection.revision)
    assert error.value.code == "unit_conflict"
    assert units.calls == [] and restart.store.load().operations == ()


def test_installed_launcher_verification_is_read_only_and_rejects_changed_bytes(tmp_path):
    import os
    import sys

    from halo_server.release import ReleaseError
    from halo_server.release_launcher import install_launchers, verify_installed_launchers

    prefix = tmp_path / "prefix"
    prefix.mkdir(mode=0o700)
    interpreter = Path(os.path.realpath(sys.executable))
    install_launchers(prefix, interpreter=interpreter)
    verify_installed_launchers(prefix, interpreter=interpreter)
    launcher = prefix / "bin/halo-server"
    launcher.chmod(0o755)
    original = launcher.read_bytes()
    launcher.write_bytes(original + b"# changed\n")
    launcher.chmod(0o555)
    with pytest.raises(ReleaseError):
        verify_installed_launchers(prefix, interpreter=interpreter)
    assert launcher.read_bytes() == original + b"# changed\n"


@pytest.mark.asyncio
async def test_uncertain_journal_save_never_sends_or_replays_restart(tmp_path, monkeypatch):
    from halo_server.private_json import PrivateJsonError

    units = FakeUnits(tmp_path)
    selection = SelectionStore(units.prefix).activate(A, expected_revision=0)
    restart = units.restart()
    original = restart.store.save

    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise PrivateJsonError("synthetic post-replace uncertainty", possibly_saved=True)

    monkeypatch.setattr(restart.store, "save", uncertain)
    identifier = str(uuid4())
    with pytest.raises(PrivateJsonError):
        await restart.start(identifier, selection.revision)
    assert (await restart.get(identifier))["status"] == "unknown"
    assert (await restart.start(identifier, selection.revision))["status"] == "unknown"
    assert units.calls == []


@pytest.mark.asyncio
async def test_possibly_sent_restart_reconciles_without_resend(tmp_path, owner_proof):
    units = FakeUnits(tmp_path)
    owner_proof["units"] = units
    selection = SelectionStore(units.prefix).activate(A, expected_revision=0)
    restart = units.restart()
    original = restart.command

    async def command(unit, *arguments):
        result = await original(unit, *arguments)
        if arguments == ("restart",):
            raise ActivationError("synthetic lost acknowledgement", code="service_failed")
        return result

    restart.command = command
    identifier = str(uuid4())
    await restart.start(identifier, selection.revision)
    await restart.tasks[identifier]
    assert restart.store.load().operations[0].status == "unknown"
    assert (await restart.get(identifier))["status"] == "completed"
    assert units.calls == [(UNIT_NAME, "restart")]
