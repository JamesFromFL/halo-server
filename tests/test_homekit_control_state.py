"""HomeKit and owner controls share fresh positive Ring observations."""

import asyncio
import json
import time
from dataclasses import asdict
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_homekit_broker import request
from test_homekit_selection import selected_bridge
from test_native_service import Camera, service_fixture

from halo_server.api.admin import AdminProtocolError
from halo_server.homekit.controls import CONTROL_STATE_TTL, observed_controls
from halo_server.homekit.ipc import validate_reply
from halo_server.ring.lighting import client_lighting


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
    service._remember_control_observations(
        client, refreshed_at=time.monotonic(), lighting_revision=client_lighting(client).revision
    )
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
    service._remember_control_observations(
        client, refreshed_at=time.monotonic(), lighting_revision=client_lighting(client).revision
    )
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
    # An admitted lighting mutation invalidates the earlier read. It does not
    # fabricate the requested False or retain an observation known to predate it.
    assert (await service.handle_admin("camera_control_list", {}))["cameras"][0]["light"] is None
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
    client._last_device_update_receipt = (client._last_device_update_at, 0)
    service._remember_control_observations(client)
    assert (await service.handle_admin("camera_control_list", {}))["cameras"][0]["light"] is None
    client._last_device_update_at = time.monotonic()
    client._last_device_update_receipt = (client._last_device_update_at, 0)
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


@pytest.mark.parametrize("explicit_read_revision", [False, True])
def test_mutation_during_observation_copy_cannot_relabel_old_light_state(
    tmp_path, monkeypatch, explicit_read_revision
):
    import halo_server.service as service_module

    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.capabilities.add("siren")
    camera.lights = "on"
    camera._attrs = {"siren_status": {"seconds_remaining": 0}}
    reservation = client_lighting(client)
    read_revision = reservation.revision
    client._last_device_update_receipt = (time.monotonic(), read_revision)
    original = service_module.observed_controls

    def crossed_copy(device):
        result = original(device)
        token = reservation.try_acquire()
        assert token is not None and reservation.mark_sent(token)
        reservation.release(token)
        return result

    monkeypatch.setattr(service_module, "observed_controls", crossed_copy)
    kwargs = {"lighting_revision": read_revision} if explicit_read_revision else {}
    service._remember_control_observations(client, refreshed_at=time.monotonic(), **kwargs)
    assert reservation.revision == read_revision + 1
    assert service._control_lighting_revision == read_revision
    assert service._observed_camera_controls("42", camera) == (None, False)


def test_crossed_lighting_get_retains_independent_siren_observation(tmp_path):

    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.capabilities.add("siren")
    camera.lights = "off"
    camera._attrs = {"siren_status": {"seconds_remaining": 30}}
    reservation = client_lighting(client)
    read_revision = reservation.revision
    token = reservation.try_acquire()
    assert reservation.mark_sent(token)
    reservation.release(token)
    service._remember_control_observations(
        client, refreshed_at=time.monotonic(), lighting_revision=read_revision
    )
    assert service._observed_camera_controls("42", camera) == (None, True)


@pytest.mark.asyncio
async def test_cached_sdk_inventory_keeps_its_actual_pre_mutation_provenance(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.capabilities.add("siren")
    camera.lights, camera._attrs = "on", {"siren_status": {"seconds_remaining": 0}}
    reservation = client_lighting(client)
    client._last_device_update_at = time.monotonic()
    client._last_device_update_receipt = (client._last_device_update_at, reservation.revision)
    client.refresh_devices = lambda: list(client.all_devices)
    service._sync_rtsp_cameras = AsyncMock()
    service._reconcile_refreshed_media = AsyncMock()
    service.homekit.reconcile = AsyncMock()
    token = reservation.try_acquire()
    assert reservation.mark_sent(token)
    reservation.release(token)
    await service._refresh_active_inventory()
    assert service._observed_camera_controls("42", camera) == (None, False)
    # Only actual new GET evidence can reestablish the light observation.
    client._last_device_update_receipt = (time.monotonic(), reservation.revision)
    await service._refresh_active_inventory()
    assert service._observed_camera_controls("42", camera) == (True, False)


def test_client_without_explicit_get_provenance_cannot_restore_light_state(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    camera = client.all_devices[0]
    camera.capabilities.add("siren")
    camera.lights, camera._attrs = "on", {"siren_status": {"seconds_remaining": 0}}
    service._remember_control_observations(client, refreshed_at=time.monotonic())
    assert service._observed_camera_controls("42", camera) == (None, False)
