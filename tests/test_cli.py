import pytest

from halo_server.cli import _paths_from_args, build_parser, main


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
