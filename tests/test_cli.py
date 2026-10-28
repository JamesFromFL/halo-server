import asyncio
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from halo_server import cli
from halo_server.cli import _paths_from_args, build_parser, main
from halo_server.configuration import ConfigurationStore, ServerConfiguration, ServerMode
from halo_server.paths import ServerPaths


def test_cli_exposes_daemon_tui_status_and_shutdown_commands():
    parser = build_parser()

    assert parser.parse_args(["daemon"]).command == "daemon"
    assert parser.parse_args(["tui"]).command == "tui"
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["shutdown"]).command == "shutdown"


def test_cli_path_overrides_are_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(
        [
            "daemon",
            "--state-directory",
            "state",
            "--runtime-directory",
            "runtime",
        ]
    )

    paths = _paths_from_args(args)

    assert paths.state_directory == tmp_path / "state"
    assert paths.runtime_directory == tmp_path / "runtime"


def test_cli_without_command_prints_help(capsys):
    assert main([]) == 0
    assert "daemon" in capsys.readouterr().out


def test_cli_rejects_unsupported_native_platform(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "win32")

    with pytest.raises(SystemExit) as raised:
        main(["status"])

    assert raised.value.code == 2
    assert "native Windows is not supported" in capsys.readouterr().err


def test_cli_allows_macos_native_runtime(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")

    cli._require_native_platform(build_parser())


@pytest.mark.parametrize(
    "arguments",
    [
        ["daemon", "--plugin-host", "0.0.0.0"],
        ["daemon", "--plugin-host", "server.example.com"],
        ["daemon", "--plugin-port", "-1"],
        ["daemon", "--plugin-port", "65536"],
        ["daemon", "--rtsp-host", "0.0.0.0"],
        ["daemon", "--rtsp-port", "65536"],
    ],
)
def test_cli_rejects_unsafe_plugin_bind_arguments(arguments):
    with pytest.raises(SystemExit):
        build_parser().parse_args(arguments)


def test_daemon_optional_overrides_do_not_hide_persisted_configuration():
    args = build_parser().parse_args(["daemon"])
    assert all(
        getattr(args, name) is None
        for name in ("mode", "plugin_host", "plugin_port", "rtsp_host", "rtsp_port")
    )


def test_saved_configuration_precedes_defaults_and_explicit_arguments_override_without_save(
    tmp_path,
):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    store = ConfigurationStore(paths.configuration_file)
    saved = store.save(
        ServerConfiguration(
            mode=ServerMode.HOMEKIT,
            plugin_host="localhost",
            plugin_port=19000,
            rtsp_host="::1",
            rtsp_port=19001,
        )
    )
    original = paths.configuration_file.read_bytes()
    assert cli._configuration_from_args(paths, build_parser().parse_args(["daemon"])) == saved
    args = build_parser().parse_args(
        [
            "daemon",
            "--mode",
            "ring-only",
            "--plugin-host",
            "127.0.0.2",
            "--plugin-port",
            "0",
        ]
    )
    effective = cli._configuration_from_args(paths, args)
    assert effective == replace(
        saved, mode=ServerMode.RING_ONLY, plugin_host="127.0.0.2", plugin_port=0
    )
    assert paths.configuration_file.read_bytes() == original


def test_missing_configuration_keeps_legacy_defaults(tmp_path):
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    effective = cli._configuration_from_args(paths, build_parser().parse_args(["daemon"]))
    assert effective == ServerConfiguration()
    assert effective.compatibility_enabled
    assert not paths.configuration_file.exists()


def test_corrupt_configuration_startup_is_visible_without_traceback_or_raw_content(
    tmp_path, capsys
):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    path = state / "configuration.json"
    path.write_text("corrupt-private-secret-content")
    path.chmod(0o600)
    assert (
        main(
            [
                "daemon",
                "--state-directory",
                str(state),
                "--runtime-directory",
                str(tmp_path / "runtime"),
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert "configuration" in error and "JSON" in error
    assert "Traceback" not in error
    assert "private-secret" not in error


def test_status_remains_available_without_loading_configuration(tmp_path, monkeypatch):
    calls = []

    async def admin_command(paths, command):
        calls.append((paths, command))
        return 0

    def no_configuration(_self):
        raise AssertionError("admin-only command loaded daemon configuration")

    monkeypatch.setattr(ConfigurationStore, "load", no_configuration)
    monkeypatch.setattr(cli, "_run_admin_command", admin_command)
    assert (
        main(
            [
                "status",
                "--state-directory",
                str(tmp_path / "state"),
                "--runtime-directory",
                str(tmp_path / "runtime"),
            ]
        )
        == 0
    )
    assert calls[0][1] == "status"


def test_path_arguments_do_not_resolve_and_follow_parent_symlinks(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    args = build_parser().parse_args(["daemon", "--state-directory", str(alias)])
    assert _paths_from_args(args).state_directory == alias
    assert main(["daemon", "--state-directory", str(alias)]) == 1
    assert "unsafe" in capsys.readouterr().err
    assert not list(real.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(ServerMode))
async def test_effective_mode_controls_listener_admission_and_is_forwarded(
    tmp_path, monkeypatch, mode
):
    calls = []

    class FakeService:
        def __init__(self, paths, **kwargs):
            calls.append((paths, kwargs))

        def request_stop(self):
            pass

        async def start(self):
            calls.append("start")

        async def wait_for_stop(self):
            calls.append("wait")

        async def stop(self):
            calls.append("stop")

    monkeypatch.setitem(
        sys.modules, "halo_server.service", SimpleNamespace(HaloService=FakeService)
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    config = ServerConfiguration(mode=mode, plugin_port=0, rtsp_port=0)
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    assert await cli._run_daemon(paths, configuration=config) == 0
    forwarded = calls[0][1]
    assert forwarded["configuration"] is config
    expected_host = "127.0.0.1" if config.compatibility_enabled else None
    assert forwarded["plugin_host"] == expected_host
    assert forwarded["rtsp_host"] == expected_host
    assert forwarded["plugin_port"] == forwarded["rtsp_port"] == 0
    assert calls[1:] == ["start", "wait", "stop"]


def test_main_explicit_homekit_override_is_not_saved(tmp_path, monkeypatch):
    calls = []

    async def daemon(paths, *, configuration):
        calls.append((paths, configuration))
        return 0

    monkeypatch.setattr(cli, "_run_daemon", daemon)
    assert (
        main(
            [
                "daemon",
                "--mode",
                "homekit",
                "--state-directory",
                str(tmp_path / "state"),
                "--runtime-directory",
                str(tmp_path / "runtime"),
            ]
        )
        == 0
    )
    paths, effective = calls[0]
    assert effective.mode is ServerMode.HOMEKIT
    assert not effective.compatibility_enabled
    assert not paths.configuration_file.exists()
