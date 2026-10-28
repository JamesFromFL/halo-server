"""Managed mode refusal occurs before resources or saved settings change."""

import pytest
from test_native_service import service_fixture

from halo_server.api.admin import AdminProtocolError
from halo_server.cli import _configuration_from_args, build_parser
from halo_server.configuration import ConfigurationError, ServerConfiguration, ServerMode
from halo_server.paths import ServerPaths
from halo_server.service import HaloService


@pytest.mark.parametrize("mode", list(ServerMode))
@pytest.mark.parametrize("managed", [False, True])
def test_source_modes_preserved_and_managed_gi_modes_rejected(monkeypatch, mode, managed):
    from halo_server import release_execution

    monkeypatch.setattr(release_execution, "managed_context", lambda: object() if managed else None)
    configuration = ServerConfiguration(mode=mode)
    if managed and configuration.compatibility_enabled:
        with pytest.raises(ConfigurationError, match="Managed releases support"):
            release_execution.validate_operating_mode(configuration)
    else:
        release_execution.validate_operating_mode(configuration)


def test_managed_cli_and_service_refuse_before_gi_or_saved_configuration(tmp_path, monkeypatch):
    from halo_server import release_execution

    monkeypatch.setattr(release_execution, "managed_context", lambda: object())
    paths = ServerPaths(tmp_path / "state", tmp_path / "runtime")
    arguments = build_parser().parse_args(["daemon"])
    with pytest.raises(ConfigurationError, match="Managed releases support"):
        _configuration_from_args(paths, arguments)
    with pytest.raises(ConfigurationError, match="Managed releases support"):
        HaloService(paths, rtsp_host="127.0.0.1")
    assert not paths.configuration_file.exists() and not paths.runtime_directory.exists()


@pytest.mark.asyncio
async def test_owner_cannot_save_unsupported_managed_mode(tmp_path, monkeypatch):
    from halo_server import release_execution

    service, _client, _sessions = service_fixture(tmp_path)
    initial = await service.handle_admin("configuration_get", {})
    assert not service.paths.configuration_file.exists()
    monkeypatch.setattr(release_execution, "managed_context", lambda: object())
    proposed = {
        key: value for key, value in initial["saved"].items() if key not in {"version", "revision"}
    }
    proposed["mode"] = "dual"
    with pytest.raises(AdminProtocolError) as error:
        await service.handle_admin(
            "configuration_set",
            {"expected_revision": initial["saved"]["revision"], "configuration": proposed},
        )
    assert error.value.code == "managed_mode_unsupported"
    assert not service.paths.configuration_file.exists()
    assert service.configuration.mode == ServerMode.HOMEKIT
