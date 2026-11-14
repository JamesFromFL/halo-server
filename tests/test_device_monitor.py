"""Composed daemon ownership and read-only policy under account changes."""

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_native_service import service_fixture

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.models import DeviceIdentity, DeviceObservation, DeviceRecord
from halo_server.devices.monitor import DeviceMonitor
from halo_server.devices.policy import DevicePolicy, DevicePolicyError
from halo_server.owner_state import MAX_OWNER_STATE_BYTES


class Provider:
    instances = None

    def __init__(self, auth, **options):
        self.options = options
        self.started = False
        self.closed = False
        self.instances.append(self)

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    def publish(self, records=(), *, revision=1, complete=True, at=100):
        self.options["on_snapshot"](
            records=records, revision=revision, complete=complete, observed_at=at
        )


def fixture(tmp_path):
    service, client, sessions = service_fixture(tmp_path)
    client.ring = SimpleNamespace(auth=object())
    client.resources = set()
    client.register_session_resource = client.resources.add
    client.unregister_session_resource = client.resources.discard
    providers = []

    class Factory(Provider):
        instances = providers

    monitor = DeviceMonitor(service, provider_factory=Factory, clock=lambda: 100)
    service._device_monitor = monitor
    return service, client, sessions, monitor, providers


def preference(sessions, *, enabled=True, revision=0):
    return dict(
        profile_id=sessions.active_scope.profile_id,
        account_generation=sessions.active_scope.generation,
        expected_revision=revision,
        enabled=enabled,
    )


def record(sessions, *, name="Window", generation=1, identifier="sensor"):
    return DeviceRecord(
        DeviceIdentity(
            sessions.active_scope.profile_id, "ring-hub", "location", "asset", identifier
        ),
        sessions.active_scope.generation,
        generation,
        name,
        "hub",
        "contact-sensor",
        capabilities=("contact",),
        capability_evidence=("ring-schema",),
        observations=(DeviceObservation("contact_open", True, 100),),
        source_health="online",
        supported=True,
    )


