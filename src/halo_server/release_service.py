"""One user unit, bounded systemctl ownership and local-only activation health."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import secrets
import signal
import stat
from pathlib import Path

from halo_server.media.manager import owned_thread, settle_owned
from halo_server.release_activation import ActivationError, ReleaseReference
from halo_server.release_legacy import capture_owner, verify_stopped

UNIT_NAME = "halo-server.service"
COMMAND_SECONDS = 55.0
HEALTH_SECONDS = 25.0


def _unit_argument(path):
    value = str(path)
    if not Path(path).is_absolute() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ActivationError("User service path is invalid")
    # systemd syntax quoting plus specifier/environment escaping; no shell.
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def render_user_unit(prefix, *, state_directory=None):
    launcher = Path(prefix) / "bin" / "halo-server"
    command = _unit_argument(launcher)
    state = (
        f" --state-directory {_unit_argument(state_directory)}"
        if state_directory is not None
        else ""
    )
    return f"""[Unit]
Description=Halo Server
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
ExecStart=/usr/bin/env -- {command}{state} daemon
Restart=on-failure
RestartSec=5
TimeoutStartSec=45
TimeoutStopSec=45
KillMode=control-group
SendSIGKILL=yes
UMask=0077
NoNewPrivileges=yes
LimitCORE=0
Environment=PYTHONNOUSERSITE=1

