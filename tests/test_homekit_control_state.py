"""HomeKit and owner controls share fresh positive Ring observations."""

import asyncio
import json
import time
from dataclasses import asdict
from uuid import uuid4

import pytest
from test_homekit_broker import request
from test_homekit_selection import selected_bridge
from test_native_service import Camera, service_fixture

from halo_server.api.admin import AdminProtocolError
from halo_server.homekit.controls import CONTROL_STATE_TTL, observed_controls
from halo_server.homekit.ipc import validate_reply


def test_missing_or_malformed_sdk_fields_never_invent_off():
    camera = Camera(capabilities=("light", "siren"))
    camera.light, camera.siren = False, 0  # SDK defaults are not evidence.
    assert observed_controls(camera) == (None, None)
    camera.lights = "off"
    camera._attrs = {"siren_status": {"seconds_remaining": 0}}
    assert observed_controls(camera) == (False, False)
    camera.lights = "on"
    camera._attrs["siren_status"]["seconds_remaining"] = 30
    assert observed_controls(camera) == (True, True)
    for malformed in (
        {},
        {"seconds_remaining": True},
        {"seconds_remaining": float("nan")},
        {"seconds_remaining": -1},
    ):
        camera._attrs["siren_status"] = malformed
        assert observed_controls(camera)[1] is None


@pytest.mark.asyncio
async def test_hap_control_state_exact_policy_and_owner_view_share_freshness(tmp_path):
    bridge, boot, scope, service, client, _ = await selected_bridge(tmp_path)
    camera = client.all_devices[0]
    camera.capabilities.add("siren")
    camera.lights, camera._attrs = "on", {"siren_status": {"seconds_remaining": 0}}
    service._remember_control_observations(client, refreshed_at=time.monotonic())
    envelope = request(
        boot, "control_state_request", {"scope": asdict(scope), "operation_id": str(uuid4())}
    )
    try:
        result = await bridge._request(envelope)
        validate_reply(envelope, envelope | {"kind": "control_state_result", "payload": result})
        assert (
            result["status"] == "accepted" and result["light"] is True and result["siren"] is False
        )
        owner = (await service.handle_admin("camera_control_list", {}))["cameras"][0]
        assert owner["light"] is True and owner["siren"] is False
        proof = service._control_observations
        service._control_observations = proof[:2] + (
            time.monotonic() - CONTROL_STATE_TTL - 1,
            proof[3],
        )
        result = await bridge._request(envelope)
        assert (
            result["status"] == "accepted" and result["light"] is None and result["siren"] is None
        )
        assert (await service.handle_admin("camera_control_list", {}))["cameras"][0][
            "light"
        ] is None
        service.homekit.fence_camera(boot, scope)
        assert (await bridge._request(envelope))["status"] == "rejected"
    finally:
        await bridge._broker.retired(boot)


@pytest.mark.asyncio
async def test_replaced_source_does_not_inherit_observed_state_and_set_is_not_optimistic(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.lights = "on"
    service._remember_control_observations(client, refreshed_at=time.monotonic())
    page = await service.handle_admin("camera_control_list", {})
    row = page["cameras"][0]
    result = await service.handle_admin(
        "camera_control",
        {
            "profile_id": page["profile_id"],
            "account_generation": page["account_generation"],
            "camera_id": row["camera_id"],
            "device_generation": row["device_generation"],
            "operation_id": str(uuid4()),
            "control": "light",
            "enabled": False,
        },
    )
    assert result["status"] == "accepted"
    assert (await service.handle_admin("camera_control_list", {}))["cameras"][0]["light"] is True
    client.all_devices[:] = [Camera()]
    assert (await service.handle_admin("camera_control_list", {}))["cameras"][0]["light"] is None


@pytest.mark.asyncio
async def test_support_is_whitelist_only_and_diagnoses_corrupt_configuration(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    service.paths.prepare()
    service.paths.configuration_file.write_text("{bad")
    service.paths.configuration_file.chmod(0o600)
    client.all_devices[0].name = "secret-personal-name"
    report = await service.handle_admin("support_report", {})
    encoded = json.dumps(report)
    for forbidden in (
        "secret-personal-name",
        "account-display-only",
        str(service.paths.state_directory),
        "username",
        "pincode",
        "setup_uri",
        "profile_id",
    ):
        assert forbidden not in encoded
    assert report["runtime"]["configuration_available"] is False
    assert report["runtime"]["saved_mode"] == "unavailable"


@pytest.mark.asyncio
async def test_listener_repair_does_not_rejuvenate_old_sdk_inventory(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    client.all_devices[0].lights = "on"
    client._last_device_update_at = time.monotonic() - CONTROL_STATE_TTL - 1
    service._remember_control_observations(client)
    assert (await service.handle_admin("camera_control_list", {}))["cameras"][0]["light"] is None
    client._last_device_update_at = time.monotonic()
    service._remember_control_observations(client)
    assert (await service.handle_admin("camera_control_list", {}))["cameras"][0]["light"] is True


@pytest.mark.asyncio
async def test_daemon_diagnostic_probe_concurrency_is_bounded(tmp_path, monkeypatch):
    import halo_server.diagnostics as diagnostics

    service, _, _ = service_fixture(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def doctor(*args, probe=False, **kwargs):
        if probe:
            entered.set()
            await release.wait()
        return {"schema": 1, "kind": "local", "probe": probe}

    monkeypatch.setattr(diagnostics, "local_doctor", doctor)
    probing = asyncio.create_task(service.handle_admin("doctor", {"probe": True}))
    try:
        await entered.wait()
        with pytest.raises(AdminProtocolError) as error:
            await service.handle_admin("doctor", {"probe": True})
        assert error.value.code == "busy"
        assert (await service.handle_admin("support_report", {}))["local"]["probe"] is False
    finally:
        release.set()
        await probing
