"""Composed bridge publication: explicit admission, stale data, isolation, and reset."""

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_device_bridge import preferences, publish, sensor
from test_device_monitor import fixture, preference

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.devices.ipc import encode_frame
from halo_server.devices.models import DeviceObservation
from halo_server.devices.publication import DeviceMaintenanceOwner
from halo_server.devices.publication_policy import (
    BridgeReset,
    PublicationConflict,
    PublicationError,
    PublicationPolicy,
    PublicationStore,
)
from halo_server.homekit.pairing_maintenance import manifest_revision

MANIFEST = {"accessory_info": "a" * 64, "identifier_cache": "b" * 64, "controller_storage": None}
REVISION = manifest_revision(MANIFEST)


class Runtime:
    instances = []
    fail_start = False

    def __init__(self, *args, **kwargs):
        self.state, self.boot_id = "starting", str(uuid4())
        self.snapshots = []
        self.invalidated = self.stopped = False
        self.instances.append(self)

    async def start(self):
        if self.fail_start:
            self.state = "failed"
            raise RuntimeError("fixture failure")
        self.state = "ready"

    def invalidate(self):
        self.invalidated = True
        self.state = "stopped"

    async def stop(self):
        self.stopped = True
        self.state = "stopped"

    async def publish(self, snapshot, *, admission):
        assert admission()
        self.snapshots.append(snapshot)
        return {"accepted": True}

    async def status(self, identity, *, admission):
        assert admission()
        return {
            **identity,
            "accepted": True,
            "published": True,
            "paired": False,
            "paired_clients": 0,
            "pairing_revision": REVISION,
            "pairing_manifest": MANIFEST,
        }

    async def reveal(self, identity, *, admission):
        assert admission()
        return {
            **identity,
            "accepted": True,
            "pincode": "031-45-154",
            "setup_uri": "X-HM://0023ISYWYHALO",
            "ttl_ms": 30000,
        }

    async def prepare_reset(self, identity, *, admission):
        assert admission()
        return {"accepted": True, "quiesced": True, "pairing_manifest": MANIFEST}


async def ready(tmp_path, *, enabled=True):
    service, client, sessions, monitor, providers = fixture(tmp_path)
    publisher = monitor.bridge.publisher
    Runtime.instances = []
    Runtime.fail_start = False
    publisher._factory = Runtime
    publisher._resources = lambda: SimpleNamespace(
        node=tmp_path / "node", entry=tmp_path / "device-main.js", build_id="test"
    )
    # Deterministic coordinator steps; production drives these on events/5s timer.
    publisher.start = publisher.notify
    await monitor.reconcile()
    await monitor.set_enabled(**preference(sessions))
    row = sensor(sessions)
    await publish(monitor, providers[0], (row,))
    await monitor.bridge.configure(**preferences(sessions))
    if enabled:
        await publisher.configure(**configuration(monitor, sessions))
        async with publisher._lock:
            await publisher._step()
    return service, client, sessions, monitor, providers, row


def configuration(monitor, sessions, **changes):
    return {
        "profile_id": sessions.active_scope.profile_id,
        "account_generation": sessions.active_scope.generation,
        "expected_registry_revision": monitor.bridge.registry.revision,
        "expected_revision": monitor.bridge.publisher.policy.revision,
        "enabled": True,
        **changes,
    }


def pairing_request(page, **changes):
    from halo_server.devices.publication import PAIRING_FIELDS

    return {key: page[key] for key in PAIRING_FIELDS} | changes


def reset_row(profile, accessory, operation=None):
    return BridgeReset(
        operation or str(uuid4()),
        profile,
        accessory,
        "02:11:22:33:44:55",
        REVISION,
        tuple(sorted(MANIFEST.items())),
    )


def test_policy_enrollment_cas_and_pending_reset_cannot_reenable(tmp_path):
    store = PublicationStore(tmp_path / "policy.json")
    assert store.load() == PublicationPolicy()
    assert not list(tmp_path.iterdir())
    profile, accessory = str(uuid4()), str(uuid4())
    policy = store.set_enabled(profile, accessory, True, expected_revision=0)
    with pytest.raises(PublicationConflict):
        store.set_enabled(profile, accessory, False, expected_revision=0)
    reset = reset_row(profile, accessory)
    policy = store.begin_reset(reset)
    assert not policy.profile(profile).enabled
    with pytest.raises(PublicationConflict):
        store.set_enabled(profile, accessory, True, expected_revision=policy.revision)
    with pytest.raises(PublicationError):
        store.advance(profile, reset.operation_id, "completed")
    with pytest.raises(PublicationError):
        store.save(replace(policy, profiles=()))
    for phase in ("quiesced", "cleanup_started", "completed"):
        policy = store.advance(profile, reset.operation_id, phase)
    assert not policy.profile(profile).enabled
    policy = store.set_enabled(profile, accessory, True, expected_revision=policy.revision)
    assert policy.profile(profile).enabled
    store.path.unlink()
    with pytest.raises(PublicationError):
        store.load()


