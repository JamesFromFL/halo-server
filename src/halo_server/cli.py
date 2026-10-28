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

from halo_server import __version__
from halo_server.configuration import (
    ConfigurationError,
    ConfigurationStore,
    ServerConfiguration,
    ServerMode,
    is_loopback_host,
)
from halo_server.paths import ServerPaths


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
        help="Override saved operating mode (HomeKit camera publication is not implemented yet)",
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

    status = subparsers.add_parser("status", help="Print daemon status as JSON")
    _add_path_arguments(status)

    shutdown = subparsers.add_parser("shutdown", help="Stop the local daemon cleanly")
    _add_path_arguments(shutdown)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    _require_native_platform(parser)
    paths = _paths_from_args(args)
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
    if args.command in {"status", "shutdown"}:
        return asyncio.run(_run_admin_command(paths, args.command))
    parser.error(f"Unknown command: {args.command}")
    return 2


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
    return replace(stored, **overrides)


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


def _require_native_platform(parser: argparse.ArgumentParser) -> None:
    if sys.platform in {"linux", "darwin"}:
        return
    if sys.platform == "win32":
        parser.error(
            "native Windows is not supported; Halo Server and Scrypted must run together "
            "in a Linux environment such as WSL2"
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