[Install]
WantedBy=default.target
"""


async def _systemctl(*arguments):
    async def run():
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/systemctl",
            "--user",
            "--no-ask-password",
            *arguments,
            *(() if arguments == ("daemon-reload",) else (UNIT_NAME,)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(COMMAND_SECONDS):
                output = bytearray()
                while len(output) <= 4096:
                    chunk = await process.stdout.read(min(1024, 4097 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                if len(output) > 4096 or await process.wait() != 0:
                    raise ActivationError("User service operation failed", code="service_failed")
                return bytes(output)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            async with asyncio.timeout(5):
                await process.wait()

    task = asyncio.create_task(run(), name="managed-user-service")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await settle_owned(task)
        raise


def _unit_directory(path):
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            sticky_system_parent = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
            if (
                metadata.st_uid not in {0, os.getuid()}
                or metadata.st_mode & 0o022
                and not sticky_system_parent
            ):
                raise ActivationError("User unit directory is unsafe")
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise ActivationError("User unit directory is unsafe")
        result, descriptor = descriptor, -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unit_metadata(descriptor):
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ActivationError("Existing user unit is unsafe")
    return metadata


def _prepare_unit(config_directory, prefix, state_directory=None):
    body = render_user_unit(prefix, state_directory=state_directory).encode()
    directory = _unit_directory(Path(config_directory) / "systemd" / "user")
    lock = temporary = -1
    temporary_name = None
    temporary_created = published = False
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            lock = os.open(
                ".halo-server.service.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
            )
            os.fchmod(lock, 0o600)
        except FileExistsError:
            lock = os.open(".halo-server.service.lock", flags, dir_fd=directory)
        metadata = _unit_metadata(lock)
        if metadata.st_size != 0:
            raise ActivationError("User unit lock is invalid")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            existing = os.open(
                UNIT_NAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
        except FileNotFoundError:
            existing = -1
        if existing >= 0:
            try:
                metadata = _unit_metadata(existing)
                if metadata.st_size != len(body):
                    raise ActivationError(
                        "A different user unit already exists", code="unit_conflict"
                    )
                current = bytearray()
                while len(current) <= len(body):
                    chunk = os.read(existing, len(body) + 1 - len(current))
                    if not chunk:
                        break
                    current.extend(chunk)
                if bytes(current) != body:
                    raise ActivationError(
                        "A different user unit already exists", code="unit_conflict"
                    )
                os.fsync(existing)
                os.fsync(directory)
                return False
            finally:
                os.close(existing)
        temporary_name = f".halo-server.service.{secrets.token_hex(12)}.tmp"
        temporary = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        temporary_created = True
        os.fchmod(temporary, 0o600)
        remaining = memoryview(body)
        while remaining:
            written = os.write(temporary, remaining)
            if written <= 0:
                raise ActivationError("User unit write made no progress")
            remaining = remaining[written:]
        os.fsync(temporary)
        os.close(temporary)
        temporary = -1
        # Atomic no-replace publication: never overwrite a concurrent edit.
        os.link(
            temporary_name,
            UNIT_NAME,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        published = True
        os.unlink(temporary_name, dir_fd=directory)
        temporary_name = None
        os.fsync(directory)
        return True
    except OSError as error:
        raise ActivationError(
            "User unit could not be installed safely", possibly_saved=published
        ) from error
    finally:
        if temporary >= 0:
            os.close(temporary)
        if temporary_created and temporary_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=directory)
        if lock >= 0:
            os.close(lock)
        os.close(directory)


class UserServiceController:
    """No implicit install/enable. The operator chooses the single user unit."""

    def __init__(
        self, admin_client, *, command=_systemctl, config_directory=None, state_directory=None
    ):
        self.client, self.command = admin_client, command
        self.state_directory = Path(state_directory) if state_directory is not None else None
        if self.state_directory is not None and not self.state_directory.is_absolute():
            raise ActivationError("State directory must be absolute")
        self.config_directory = (
            Path(config_directory)
            if config_directory is not None
            else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        )
        if not self.config_directory.is_absolute():
            raise ActivationError("User configuration directory must be absolute")

    async def prepare(self, prefix):
        await owned_thread(_prepare_unit, self.config_directory, prefix, self.state_directory)
        await self.command("daemon-reload")

    async def stop(self):
        owner = await capture_owner(self.client)
        try:
            await self.command("stop")
            await verify_stopped(self.client, owner)
        finally:
            if owner is not None:
                owner.close()
        # The activation owner additionally requires the canonical EX lifetime
        # guard; a legacy parent PID alone cannot prove child retirement.

    async def disable(self):
        await self.command("disable")

    async def start(self, reference):
        if not isinstance(reference, ReleaseReference):
            raise ActivationError("Release identity is invalid")
        await self.command("start")

    async def healthy(self, reference):
        deadline = asyncio.get_running_loop().time() + HEALTH_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with asyncio.timeout(min(2, deadline - asyncio.get_running_loop().time())):
                    status = await self.client.status()
                if local_release_healthy(status, reference):
                    return True
            except Exception:
                pass
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
        return False


def local_release_healthy(status, reference):
    if not isinstance(status, dict) or not isinstance(reference, ReleaseReference):
        return False
    identity = status.get("release")
    if (
        not isinstance(identity, dict)
        or identity.get("release_id") != reference.release_id
        or identity.get("manifest_sha256") != reference.manifest_sha256
        or type(identity.get("minimum_reader")) is not int
        or identity["minimum_reader"] < 14
    ):
        return False
    configuration = status.get("configuration")
    if not isinstance(configuration, dict) or configuration.get("mode") not in {
        "ring-only",
        "compatibility",
        "homekit",
        "dual",
    }:
        return False
    if status.get("shutdown_complete") is not False or status.get("service") != "running":
        return False
    if configuration["mode"] in {"homekit", "dual"}:
        native = status.get("homekit")
        if not isinstance(native, dict) or not all(
            native.get(key) is True
            for key in (
                "transport_ready",
                "live_video_ready",
                "live_audio_ready",
                "snapshots_ready",
                "recording_samples_ready",
            )
        ):
            return False
    if configuration["mode"] in {"compatibility", "dual"}:
        integrations, media = status.get("integrations"), status.get("media")
        api = integrations.get("api") if isinstance(integrations, dict) else None
        gateway = media.get("gateway") if isinstance(media, dict) else None
        if any(
            not isinstance(endpoint, dict)
            or endpoint.get("enabled") is not True
            or type(endpoint.get("port")) is not int
            or not 1 <= endpoint["port"] <= 65535
            for endpoint in (api, gateway)
        ):
            return False
    # Cloud/session/camera/hub availability and recording warm demand are
    # deliberately irrelevant to whether the local release itself works.
    return True
