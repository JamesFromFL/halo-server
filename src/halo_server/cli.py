"""Command-line entry point for the Halo Server daemon and admin client."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from halo_server import __version__
from halo_server.configuration import (
    ConfigurationError,
    ConfigurationStore,
    ServerConfiguration,
    ServerMode,
    is_loopback_host,
)
from halo_server.paths import ServerPaths
from halo_server.web.defaults import DEFAULT_WEB_PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="halo-server",
        description="Headless Ring event and media backend for Halo integrations.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    daemon = subparsers.add_parser("daemon", help="Run the foreground Halo Server daemon")
    _add_path_arguments(daemon)
    daemon.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    daemon.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in ServerMode),
        default=None,
        help="Override the saved operating mode for this daemon run",
    )
    daemon.add_argument(
        "--plugin-host",
        type=_loopback_host,
        default=None,
        help="Override saved plugin API loopback bind address",
    )
    daemon.add_argument(
        "--plugin-port",
        type=_tcp_port,
        default=None,
        help="Override saved plugin API TCP port (0 selects an ephemeral port)",
    )
    daemon.add_argument(
        "--rtsp-host",
        type=_loopback_host,
        default=None,
        help="Override saved RTSP media loopback bind address",
    )
    daemon.add_argument(
        "--rtsp-port",
        type=_tcp_port,
        default=None,
        help="Override saved RTSP media TCP port (0 selects an ephemeral port)",
    )

    tui = subparsers.add_parser("tui", help="Open the local administration interface")
    _add_path_arguments(tui)

    web = subparsers.add_parser("web", help="Run or configure the secure browser interface")
    _add_path_arguments(web)
    web_commands = web.add_subparsers(dest="web_command")
    web_commands.add_parser("serve", help="Run the independent HTTPS companion")
    web_configure = web_commands.add_parser("configure", help="Prepare local HTTPS access")
    web_configure.add_argument("--host", default="127.0.0.1")
    web_configure.add_argument(
        "--port",
        type=_tcp_port,
        default=DEFAULT_WEB_PORT,
        help=f"HTTPS port (default: {DEFAULT_WEB_PORT})",
    )
    web_configure.add_argument("--canonical-host")
    web_configure.add_argument(
        "--renew-certificate",
        action="store_true",
        help="Explicitly replace the local certificate and fingerprint",
    )
    web_configure.add_argument(
        "--allow-lan", action="store_true", help="Explicitly allow local-network access"
    )
    web_commands.add_parser("access", help="Show the access URL and certificate fingerprint")
    web_commands.add_parser("setup", help="Issue a short-lived initial owner enrollment code")
    web_certificate = web_commands.add_parser(
        "certificate", help="Import an owner-trusted TLS certificate"
    )
    web_certificate.add_argument("--certificate", type=Path, required=True)
    web_certificate.add_argument("--private-key", type=Path, required=True)
    web_reset = web_commands.add_parser("reset", help="Reset only the web password and sessions")
    web_reset.add_argument("--yes", action="store_true", help="Confirm web authentication recovery")

    status = subparsers.add_parser("status", help="Print daemon status as JSON")
    _add_path_arguments(status)

    shutdown = subparsers.add_parser("shutdown", help="Stop the local daemon cleanly")
    _add_path_arguments(shutdown)

    homekit = subparsers.add_parser("homekit", help="Manage direct HomeKit cameras and pairing")
    _add_path_arguments(homekit)
    commands = homekit.add_subparsers(dest="homekit_command", required=True)
    commands.add_parser("list", help="List cameras, selection and stable accessory identity")
    selection = commands.add_parser("select", help="Replace the explicit selected camera set")
    selection.add_argument("camera_ids", nargs="*")
    selection.add_argument("--none", action="store_true", help="Explicitly deselect every camera")
    warm = commands.add_parser("warm", help="Opt a selected camera into warm recording")
    warm.add_argument("camera_id")
    warm.add_argument("state", choices=("on", "off"))
    pairing = commands.add_parser("pairing", help="Inspect or explicitly reveal/reset setup data")
    pairing_commands = pairing.add_subparsers(dest="pairing_command", required=True)
    pairing_commands.add_parser("list")
    reveal = pairing_commands.add_parser("reveal", help="Print sensitive camera setup data")
    reveal.add_argument("camera_id")
    reset = pairing_commands.add_parser("reset", help="Remove one camera's Apple Home pairing")
    reset.add_argument("camera_id")
    reset.add_argument("--yes", action="store_true", help="Confirm reset and adapter restart")
    reset.add_argument("--operation-id", type=_operation_id, help="Resume an exact pending reset")

    configuration = subparsers.add_parser(
        "config", help="Inspect/save settings; restart applies them"
    )
    _add_path_arguments(configuration)
    configurations = configuration.add_subparsers(dest="config_command", required=True)
    configurations.add_parser("get")
    save = configurations.add_parser("set")
    save.add_argument("--expected-revision", type=int)
    save.add_argument("--mode", choices=tuple(mode.value for mode in ServerMode))
    save.add_argument("--plugin-host", type=_loopback_host)
    save.add_argument("--plugin-port", type=_tcp_port)
    save.add_argument("--rtsp-host", type=_loopback_host)
    save.add_argument("--rtsp-port", type=_tcp_port)

    control = subparsers.add_parser("camera-control", help="Set an evidenced light/siren control")
    _add_path_arguments(control)
    control.add_argument("camera_id")
    control.add_argument("control", choices=("light", "siren"))
    control.add_argument("state", choices=("on", "off"))
    doctor = subparsers.add_parser(
        "doctor", help="Run bounded local diagnostics without waking cameras"
    )
    _add_path_arguments(doctor)
    doctor.add_argument("--probe", action="store_true", help="Explicit bounded local codec probes")
    support = subparsers.add_parser("support", help="Export a bounded sanitized support report")
    _add_path_arguments(support)
    support.add_argument("--output", type=Path, required=True)
    capture = subparsers.add_parser(
        "stream-capture", help="Explicit bounded stream diagnostics with private media excerpts"
    )
    _add_path_arguments(capture)
    captures = capture.add_subparsers(dest="capture_command", required=True)
    begin = captures.add_parser("start", help="Arm diagnostics; the camera stays closed")
    begin.add_argument("camera_id")
    begin.add_argument("--seconds", type=int, default=240)
    begin.add_argument("--capture-id", default=None, help="Optional UUID for idempotent retries")
    captures.add_parser("status")
    for name in ("mark", "stop"):
        action = captures.add_parser(name)
        action.add_argument("capture_id", help="Capture UUID returned by start")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    _require_native_platform(parser)
    try:
        paths = _paths_from_args(args)
        from halo_server.release_execution import validate_runtime_state

        validate_runtime_state(paths.state_directory)
    except (RuntimeError, OSError, ValueError) as error:
        print(f"halo-server: {error}", file=sys.stderr)
        return 1
    if args.command == "daemon":
        from halo_server.logging_setup import configure_logging

        configure_logging(args.log_level)
        try:
            configuration = _configuration_from_args(paths, args)
        except ConfigurationError as exc:
            print(f"halo-server: {exc}", file=sys.stderr)
            return 1
        return asyncio.run(_run_daemon(paths, configuration=configuration))
    if args.command == "tui":
        from halo_server.tui.app import HaloTui
        from halo_server.tui.client import AdminClient

        HaloTui(AdminClient(paths.admin_socket)).run()
        return 0
    if args.command == "web":
        if args.web_command == "reset" and not args.yes:
            parser.error("web reset revokes browser access; use --yes to confirm recovery")
        return _run_web_command(paths, args)
    if args.command in {"status", "shutdown"}:
        return asyncio.run(_run_admin_command(paths, args.command))
    if args.command in {
        "homekit",
        "config",
        "camera-control",
        "doctor",
        "support",
        "stream-capture",
    }:
        if (
            args.command == "homekit"
            and args.homekit_command == "select"
            and bool(args.camera_ids) == args.none
        ):
            parser.error("select requires camera IDs or --none")
        if (
            args.command == "homekit"
            and args.homekit_command == "pairing"
            and args.pairing_command == "reset"
            and not args.yes
        ):
            parser.error("reset removes this camera's pairing and restarts the adapter; use --yes")
        return asyncio.run(_run_owner_command(paths, args))
    parser.error(f"Unknown command: {args.command}")
    return 2


def _run_web_command(paths: ServerPaths, args: argparse.Namespace) -> int:
    """Only local owner commands may configure access or recover credentials."""
    lease = None
    try:
        from halo_server.web import auth, server, settings

        command = args.web_command or "serve"
        if command == "serve":
            asyncio.run(server.serve(paths))
            return 0
        from halo_server.release_execution import validate_runtime_state
        from halo_server.release_guard import acquire_source_guard

        lease = acquire_source_guard(paths.state_directory)
        validate_runtime_state(paths.state_directory)
        if command == "configure":
            result = settings.configure(
                paths,
                host=args.host,
                port=args.port,
                canonical_host=args.canonical_host,
                allow_lan=args.allow_lan,
                renew_certificate=args.renew_certificate,
            )
        elif command == "access":
            result = settings.access_info(paths)
        elif command == "certificate":
            result = settings.import_certificate(paths, args.certificate, args.private_key)
        else:
            result = auth.issue_setup(paths, reset=command == "reset")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RuntimeError, OSError, ValueError) as error:
        print(f"halo-server web: {error}", file=sys.stderr)
        return 1
    finally:
        if lease is not None:
            lease.close()


def _add_path_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-directory",
        type=Path,
        help="Override the Halo Server state directory",
    )
    parser.add_argument(
        "--runtime-directory",
        type=Path,
        help="Override the Halo Server runtime directory",
    )


def _paths_from_args(args: argparse.Namespace) -> ServerPaths:
    defaults = ServerPaths.from_environment()
    state = args.state_directory or defaults.state_directory
    runtime = args.runtime_directory or defaults.runtime_directory
    # Normalize lexically: resolving would silently follow a sensitive-path symlink.
    return ServerPaths(
        Path(os.path.abspath(state.expanduser())), Path(os.path.abspath(runtime.expanduser()))
    )


def _configuration_from_args(paths: ServerPaths, args: argparse.Namespace) -> ServerConfiguration:
    stored = ConfigurationStore(paths.configuration_file).load()
    overrides = {
        name: value
        for name in ("mode", "plugin_host", "plugin_port", "rtsp_host", "rtsp_port")
        if (value := getattr(args, name)) is not None
    }
    proposed = replace(stored, **overrides)
    from halo_server.release_execution import validate_operating_mode

    validate_operating_mode(proposed)
    return proposed


def _loopback_host(value: str) -> str:
    if not is_loopback_host(value):
        raise argparse.ArgumentTypeError(
            "plugin host must be a loopback address until TLS is configured"
        )
    return value


def _tcp_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("TCP port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("TCP port must be between 0 and 65535")
    return port


def _operation_id(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as error:
        raise argparse.ArgumentTypeError("Operation ID must be a canonical UUID") from error
    return value


def _require_native_platform(parser: argparse.ArgumentParser) -> None:
    if sys.platform in {"linux", "darwin"}:
        return
    if sys.platform == "win32":
        parser.error(
            "native Windows is not supported; run Halo Server in Linux. "
            "WSL2 direct HomeKit networking remains unqualified"
        )
    parser.error(f"native Halo Server does not support this platform: {sys.platform}")


async def _run_daemon(
    paths: ServerPaths,
    *,
    configuration: ServerConfiguration,
) -> int:
    from halo_server.service import HaloService

    service = HaloService(
        paths,
        configuration=configuration,
        plugin_host=configuration.plugin_host if configuration.compatibility_enabled else None,
        plugin_port=configuration.plugin_port,
        rtsp_host=configuration.rtsp_host if configuration.compatibility_enabled else None,
        rtsp_port=configuration.rtsp_port,
    )
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, service.request_stop)
    try:
        await service.start()
        await service.wait_for_stop()
    finally:
        await service.stop()
    return 0


async def _run_admin_command(paths: ServerPaths, command: str) -> int:
    from halo_server.tui.client import AdminClient, AdminClientError

    client = AdminClient(paths.admin_socket)
    try:
        if command == "status":
            result = await client.status()
        else:
            result = await client.request("shutdown")
    except AdminClientError as exc:
        print(f"halo-server: {exc}")
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


async def _run_owner_command(paths: ServerPaths, args: argparse.Namespace) -> int:
    from halo_server.tui.client import AdminClient, AdminClientError
    from halo_server.tui.support import write_support_report

    client = AdminClient(paths.admin_socket)
    try:
        if args.command == "homekit":
            result = await _homekit_command(client, args)
        elif args.command == "config":
            current = await client.configuration_get()
            if args.config_command == "get":
                result = current
            else:
                saved = dict(current["saved"])
                changes = {
                    name: value
                    for name in ("mode", "plugin_host", "plugin_port", "rtsp_host", "rtsp_port")
                    if (value := getattr(args, name)) is not None
                }
                if not changes:
                    raise AdminClientError("bad_params", "config set requires a setting to change")
                revision = (
                    saved["revision"] if args.expected_revision is None else args.expected_revision
                )
                saved.update(changes)
                result = await client.configuration_set(revision, saved)
        elif args.command == "camera-control":
            page = await client.camera_controls()
            camera = _camera_row(page, args.camera_id)
            result = await client.camera_control(
                page["profile_id"],
                page["account_generation"],
                args.camera_id,
                camera["device_generation"],
                str(uuid4()),
                args.control,
                args.state == "on",
            )
        elif args.command == "stream-capture":
            params = {}
            if args.capture_command == "start":
                params = {
                    "camera_id": args.camera_id,
                    "seconds": args.seconds,
                    "capture_id": args.capture_id or str(uuid4()),
                    "record_media": True,
                }
            elif args.capture_command != "status":
                params = {"capture_id": args.capture_id}
            result = await client.request("stream_capture_" + args.capture_command, **params)
        elif args.command == "doctor":
            try:
                result = await client.doctor(probe=args.probe)
            except AdminClientError as error:
                if error.code != "daemon_unavailable":
                    raise
                from halo_server.diagnostics import local_doctor

                result = await local_doctor(paths, probe=args.probe)
        else:
            result = await client.support_report()
            write_support_report(args.output, result)
            print(f"Saved private support report: {args.output}")
            return 0
    except (AdminClientError, OSError, ValueError) as error:
        print(f"halo-server: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


async def _homekit_command(client, args: argparse.Namespace):
    from halo_server.tui.client import AdminClientError

    if args.homekit_command == "list":
        return await client.homekit_cameras()
    if args.homekit_command == "select":
        page = await client.homekit_cameras()
        if len(set(args.camera_ids)) != len(args.camera_ids):
            raise AdminClientError("bad_params", "Camera IDs must be unique")
        for camera_id in args.camera_ids:
            _camera_row(page, camera_id)
        return await client.homekit_select(
            page["profile_id"], page["account_generation"], args.camera_ids, page["revision"]
        )
    if args.homekit_command == "warm":
        page = await client.homekit_recording_cameras()
        _camera_row(page, args.camera_id)
        return await client.homekit_recording_warm(
            page["profile_id"],
            page["account_generation"],
            args.camera_id,
            page["revision"],
            args.state == "on",
        )
    page = await client.homekit_pairing_list()
    if args.pairing_command == "list":
        return page
    row = _camera_row(page, args.camera_id)
    if args.pairing_command == "reveal":
        return await client.homekit_pairing_reveal(page, row)
    operation = args.operation_id or row.get("reset_operation_id") or str(uuid4())
    return await client.homekit_pairing_reset(page, row, operation)


def _camera_row(page, camera_id):
    from halo_server.tui.client import AdminClientError

    rows = [row for row in page["cameras"] if row.get("camera_id") == camera_id]
    if len(rows) != 1:
        raise AdminClientError("camera_unavailable", "Camera is not in the current observed view")
    return rows[0]
