"""Typed owner requests over real private IPC and bounded coherent discovery."""

import copy
import json
import os
import stat
from uuid import uuid4

import pytest
from test_native_service import service_fixture
from test_tui_owner import OwnerClient

from halo_server.api.admin import AdminServer
from halo_server.tui.client import AdminClient, AdminClientError
from halo_server.tui.support import MAX_SUPPORT_BYTES, write_support_report


@pytest.mark.asyncio
async def test_owner_client_roundtrip(tmp_path):
    fixture, calls = OwnerClient(), []

    async def handler(method, params):
        calls.append((method, params))
        if method == "configuration_get":
            return fixture.configuration
        if method == "configuration_set":
            return fixture.configuration
        if method == "homekit_list":
            return fixture.view
        if method == "homekit_pairing_list":
            return fixture.pairing
        if method == "homekit_pairing_reveal":
            return {
                name: value for name, value in params.items() if not name.startswith("expected_")
            } | {
                "registry_revision": params["expected_registry_revision"],
                "pairing_revision": params["expected_pairing_revision"],
                "pin": "123-45-678",
                "setup_uri": "X-HM://012345678ABCD",
                "expires_in_ms": 60000,
            }
        if method == "camera_control_list":
            return fixture.controls
        if method == "camera_control":
            return {"status": "unknown", "code": "timeout"}
        return {"saved": True}

    server = AdminServer(tmp_path / "a.sock", handler)
    await server.start()
    try:
        client = AdminClient(server.socket_path)
        configuration = await client.configuration_get()
        await client.configuration_set(4, configuration["saved"])
        assert set(calls[-1][1]["configuration"]) == {
            "mode",
            "plugin_host",
            "plugin_port",
            "rtsp_host",
            "rtsp_port",
        }
        assert calls[-1][1]["expected_revision"] == 4
        cameras = await client.homekit_cameras()
        await client.homekit_select(
            cameras["profile_id"], cameras["account_generation"], ["42"], cameras["revision"]
        )
        assert calls[-1][1]["expected_revision"] == 3
        page = await client.homekit_pairing_list()
        assert "pin" not in str(page) and "setup_uri" not in str(page)
        secret = await client.homekit_pairing_reveal(page, page["cameras"][0])
        assert secret["pin"] == "123-45-678"
        assert calls[-1][1]["expected_pairing_revision"] == "a" * 64
        controls = await client.camera_controls()
        result = await client.camera_control(
            controls["profile_id"],
            controls["account_generation"],
            "42",
            7,
            str(uuid4()),
            "light",
            True,
        )
        assert result["status"] == "unknown"
        assert len([method for method, _ in calls if method == "camera_control"]) == 1
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_owner_client_real_service(tmp_path):
    service, ring, _ = service_fixture(tmp_path)
    server = AdminServer(tmp_path / "a.sock", service.handle_admin)
    await server.start()
    try:
        client = AdminClient(server.socket_path)
        current = await client.configuration_get()
        saved = await client.configuration_set(
            current["saved"]["revision"], current["saved"] | {"mode": "ring-only"}
        )
        assert saved["saved"]["mode"] == "ring-only"
        assert saved["applied"]["mode"] == "homekit" and saved["restart_required"]
        page = await client.camera_controls()
        camera = page["cameras"][0]
        assert camera["has_light"] and not camera["has_siren"]
        result = await client.camera_control(
            page["profile_id"],
            page["account_generation"],
            camera["camera_id"],
            camera["device_generation"],
            str(uuid4()),
            "light",
            True,
        )
        assert result == {"status": "accepted", "code": "ok"}
        assert ring.all_devices[0].light_values == [True] and ring.submissions == 1
        diagnostics = await client.doctor()
        assert diagnostics["schema"] == 1
        report = await client.support_report()
        assert "account-display-only" not in json.dumps(report)
    finally:
        await server.stop()


class Pages(AdminClient):
    def __init__(self, *, changed=None):
        super().__init__(None)
        self.fixture = OwnerClient()
        self.changed = changed
        self.calls = 0

    async def request(self, method, **params):
        self.calls += 1
        page = copy.deepcopy(self.fixture.view)
        page["total"] = 33
        page["offset"] = params["offset"]
        page["cameras"] = [
            {"camera_id": str(i)} for i in range(params["offset"], min(params["offset"] + 32, 33))
        ]
        if self.calls == 2 and self.changed is not None:
            page[self.changed] = (
                "new-profile" if self.changed == "profile_id" else page[self.changed] + 1
            )
        return page


@pytest.mark.asyncio
async def test_owner_camera_pages_are_bounded_and_complete():
    client = Pages()
    page = await client.homekit_cameras()
    assert len(page["cameras"]) == 33 and client.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["profile_id", "account_generation", "revision"])
async def test_owner_camera_pages_never_merge_changed_authority(changed):
    client = Pages(changed=changed)
    with pytest.raises(AdminClientError) as error:
        await client.homekit_cameras()
    assert error.value.code == "configuration_conflict" and client.calls == 2


@pytest.mark.asyncio
async def test_owner_setup_reply_wrong_identity_is_rejected_without_secret_in_error():
    fixture = OwnerClient()
    client = AdminClient(None)

    async def invalid(method, **params):
        return {
            "profile_id": "wrong",
            "pin": "123-45-678",
            "setup_uri": "X-HM://012345678ABCD",
            "expires_in_ms": 60000,
        }

    client.request = invalid
    with pytest.raises(AdminClientError) as error:
        await client.homekit_pairing_reveal(fixture.pairing, fixture.pairing["cameras"][0])
    assert "123-45-678" not in str(error.value) and error.value.code == "bad_response"


@pytest.mark.parametrize("mask", [0, 0o777])
def test_support_export_is_exclusive_private_even_with_unusual_umask(tmp_path, mask):
    path = tmp_path / "support.json"
    old = os.umask(mask)
    try:
        write_support_report(path, {"safe": True})
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_support_report(path, {"replaced": True})
    assert "replaced" not in path.read_text()


def test_support_export_refuses_links_and_oversized_or_nonfinite_report(tmp_path):
    target = tmp_path / "original"
    target.write_text("unchanged")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        write_support_report(link, {"safe": True})
    for report in ({"large": "x" * MAX_SUPPORT_BYTES}, {"invalid": float("nan")}):
        with pytest.raises(ValueError):
            write_support_report(tmp_path / "new", report)
    assert target.read_text() == "unchanged" and not (tmp_path / "new").exists()
