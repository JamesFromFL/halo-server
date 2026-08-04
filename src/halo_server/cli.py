"""Command-line entry point for the Halo Server daemon and admin client."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import signal
from pathlib import Path

from halo_server import __version__
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
        "--plugin-host",
        type=_loopback_host,
        default="127.0.0.1",
        help="Plugin API loopback bind address",
    )
    daemon.add_argument(
        "--plugin-port",
        type=_tcp_port,
        default=18443,
        help="Plugin API TCP port",
    )
    daemon.add_argument(
        "--rtsp-host",
        type=_loopback_host,
        default="127.0.0.1",
        help="RTSP media loopback bind address",
    )
    daemon.add_argument(
        "--rtsp-port",
        type=_tcp_port,
        default=18554,
        help="RTSP media TCP port",
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
    paths = _paths_from_args(args)
    if args.command == "daemon":
        from halo_server.logging_setup import configure_logging

        configure_logging(args.log_level)
        return asyncio.run(
            _run_daemon(
                paths,
                plugin_host=args.plugin_host,
                plugin_port=args.plugin_port,
                rtsp_host=args.rtsp_host,
                rtsp_port=args.rtsp_port,
            )
        )
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
    return ServerPaths(state.expanduser().resolve(), runtime.expanduser().resolve())


def _loopback_host(value: str) -> str:
    if value == "localhost":
        return value
    try:
        is_loopback = ipaddress.ip_address(value).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
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


async def _run_daemon(
    paths: ServerPaths,
    *,
    plugin_host: str,
    plugin_port: int,
    rtsp_host: str,
    rtsp_port: int,
) -> int:
    from halo_server.service import HaloService

    service = HaloService(
        paths,
        plugin_host=plugin_host,
        plugin_port=plugin_port,
        rtsp_host=rtsp_host,
        rtsp_port=rtsp_port,
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