@pytest.mark.asyncio
async def test_default_inert_then_monitor_without_camera_readiness(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    state = await service.handle_admin("owner_state", {})
    assert state["noncamera"]["enabled"] is False
    assert not service.paths.state_directory.exists()
    await monitor.reconcile()
    assert not providers and client.submissions == 0
    service._account_integrations_ready = False
    service._media_scope = None
    result = await service.handle_admin("devices_monitoring_set", preference(sessions))
    assert result["state"] == "starting" and result["policy_revision"] == 1
    provider = providers[0]
    assert provider.started and provider in client.resources
    provider.publish((record(sessions),))
    await asyncio.sleep(0)
    state = await service.handle_admin("owner_state", {})
    assert state["noncamera"]["state"] == "ready"
    assert state["noncamera"]["devices"][0]["observed"]["contact_open"] is True
    assert not state["devices"]
    await monitor.close()
    assert provider.closed and not client.resources


@pytest.mark.asyncio
async def test_disable_persists_and_rejects_late_callbacks(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    provider = providers[0]
    provider.publish((record(sessions),))
    await asyncio.sleep(0)
    await monitor.set_enabled(**preference(sessions, enabled=False, revision=1))
    provider.publish((record(sessions),), revision=2)
    await asyncio.sleep(0)
    assert monitor.snapshot()["devices"] == []
    assert monitor.snapshot()["state"] == "disabled"
    assert provider.closed and not provider.options["admission"]()
    assert monitor.store.load() == DevicePolicy(revision=2, enabled=False)
    restarted = DeviceMonitor(service, provider_factory=lambda *_a, **_k: pytest.fail("disabled"))
    await restarted.reconcile()
    assert restarted.snapshot()["enabled"] is False
    assert restarted.snapshot()["policy_revision"] == 2


@pytest.mark.asyncio
async def test_account_and_empty_inventory_callbacks_fenced(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    old = providers[0]
    sessions.active_scope = AccountScope(str(uuid4()), 2)
    assert monitor.snapshot()["devices"] == []
    assert not old.options["admission"]()
    await monitor.reconcile()
    assert old.closed
    current = providers[1]
    current.publish((record(sessions, generation=2),))
    await asyncio.sleep(0)
    old.publish((), revision=99)
    await asyncio.sleep(0)
    assert len(monitor.snapshot()["devices"]) == 1
    assert monitor.snapshot()["revision"] == 1
    await monitor.close()


@pytest.mark.asyncio
async def test_policy_revision_scope_and_dangerous_parameters_rejected(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    for params in [
        preference(sessions) | {"panic_enabled": True},
        preference(sessions) | {"enabled": "true"},
        preference(sessions) | {"account_generation": True},
        preference(sessions) | {"profile_id": str(uuid4())},
    ]:
        with pytest.raises(AdminProtocolError):
            await service.handle_admin("devices_monitoring_set", params)
    assert not providers and client.submissions == 0
    await monitor.set_enabled(**preference(sessions))
    with pytest.raises(AdminProtocolError) as error:
        await monitor.set_enabled(**preference(sessions, enabled=False))
    assert error.value.code == "configuration_conflict"
    assert monitor.policy.enabled
    await monitor.close()


@pytest.mark.asyncio
async def test_expired_and_cropped_owner_projection_is_truthful_and_bounded(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    rows = tuple(
        replace(
            record(sessions, identifier=str(i)),
            name="\ufffd" * 256,
            model="\ufffd" * 128,
            location_name="\ufffd" * 256,
        )
        for i in range(300)
    )
    providers[0].publish(rows)
    await asyncio.sleep(0)
    state = await service.handle_admin("owner_state", {})
    assert state["truncated"] and state["noncamera"]["truncated"]
    assert not state["noncamera"]["complete"]
    assert (
        len(json.dumps(state, ensure_ascii=True, separators=(",", ":")).encode())
        <= MAX_OWNER_STATE_BYTES
    )
    monitor._clock = lambda: 500
    view = monitor.snapshot()
    assert view["state"] == "partial" and not view["complete"]
    assert view["devices"][0]["observed"]["contact_open"] is None
    assert view["devices"][0]["observation_details"]["contact_open"]["last_known_value"] is True
    await monitor.close()


@pytest.mark.asyncio
async def test_bad_policy_does_not_start_provider_or_change_cameras(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    monitor.store.path.parent.mkdir(parents=True, mode=0o700)
    monitor.store.path.write_text('{"version":1,"revision":0,"enabled":true,"panic_enabled":true}')
    monitor.store.path.chmod(0o600)
    await monitor.reconcile()
    assert monitor.snapshot()["state"] == "error" and not providers
    assert client.is_authenticated and len(client.all_devices) == 1


@pytest.mark.asyncio
async def test_conflict_reloads_disabled_policy_and_retires_owned_provider(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    monitor.store.set_enabled(False, expected_revision=1)
    with pytest.raises(AdminProtocolError) as error:
        await monitor.set_enabled(**preference(sessions, revision=1))
    assert error.value.code == "configuration_conflict"
    assert providers[0].closed and not client.resources
    assert not monitor.snapshot()["enabled"]


@pytest.mark.asyncio
async def test_owner_disconnect_during_disable_still_joins_commit_and_provider_close(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    entered, release = threading.Event(), threading.Event()
    save = monitor.store.set_enabled

    def blocked_save(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return save(*args, **kwargs)

    monitor.store.set_enabled = blocked_save
    request = asyncio.create_task(
        service.handle_admin(
            "devices_monitoring_set", preference(sessions, enabled=False, revision=1)
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    finally:
        release.set()
        await asyncio.gather(*service._owner_tasks)
    assert monitor.store.load() == DevicePolicy(revision=2, enabled=False)
    assert providers[0].closed and not client.resources


@pytest.mark.asyncio
async def test_uncertain_disable_save_reloads_before_any_provider_readmission(tmp_path):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    await monitor.set_enabled(**preference(sessions))
    save = monitor.store.set_enabled

    def uncertain_save(*args, **kwargs):
        save(*args, **kwargs)
        raise DevicePolicyError("Fixture directory sync failed", possibly_saved=True)

    monitor.store.set_enabled = uncertain_save
    with pytest.raises(AdminProtocolError) as error:
        await monitor.set_enabled(**preference(sessions, enabled=False, revision=1))
    assert error.value.code == "device_policy_unavailable"
    assert providers[0].closed and not client.resources
    await monitor.reconcile()
    assert len(providers) == 1 and not monitor.policy.enabled
    assert monitor.snapshot()["state"] == "disabled"
