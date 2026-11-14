"""Explicit offline release operator, separate from daemon administration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

from halo_server.paths import ServerPaths
from halo_server.release_activation import ActivationError, ReleaseActivator, ReleaseReference
from halo_server.release_service import render_user_unit, render_web_unit


def parser():
    result = argparse.ArgumentParser(
        prog="halo-release",
        description="Verify and activate immutable code without restoring mutable state",
    )
    result.add_argument("--prefix", type=Path)
    result.add_argument("--state-directory", type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    activate = commands.add_parser("activate", help="Activate an already staged offline release")
    activate.add_argument(
        "--install-unit",
        action="store_true",
        help="Install the exact user unit and reload it; refuse an existing different unit",
    )
    activate.add_argument("--release-id", required=True)
    activate.add_argument(
        "--install-web-unit",
        action="store_true",
        help="Install the exact web companion unit too; do not enable or start inactive services",
    )
    activate.add_argument(
        "--initial-mode",
        choices=("homekit",),
        help="Initialize homekit mode only when configuration is absent; preserve saved settings",
    )
    activate.add_argument("--manifest-sha256", required=True)
    activate.add_argument("--expected-revision", required=True, type=_revision)
    rollback = commands.add_parser(
        "rollback", help="Activate previous code only if it can read current mutable state"
    )
    rollback.add_argument("--expected-revision", required=True, type=_revision)
    uninstall = commands.add_parser(
        "uninstall", help="Disable user unit and remove selection; preserve state and release files"
    )
    uninstall.add_argument("--expected-revision", required=True, type=_revision)
    commands.add_parser("unit", help="Print the single user unit; do not install or start it")
    commands.add_parser(
        "web-unit", help="Print the independent web unit; do not install or start it"
    )
    return result


def _revision(value):
    if (
        not value.isascii()
        or not value.isdecimal()
        or len(value) > 16
        or not 0 <= int(value) < 2**53
    ):
        raise argparse.ArgumentTypeError("revision must be a nonnegative safe integer")
    return int(value)


def _prefix(value):
    if value is None:
        root = os.environ.get("HALO_RELEASE_ROOT")
        if root is None:
            raise ActivationError("An absolute install prefix is required", code="bad_parameters")
        release = Path(root)
        if release.parent.name != "releases":
            raise ActivationError("Managed release prefix is invalid", code="bad_parameters")
        value = release.parent.parent
    if not value.is_absolute():
        raise ActivationError("An absolute install prefix is required", code="bad_parameters")
    return value


def _drop_operator_guard(paths):
    from halo_server.release_guard import (
        GUARD_FD_ENV,
        GUARD_STATE_ENV,
        inherited_guard_fds,
    )

    if os.environ.get(GUARD_FD_ENV) is None and os.environ.get(GUARD_STATE_ENV) is None:
        return
    # A daemon must never drop its lifetime lease through an admin request.
    # Only the dedicated verified bootstrap may enter this path, before any
    # event loop, service, thread or child has started.
    if os.environ.get("HALO_RELEASE_OPERATOR") != "1" or threading.active_count() != 1:
        raise ActivationError(
            "Release operation requires the separate operator entry", code="unsafe_operator"
        )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ActivationError(
            "Release operation cannot run inside a daemon", code="unsafe_operator"
        )
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    if not children.is_file() or children.read_text().strip():
        raise ActivationError("Release operator already owns children", code="unsafe_operator")
    descriptors = inherited_guard_fds(state_directory=paths.state_directory)
    for descriptor in descriptors:
        os.close(descriptor)
    os.environ.pop(GUARD_FD_ENV, None)
    os.environ.pop(GUARD_STATE_ENV, None)


async def _run(arguments, paths, *, factory=ReleaseActivator, service=None):
    if service is None:
        from halo_server.release_services import ManagedServicesController
        from halo_server.tui.client import AdminClient

        service = ManagedServicesController(
            AdminClient(paths.admin_socket),
            AdminClient(paths.runtime_directory / "web-admin.sock"),
            _prefix(arguments.prefix),
            paths,
            install_web=getattr(arguments, "install_web_unit", False),
        )
    operation = factory(
        _prefix(arguments.prefix),
        paths.state_directory,
        service,
        prepare_unit=getattr(arguments, "install_unit", False)
        or getattr(arguments, "install_web_unit", False),
        initial_mode=getattr(arguments, "initial_mode", None),
    )
    if arguments.command == "activate":
        result = await operation.activate(
            ReleaseReference(arguments.release_id, arguments.manifest_sha256),
            expected_revision=arguments.expected_revision,
        )
        return result.as_dict()
    if arguments.command == "rollback":
        return (await operation.rollback(expected_revision=arguments.expected_revision)).as_dict()
    return await operation.uninstall(expected_revision=arguments.expected_revision)


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        prefix = _prefix(arguments.prefix)
        arguments.prefix = prefix
        paths = ServerPaths.from_environment()
        if arguments.state_directory is not None:
            if not arguments.state_directory.is_absolute():
                raise ActivationError("State directory must be absolute", code="bad_parameters")
            paths = ServerPaths(arguments.state_directory, paths.runtime_directory)
        if arguments.command in {"unit", "web-unit"}:
            render = render_user_unit if arguments.command == "unit" else render_web_unit
            sys.stdout.write(render(prefix, state_directory=paths.state_directory))
            return 0
        _drop_operator_guard(paths)
        result = asyncio.run(_run(arguments, paths))
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0 if result.get("status") in {"active", "uninstalled"} else 1
    except ActivationError as error:
        print(
            json.dumps(
                {"status": "failed", "code": error.code, "possibly_saved": error.possibly_saved},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError, ValueError):
        print('{"status":"failed","code":"unavailable","possibly_saved":false}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