def test_reset_manifest_and_identity_validation(tmp_path):
    profile, accessory = str(uuid4()), str(uuid4())
    for changes in (
        {"username": "01:11:22:33:44:55"},
        {"pairing_revision": "c" * 64},
        {"manifest": ()},
        {"phase": "delete"},
    ):
        with pytest.raises(PublicationError):
            replace(reset_row(profile, accessory), **changes)
    store = PublicationStore(tmp_path / "policy.json")
    store.set_enabled(profile, accessory, True, expected_revision=0)
    first = reset_row(profile, accessory)
    policy = store.begin_reset(first)
    assert store.begin_reset(first) == policy
    with pytest.raises(PublicationConflict):
        store.begin_reset(reset_row(profile, accessory))


@pytest.mark.asyncio
async def test_prepared_import_does_not_publish_without_new_opt_in(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path, enabled=False)
    publisher = monitor.bridge.publisher
    await publisher._step()
    assert not Runtime.instances
    assert not publisher.store.path.exists()
    assert monitor.snapshot()["bridge"]["publication"] == "implemented"
    await monitor.close()


@pytest.mark.asyncio
async def test_snapshot_current_unknown_missing_and_account_fence(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    runtime = publisher.runtime
    first = runtime.snapshots[-1]
    child = first["children"][0]
    assert child["values"]["contact"] is True and child["available"]
    assert child["mappings"] == ["contact"]
    assert "location" not in json.dumps(first) and "asset" not in json.dumps(first)
    monitor._clock = lambda: 500
    await publisher._step()
    stale = runtime.snapshots[-1]["children"][0]
    assert not stale["available"] and set(stale["values"].values()) == {None}
    monitor._clock = lambda: 100
    await publish(monitor, providers[0], (), revision=2)
    await publisher._step()
    missing = runtime.snapshots[-1]["children"][0]
    assert missing["accessory_uuid"] == child["accessory_uuid"]
    assert not missing["available"]
    old_camera_bridge = service._homekit_bridge
    monitor.invalidate()
    assert runtime.invalidated and not publisher.snapshot()["published"]
    await publisher._step()
    assert runtime.stopped and service._homekit_bridge is old_camera_bridge
    await monitor.close()


@pytest.mark.asyncio
async def test_pairing_reveal_scope_revision_and_publication_disable(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    page = await service.handle_admin("devices_bridge_pairing_get", {})
    assert page["available"] and page["published"] and page["paired_known"]
    assert "pairing_manifest" not in page and "pin" not in page
    secret = await service.handle_admin("devices_bridge_pairing_reveal", pairing_request(page))
    assert secret["pin"] == "031-45-154"
    with pytest.raises(AdminProtocolError):
        await publisher.reveal(**pairing_request(page, boot_id=str(uuid4())))
    with pytest.raises(AdminProtocolError):
        await publisher.reveal(**pairing_request(page, account_generation=999))
    runtime = publisher.runtime
    await publisher.configure(**configuration(monitor, sessions, enabled=False))
    assert runtime.invalidated and runtime.stopped
    assert not (await publisher.pairing())["available"]
    assert not publisher.snapshot()["publication_enabled"]
    await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_code", "owner_code"),
    [
        ("policy-denied", "pairing_not_available"),
        ("conflict", "configuration_conflict"),
        ("not-ready", "device_bridge_unavailable"),
    ],
)
async def test_bridge_reveal_rejections_preserve_pairing_and_report_actual_reason(
    tmp_path, monkeypatch, adapter_code, owner_code
):
    service, _, _, monitor, _, _ = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    page, runtime = await publisher.pairing(), publisher.runtime
    before = publisher.policy

    async def rejected_reveal(identity, *, admission):
        assert admission()
        return {
            **identity,
            "accepted": False,
            "code": adapter_code,
            "pincode": None,
            "setup_uri": None,
            "ttl_ms": 0,
        }

    monkeypatch.setattr(runtime, "reveal", rejected_reveal)
    try:
        with pytest.raises(AdminProtocolError) as error:
            await service.handle_admin("devices_bridge_pairing_reveal", pairing_request(page))
        assert error.value.code == owner_code
        assert publisher.policy == before and publisher.runtime is runtime
        assert publisher.snapshot()["published"] and not runtime.invalidated
        if adapter_code == "policy-denied":
            activity = service._owner_activity.snapshot()["entries"][-1]
            assert activity["action"] == "devices_bridge_pairing_reveal"
            assert activity["outcome"] == "rejected"
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_reset_denies_first_and_leaves_reenable_explicit(tmp_path, monkeypatch):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    page = await publisher.pairing()
    camera_pairing = service.paths.homekit_pairing_directory / "camera.txt"
    camera_pairing.parent.mkdir(parents=True, exist_ok=True)
    camera_pairing.write_text("must survive")
    calls = []

    class Owner:
        async def cleanup(self, resources, reset, *, resume):
            policy = publisher.store.load()
            assert not policy.profile(page["profile_id"]).enabled
            assert policy.profile(page["profile_id"]).reset.phase == "cleanup_started"
            assert publisher.runtime is None
            assert not resume
            calls.append(reset.operation_id)

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(DeviceMaintenanceOwner, "capture", lambda *_: Owner())
    result = await publisher.reset(**pairing_request(page, operation_id=str(uuid4())))
    assert result["completed"] and not publisher.snapshot()["publication_enabled"]
    assert publisher.policy.profile(page["profile_id"]).reset.phase == "completed"
    await publisher._step()
    assert publisher.runtime is None and len(calls) == 2
    assert camera_pairing.read_text() == "must survive"
    await monitor.close()


@pytest.mark.asyncio
async def test_failed_reset_resumes_same_operation_and_never_republishes(tmp_path, monkeypatch):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    page = await publisher.pairing()
    calls = []

    class Owner:
        async def cleanup(self, resources, reset, *, resume):
            calls.append(resume)
            if len(calls) == 1:
                raise RuntimeError("interrupted cleanup")

        def close(self):
            pass

    monkeypatch.setattr(DeviceMaintenanceOwner, "capture", lambda *_: Owner())
    operation = str(uuid4())
    with pytest.raises(AdminProtocolError):
        await publisher.reset(**pairing_request(page, operation_id=operation))
    await publisher._step()
    pending = await publisher.pairing()
    assert pending["pending_reset"] and pending["boot_id"] is None
    assert publisher.runtime is None
    with pytest.raises(AdminProtocolError):
        await publisher.reset(**pairing_request(pending, operation_id=str(uuid4())))
    await publisher.reset(**pairing_request(pending, operation_id=operation))
    assert calls == [False, True]
    assert not publisher.snapshot()["publication_enabled"]
    await monitor.close()


@pytest.mark.asyncio
async def test_start_failure_budget_not_replenished_by_refresh(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path, enabled=False)
    publisher = monitor.bridge.publisher
    Runtime.fail_start = True
    await publisher.configure(**configuration(monitor, sessions))
    for _ in range(4):
        await publisher._step()
    assert len(Runtime.instances) == 1
    assert publisher.snapshot()["state"] == "error"
    Runtime.fail_start = False
    await monitor.close()


def assert_wire_snapshot(snapshot):
    """Exercise the real boundary rather than the permissive runtime fixture."""
    encode_frame(
        {
            "v": 1,
            "kind": "publish",
            "boot_id": str(uuid4()),
            "request_id": str(uuid4()),
            "payload": snapshot,
        }
    )


@pytest.mark.asyncio
async def test_unmapped_provider_fields_cannot_expand_sensor_graph(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    try:
        row = replace(
            row,
            observations=row.observations
            + (
                DeviceObservation("motion_detected", True, 100),
                DeviceObservation("smoke_detected", False, 100),
            ),
        )
        await publish(monitor, providers[0], (row,), revision=2)
        await publisher._step()
        snapshot = publisher.runtime.snapshots[-1]
        child = snapshot["children"][0]
        assert child["mappings"] == ["contact"]
        assert child["values"]["contact"] is True
        assert all(value is None for key, value in child["values"].items() if key != "contact")
        assert_wire_snapshot(snapshot)
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_stale_unrelated_battery_does_not_hide_fresh_contact(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    try:
        monitor._clock = lambda: 400
        row = replace(
            row,
            observations=(
                DeviceObservation("contact_open", False, 395),
                DeviceObservation("battery_percent", 75, 0),
                DeviceObservation("tampered", False, 0),
            ),
        )
        await publish(monitor, providers[0], (row,), revision=2, at=400)
        # The broad diagnostic row truthfully notes stale supplementary fields.
        assert monitor._catalog.snapshot(now=400, max_age=360)["devices"][0]["stale"]
        await publisher._step()
        snapshot = publisher.runtime.snapshots[-1]
        child = snapshot["children"][0]
        assert child["available"] and child["values"]["contact"] is False
        assert_wire_snapshot(snapshot)
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_stale_sensor_observation_is_unknown_despite_fresh_battery_and_inventory(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    try:
        monitor._clock = lambda: 400
        row = replace(
            row,
            observations=(
                DeviceObservation("contact_open", False, 0),
                DeviceObservation("battery_percent", 75, 400),
            ),
        )
        await publish(monitor, providers[0], (row,), revision=2, at=400)
        await publisher._step()
        snapshot = publisher.runtime.snapshots[-1]
        assert snapshot["children"][0]["values"]["contact"] is None
        assert_wire_snapshot(snapshot)
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["offline", "stale", "unavailable", "unknown"])
async def test_fresh_boolean_cannot_override_unavailable_source(tmp_path, health):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    try:
        await publish(monitor, providers[0], (replace(row, source_health=health),), revision=2)
        await publisher._step()
        snapshot = publisher.runtime.snapshots[-1]
        child = snapshot["children"][0]
        assert not child["available"] and set(child["values"].values()) == {None}
        assert_wire_snapshot(snapshot)
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_display_names_follow_frozen_wire_unicode_rules(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    try:
        row = replace(row, name="\u0378😀 Window\u200b", model="\u0378Model\u200b")
        await publish(monitor, providers[0], (row,), revision=2)
        await publisher._step()
        snapshot = publisher.runtime.snapshots[-1]
        child = snapshot["children"][0]
        assert child["name"] == "\u0378😀 Window"
        assert child["model"] == "\u0378Model"
        assert_wire_snapshot(snapshot)
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_cancelled_reset_after_durable_denial_retires_and_resumes_same_operation(
    tmp_path, monkeypatch
):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    page, runtime = await publisher.pairing(), publisher.runtime
    old_camera = service._homekit_bridge
    persisted, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    begin = publisher.store.begin_reset

    def blocked_begin(reset):
        saved = begin(reset)
        loop.call_soon_threadsafe(persisted.set)
        assert release.wait(5)
        return saved

    class Owner:
        async def cleanup(self, resources, reset, *, resume):
            assert not resume
            assert publisher.runtime is None

        def close(self):
            pass

    monkeypatch.setattr(publisher.store, "begin_reset", blocked_begin)
    monkeypatch.setattr(DeviceMaintenanceOwner, "capture", lambda *_: Owner())
    operation = str(uuid4())
    task = asyncio.create_task(publisher.reset(**pairing_request(page, operation_id=operation)))
    try:
        await asyncio.wait_for(persisted.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.invalidated and runtime.stopped
        assert publisher.runtime is None
        assert service._homekit_bridge is old_camera
        saved = publisher.store.load().profile(page["profile_id"])
        assert not saved.enabled and saved.reset.phase == "denied"
        await publisher._step()
        pending = await publisher.pairing()
        assert pending["pending_reset"] and publisher.runtime is None
        await publisher.reset(**pairing_request(pending, operation_id=operation))
        assert publisher.store.load().profile(page["profile_id"]).reset.phase == "completed"
        assert not publisher.snapshot()["publication_enabled"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await monitor.close()


@pytest.mark.asyncio
async def test_logout_retirement_allows_same_account_publication_after_relogin(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    old_runtime, old_identity = publisher.runtime, publisher.runtime.snapshots[-1]["bridge"]
    old_scope = sessions.active_scope
    await monitor.close()
    assert old_runtime.stopped
    sessions.active_scope = AccountScope(old_scope.profile_id, old_scope.generation + 1)
    # Restore production start(): this regression is the coordinator lifecycle,
    # not a manual _step() that could bypass the permanent close fence.
    del publisher.start
    try:
        await monitor.reconcile()
        await publish(monitor, providers[-1], (sensor(sessions, generation=2),))
        async with asyncio.timeout(2):
            while (
                publisher.runtime is None
                or publisher.runtime is old_runtime
                or not publisher.runtime.snapshots
            ):
                await asyncio.sleep(0.01)
        current = publisher.runtime.snapshots[-1]
        assert current["scope"]["account_generation"] == old_scope.generation + 1
        assert current["bridge"] == old_identity
        assert current["children"][0]["values"]["contact"] is True
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_pairing_reply_from_retired_boot_cannot_authorize_new_boot(tmp_path, monkeypatch):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    runtime, status = publisher.runtime, publisher.runtime.status

    async def rotate_after_status(identity, *, admission):
        response = await status(identity, admission=admission)
        runtime.boot_id = str(uuid4())
        return response

    monkeypatch.setattr(runtime, "status", rotate_after_status)
    try:
        page = await publisher.pairing()
        assert not page["available"] and not page["paired_known"]
        assert page["boot_id"] is None
        assert not publisher.snapshot()["published"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_publish_ack_from_retired_boot_does_not_mark_new_boot_published(
    tmp_path, monkeypatch
):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    runtime, publish_snapshot = publisher.runtime, publisher.runtime.publish

    async def rotate_after_publish(snapshot, *, admission):
        response = await publish_snapshot(snapshot, admission=admission)
        runtime.boot_id = str(uuid4())
        return response

    monkeypatch.setattr(runtime, "publish", rotate_after_publish)
    try:
        await publisher._step()
        assert not publisher.snapshot()["published"]
        assert publisher.snapshot()["boot_id"] is None
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_account_change_during_reveal_does_not_return_pairing_secret(tmp_path, monkeypatch):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    page, runtime = await publisher.pairing(), publisher.runtime
    reveal = runtime.reveal

    async def change_account_after_reveal(identity, *, admission):
        response = await reveal(identity, admission=admission)
        sessions.active_scope = AccountScope(str(uuid4()), sessions.active_scope.generation + 1)
        return response

    monkeypatch.setattr(runtime, "reveal", change_account_after_reveal)
    try:
        with pytest.raises(AdminProtocolError):
            await publisher.reveal(**pairing_request(page))
        assert not publisher.snapshot()["published"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("prerequisite", ["mode", "imports", "monitoring"])
async def test_publication_enable_rejects_missing_prerequisite_without_saving(
    tmp_path, prerequisite
):
    service, client, sessions, monitor, providers, row = await ready(tmp_path, enabled=False)
    publisher = monitor.bridge.publisher
    if prerequisite == "mode":
        from halo_server.configuration import ServerMode

        service.configuration = replace(service.configuration, mode=ServerMode.RING_ONLY)
    elif prerequisite == "imports":
        await monitor.bridge.configure(
            **preferences(sessions, monitor.bridge.registry.revision, enabled=False)
        )
    else:
        await monitor.set_enabled(
            **preference(sessions, revision=monitor.policy.revision, enabled=False)
        )
    try:
        with pytest.raises(AdminProtocolError) as error:
            await publisher.configure(**configuration(monitor, sessions))
        assert error.value.code == "configuration_conflict"
        assert not publisher.store.path.exists()
        assert not Runtime.instances
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_publication_ack_without_advertisement_is_not_reported_published(
    tmp_path, monkeypatch
):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher = monitor.bridge.publisher
    runtime, original = publisher.runtime, publisher.runtime.status

    async def pending(identity, *, admission):
        reply = await original(identity, admission=admission)
        return reply | {"published": False}

    monkeypatch.setattr(runtime, "status", pending)
    try:
        await publisher._step()
        assert not publisher.snapshot()["published"]
        assert not (await publisher.pairing())["available"]
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_import_write_in_flight_cannot_restart_old_publication(tmp_path, monkeypatch):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher, bridge = monitor.bridge.publisher, monitor.bridge
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    save = bridge.store.configure
    old_runtime = publisher.runtime

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return save(*args, **kwargs)

    monkeypatch.setattr(bridge.store, "configure", blocked)
    task = asyncio.create_task(
        bridge.configure(**preferences(sessions, bridge.registry.revision, enabled=False))
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert old_runtime.invalidated
        await publisher._step()
        assert publisher.runtime is None
        assert len(Runtime.instances) == 1
        release.set()
        await task
        await publisher._step()
        assert publisher.runtime is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await monitor.close()


@pytest.mark.asyncio
async def test_conflicting_current_schema_faults_reading_before_registry_flush(tmp_path):
    service, client, sessions, monitor, providers, row = await ready(tmp_path)
    publisher, bridge = monitor.bridge.publisher, monitor.bridge
    try:
        bridge._records = (replace(row, capability_evidence=("unqualified-schema",)),)
        # The metadata write has not yet changed the durable graph/revision.
        assert bridge.registry.entries[0].selected
        await publisher._step()
        child = publisher.runtime.snapshots[-1]["children"][0]
        assert not child["available"] and child["values"]["contact"] is None
        assert_wire_snapshot(publisher.runtime.snapshots[-1])
    finally:
        await monitor.close()
