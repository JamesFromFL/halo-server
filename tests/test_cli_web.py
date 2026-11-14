"""Local web bootstrap commands never send secrets through daemon RPC."""

import json
from types import SimpleNamespace

import pytest

from halo_server import cli


@pytest.fixture
def web_commands(monkeypatch, tmp_path):
    import halo_server.web as web

    calls = []
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime-home"))

    def configure(paths, **values):
        calls.append(("configure", paths, values))
        return {"url": "https://127.0.0.1:18444", "fingerprint": "fixture"}

    def setup(paths, *, reset):
        calls.append(("setup", paths, reset))
        return {"setup_code": "fixture-enrollment-code", "expires_at": 600}

    def certificate(paths, cert, key):
        calls.append(("certificate", paths, cert, key))
        return {"fingerprint": "imported-fixture"}

    async def serve(paths):
        calls.append(("serve", paths))

    monkeypatch.setattr(web, "auth", SimpleNamespace(issue_setup=setup), raising=False)
    monkeypatch.setattr(
        web,
        "settings",
        SimpleNamespace(
            configure=configure,
            access_info=lambda paths: {"configured": True},
            import_certificate=certificate,
        ),
        raising=False,
    )
    monkeypatch.setattr(web, "server", SimpleNamespace(serve=serve), raising=False)
    return calls


def test_web_local_configuration_is_explicit(web_commands, tmp_path, capsys):
    state, runtime = tmp_path / "state", tmp_path / "runtime"
    assert (
        cli.main(
            [
                "web",
                "--state-directory",
                str(state),
                "--runtime-directory",
                str(runtime),
                "configure",
            ]
        )
        == 0
    )
    name, paths, arguments = web_commands[-1]
    assert name == "configure" and paths.state_directory == state
    assert arguments == {
        "host": "127.0.0.1",
        "port": 18444,
        "canonical_host": None,
        "allow_lan": False,
        "renew_certificate": False,
    }
    assert "https://127.0.0.1:18444" in capsys.readouterr().out


def test_web_recovery_requires_explicit_confirmation(web_commands, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["web", "reset"])
    assert error.value.code == 2 and not web_commands
    assert cli.main(["web", "reset", "--yes"]) == 0
    assert web_commands[-1][0] == "setup" and web_commands[-1][2] is True
    assert json.loads(capsys.readouterr().out)["setup_code"] == "fixture-enrollment-code"


def test_web_setup_and_foreground_server(web_commands):
    assert cli.main(["web", "setup"]) == 0
    assert web_commands[-1][2] is False
    assert cli.main(["web"]) == 0
    assert web_commands[-1][0] == "serve"


def test_web_import_only_accepts_explicit_local_files(web_commands, tmp_path):
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    assert (
        cli.main(
            ["web", "certificate", "--certificate", str(certificate), "--private-key", str(key)]
        )
        == 0
    )
    assert web_commands[-1][2:] == (certificate, key)


@pytest.mark.parametrize("command", [["configure"], ["setup"], ["reset", "--yes"]])
def test_local_web_writes_refuse_release_activation_gate(web_commands, tmp_path, command):
    from halo_server.release_guard import ReleaseGuard

    state = tmp_path / "state"
    gate = ReleaseGuard.acquire(state, exclusive=True, nonblocking=True, create=True)
    try:
        assert cli.main(["web", "--state-directory", str(state), *command]) == 1
        assert not web_commands
    finally:
        gate.close()


@pytest.mark.parametrize("fail", [False, True])
def test_local_web_write_holds_and_releases_source_gate(web_commands, tmp_path, monkeypatch, fail):
    from halo_server import web
    from halo_server.release_guard import ReleaseGuard, ReleaseGuardError

    state = tmp_path / "state"
    called = False

    def write(paths, **kwargs):
        nonlocal called
        called = True
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(paths.state_directory, exclusive=True, nonblocking=True)
        if fail:
            raise RuntimeError("fixture write failed")
        return {"configured": True}

    monkeypatch.setattr(web.settings, "configure", write)
    assert cli.main(["web", "--state-directory", str(state), "configure"]) == int(fail)
    assert called
    ReleaseGuard.acquire(state, exclusive=True, nonblocking=True).close()
