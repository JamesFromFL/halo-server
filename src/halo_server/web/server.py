"""Independent HTTPS companion with owner health and release lifetime ownership."""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import stat

from aiohttp import web

from halo_server.api.admin import AdminProtocolError, AdminServer
from halo_server.paths import open_private_directory
from halo_server.release_execution import release_status, validate_runtime_state
from halo_server.release_guard import acquire_source_guard
from halo_server.web.routes import WebConsole, create_application
from halo_server.web.settings import SettingsStore, WebSettingsError, tls_context, web_directory

# A failed source retirement must retain its real process-lifetime claim. Do not
# allow a second invocation to hide that failure or reopen release activation.
_retained_retirements = {}


class CompanionLease:
    """Atomic process ownership, independent of HTTP and Unix socket addresses."""

    def __init__(self, paths):
        self.descriptor = -1
        directory = open_private_directory(web_directory(paths), create=True)
        descriptor = -1
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            try:
                descriptor = os.open(
                    "companion.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
                )
                os.fchmod(descriptor, 0o600)
            except FileExistsError:
                descriptor = os.open("companion.lock", flags, dir_fd=directory)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_size != 0
            ):
                raise WebSettingsError("Web companion ownership file is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WebSettingsError("Another web companion already owns this state") from None
            self.descriptor, descriptor = descriptor, -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory)

    def close(self):
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


class BoundedServer(web.Server):
    """Bound accepted HTTP keep-alive connections as well as active requests."""

    def connection_made(self, handler, transport):
        if len(self.connections) >= 32:
            transport.close()
            return
        super().connection_made(handler, transport)


class BoundedRunner(web.AppRunner):
    async def _make_server(self):
        original = await super()._make_server()
        return BoundedServer(
            original.request_handler,
            request_factory=original.request_factory,
            handler_cancellation=True,
            access_log=None,
            keepalive_timeout=15,
            max_line_size=4096,
            max_field_size=8192,
        )


class WebHealth:
    def __init__(self, console):
        self.console = console
        self.ready = False

    async def handle(self, method, params):
        if params:
            raise AdminProtocolError("bad_params", "Web health parameters are invalid")
        if method == "drain":
            return await self.console.drain()
        if method != "status":
            raise AdminProtocolError("unknown_method", "Unknown web health method")
        return {
            "service": "running",
            "shutdown_complete": False,
            "boot_id": self.console.state.boot_id,
            "release": release_status(),
            "web": {"ready": self.ready} | self.console.web_status(),
            "accepting_operations": self.ready and not self.console.draining,
            "pending_operations": self.console.pending_operations,
        }


async def serve(paths, *, stop_event=None):
    """Run until an owner stop/signal. No daemon or camera is started implicitly."""
    await asyncio.to_thread(paths.prepare)
    if paths.state_directory in _retained_retirements:
        raise RuntimeError("Previous web companion retirement is incomplete")
    lease = acquire_source_guard(paths.state_directory)
    runner = health_server = console = health = ownership = None
    signals = []
    try:
        validate_runtime_state(paths.state_directory)
        ownership = CompanionLease(paths)
        settings = await asyncio.to_thread(SettingsStore(paths).load)
        if not settings.enabled:
            raise WebSettingsError("Web access is disabled; configure it through the owner CLI")
        tls = await asyncio.to_thread(tls_context, paths, settings)
        console = WebConsole(paths, settings)
        app = create_application(console)
        runner = BoundedRunner(app, shutdown_timeout=35, access_log=None, handler_cancellation=True)
        health = WebHealth(console)
        health_server = AdminServer(
            paths.runtime_directory / "web-admin.sock",
            health.handle,
            max_connections=4,
            handler_timeout=40,
        )
        stopping = stop_event if stop_event is not None else asyncio.Event()
        if stop_event is None:
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stopping.set)
                signals.append(signum)
        # Publish private health before startup reconciles mutation journals or
        # accepts browser writes. CompanionLease makes concurrent starts atomic.
        await health_server.start()
        await runner.setup()
        await web.TCPSite(runner, settings.host, settings.port, ssl_context=tls, backlog=32).start()
        health.ready = True
        await stopping.wait()
    finally:
        if health is not None:
            health.ready = False
        errors = []
        for resource, method in ((runner, "cleanup"), (console, "close"), (health_server, "stop")):
            if resource is None:
                continue
            try:
                await getattr(resource, method)()
            except BaseException as error:
                errors.append(error)
        for signum in signals:
            asyncio.get_running_loop().remove_signal_handler(signum)
        if errors:
            _retained_retirements[paths.state_directory] = (
                lease,
                runner,
                console,
                health_server,
                ownership,
            )
            raise BaseExceptionGroup("Web companion retirement is incomplete", errors)
        if ownership is not None:
            ownership.close()
        lease.close()
