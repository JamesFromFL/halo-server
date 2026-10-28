"""Owner CLI uses exact observed scope/CAS and never silently retries effects."""

import json

import pytest
from test_tui_owner import OwnerClient

from halo_server import cli
from halo_server.tui.client import AdminClientError


@pytest.fixture
def client(monkeypatch):
    value = OwnerClient()
    monkeypatch.setattr("halo_server.tui.client.AdminClient", lambda path: value)
    return value


def test_CLI_explicit_selection_and_warm(client, capsys):
    assert cli.main(["homekit", "select", "42"]) == 0
    assert ("select", client.view["profile_id"], 2, ["42"], 3) in client.calls
    assert cli.main(["homekit", "warm", "42", "on"]) == 0
    assert next(call for call in client.calls if call[0] == "warm")[-1] is True
    assert "123-45-678" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["homekit", "select"],
        ["homekit", "select", "--none", "42"],
        ["homekit", "pairing", "reset", "42"],
    ],
)
def test_CLI_refuses_ambiguous_selection_or_unconfirmed_reset(client, arguments):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2 and not client.calls


def test_CLI_reveal_is_explicit_and_reset_is_targeted(client, capsys):
    assert cli.main(["homekit", "pairing", "list"]) == 0
    assert "123-45-678" not in capsys.readouterr().out
    assert cli.main(["homekit", "pairing", "reveal", "42"]) == 0
    assert json.loads(capsys.readouterr().out)["pin"] == "123-45-678"
    assert cli.main(["homekit", "pairing", "reset", "42", "--yes"]) == 0
    assert next(call for call in client.calls if call[0] == "reset")[1:3] == ("42", 3)


def test_CLI_configuration_save_keeps_applied_mode_and_uses_saved_revision(client, capsys):
    assert cli.main(["config", "set", "--mode", "dual", "--plugin-port", "20000"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["applied"]["mode"] == "compatibility" and output["restart_required"] is True
    assert client.calls[-1][0:2] == ("configuration", 4)
    assert output["saved"]["mode"] == "dual" and output["saved"]["plugin_port"] == 20000


def test_CLI_conflict_never_retries_or_overwrites(client, capsys):
    assert cli.main(["config", "set", "--mode", "dual", "--expected-revision", "0"]) == 1
    assert len(client.calls) == 1 and client.configuration["saved"]["mode"] == "homekit"
    assert "reload" in capsys.readouterr().err


def test_CLI_control_unknown_is_reported_once(client, capsys):
    assert cli.main(["camera-control", "42", "light", "on"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    assert len([call for call in client.calls if call[0] == "control"]) == 1


def test_CLI_support_creates_requested_private_report(client, tmp_path, capsys):
    output = tmp_path / "support.json"
    assert cli.main(["support", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["kind"] == "support"
    assert cli.main(["support", "--output", str(output)]) == 1
    assert "123-45-678" not in capsys.readouterr().out


def test_CLI_doctor_falls_back_to_static_only_when_daemon_unavailable(client, monkeypatch, capsys):
    calls = []

    async def unavailable(**kwargs):
        raise AdminClientError("daemon_unavailable", "Unavailable")

    async def static(paths, **kwargs):
        calls.append(kwargs)
        return {"kind": "local", "probe": kwargs["probe"], "status": "ready"}

    client.doctor = unavailable
    monkeypatch.setattr("halo_server.diagnostics.local_doctor", static)
    assert cli.main(["doctor", "--probe"]) == 0
    assert calls == [{"probe": True}] and json.loads(capsys.readouterr().out)["kind"] == "local"


def test_CLI_pending_reset_resumes_the_original_operation(client):
    operation = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    client.pairing["cameras"][0].update(pending_reset=True, reset_operation_id=operation)
    assert cli.main(["homekit", "pairing", "reset", "42", "--yes"]) == 0
    assert next(call for call in client.calls if call[0] == "reset")[-1] == operation
