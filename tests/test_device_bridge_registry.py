"""Bridge identity, admission, capacity, and crash-safety regression tests."""

import json
import os
import stat
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from halo_server.devices.bridge_registry import (
    MAX_BRIDGE_CHILDREN,
    BridgeDevice,
    BridgeProfile,
    BridgeRegistry,
    BridgeRegistryConflictError,
    BridgeRegistryError,
    BridgeRegistryStore,
    bridge_projection,
)
from halo_server.devices.models import DeviceIdentity, DeviceObservation, DeviceRecord

PROFILE = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def record(
    number=1, *, profile=PROFILE, kind="contact-sensor", capabilities=("contact",), **changes
):
    return DeviceRecord(
        **(
            {
                "identity": DeviceIdentity(
                    profile, "ring-hub", "location", "asset", f"device-{number}"
                ),
                "account_generation": 1,
                "provider_generation": 1,
                "name": "Front Door",
                "family": "hub",
                "kind": kind,
                "capabilities": capabilities,
                "capability_evidence": ("ring-hub-device-schema",),
                "supported": True,
                "observations": (DeviceObservation("contact_open", False, 10),),
            }
            | changes
        )
    )


def configure(store, records, **changes):
    return store.configure(
        PROFILE,
        **(
            {
                "records": records,
                "enabled": True,
                "auto_import": True,
                "expected_revision": store.load().revision,
            }
            | changes
        ),
    )


def test_pristine_load_and_unconfigured_discovery_do_not_touch_disk(tmp_path):
    store = BridgeRegistryStore(tmp_path / "private" / "registry.json")
    assert store.load() == BridgeRegistry()
    assert store.reconcile(PROFILE, (record(),)) == BridgeRegistry()
    assert not store.path.parent.exists()
    default = bridge_projection(BridgeRegistry(), None)
    assert not default["configured"] and default["devices"] == []
    seen = bridge_projection(BridgeRegistry(), PROFILE, (record(),))
    assert seen["devices"][0]["reason"] == "bridge-not-configured"
    assert seen["devices"][0]["accessory_uuid"] is None
    assert not seen["devices"][0]["selected"]


def test_explicit_configuration_is_durable_private_and_excludes_live_state(tmp_path):
    store = BridgeRegistryStore(tmp_path / "private" / "registry.json")
    saved = configure(store, (record(),))
    assert saved.revision == 1 and saved.entries[0].selected
    assert BridgeRegistryStore(store.path).load() == saved
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.enrollment_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    body = store.path.read_text()
    for secret_or_live in (
        "Front Door",
        "observations",
        "contact_open",
        "generation",
        "pairing_code",
        "private_key",
    ):
        assert secret_or_live not in body
    assert int(saved.profiles[0].username[:2], 16) & 3 == 2
    with pytest.raises(FrozenInstanceError):
        saved.profiles[0].enabled = False


def test_identity_survives_names_order_absence_restart_and_runtime_generations(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    records = (record(1), record(2))
    saved = configure(store, records)
    raw = store.path.read_bytes()
    renamed = tuple(
        replace(
            item,
            name="Renamed",
            account_generation=7,
            provider_generation=9,
            observations=(DeviceObservation("contact_open", True, 500),),
        )
        for item in reversed(records)
    )
    assert store.reconcile(PROFILE, renamed) == saved
    assert store.path.read_bytes() == raw
    assert store.reconcile(PROFILE, ()) == saved
    assert BridgeRegistryStore(store.path).reconcile(PROFILE, records) == saved
    assert all(
        item["reason"] == "device-missing" for item in bridge_projection(saved, PROFILE)["devices"]
    )


def test_auto_import_off_requires_explicit_per_device_selection_and_cannot_actuate(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    devices = (record(1), record(2), record(3, kind="panic-button", capabilities=("siren",)))
    saved = configure(store, devices, auto_import=False)
    assert not any(item.selected for item in saved.entries)
    selected = configure(
        store,
        devices,
        auto_import=False,
        included_devices=tuple(sorted((devices[0].key, devices[2].key))),
    )
    assert [item.device_id for item in selected.entries if item.selected] == [devices[0].key]
    rows = {item["id"]: item for item in bridge_projection(selected, PROFILE, devices)["devices"]}
    assert rows[devices[2].key]["reason"] == "unsupported-device"
    assert rows[devices[2].key]["mappings"] == []


@pytest.mark.parametrize(
    "kind,capabilities,mappings",
    [
        ("contact-sensor", ("contact",), ["contact"]),
        ("retrofit-zone", ("contact",), ["contact"]),
        ("motion-sensor", ("motion",), ["motion"]),
        ("flood-sensor", ("flood",), ["leak"]),
        ("flood-freeze-sensor", ("flood", "freeze"), ["leak"]),
        ("smoke-alarm", ("smoke",), ["smoke"]),
        ("co-alarm", ("carbon-monoxide",), ["carbon_monoxide"]),
        ("smoke-co-alarm", ("smoke", "carbon-monoxide"), ["carbon_monoxide", "smoke"]),
        ("smoke-co-listener", ("smoke", "carbon-monoxide"), ["carbon_monoxide", "smoke"]),
        ("freeze-sensor", ("freeze",), []),
        ("glass-break-sensor", ("glass-break",), []),
        ("security-panel", ("mode", "siren"), []),
        ("panic-button", ("siren",), []),
        ("switch", ("switch",), []),
        ("lock", ("lock",), []),
        ("valve", ("valve",), []),
        ("unknown", ("contact",), []),
    ],
)
def test_closed_mapping_allowlist(kind, capabilities, mappings, tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    item = record(kind=kind, capabilities=capabilities)
    saved = configure(store, (item,))
    row = bridge_projection(saved, PROFILE, (item,))["devices"][0]
    assert row["mappings"] == mappings
    assert row["selected"] is bool(mappings)


@pytest.mark.parametrize(
    "changes",
    [
        {"identity": DeviceIdentity(PROFILE, "ring-rest", "location", "asset", "contact")},
        {"capability_evidence": ("ring-rest-schema",)},
        {"capabilities": ()},
        {"family": "unknown"},
        {"supported": False},
    ],
)
def test_mapping_requires_provider_kind_capability_and_schema_evidence(changes, tmp_path):
    saved = configure(BridgeRegistryStore(tmp_path / "registry.json"), (record(**changes),))
    assert not saved.entries[0].selected and not saved.entries[0].mappings


def test_later_qualification_and_revocation_survive_restart_and_absence(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    incomplete, complete = record(capabilities=()), record()
    first = configure(store, (incomplete,))
    assert first.entries[0].mappings == ()
    qualified = store.reconcile(PROFILE, (complete,))
    assert qualified.entries[0].accessory_uuid == first.entries[0].accessory_uuid
    assert qualified.entries[0].selected and qualified.entries[0].qualified
    revoked = BridgeRegistryStore(store.path).reconcile(PROFILE, (incomplete,))
    assert not revoked.entries[0].selected and not revoked.entries[0].qualified
    assert revoked.entries[0].mappings == ("contact",)
    assert store.reconcile(PROFILE, ()) == revoked
    restored = store.reconcile(PROFILE, (complete,))
    assert restored.entries[0] == qualified.entries[0]


def test_service_enrichment_preserves_identity_and_partial_service_filter(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    partial = record(kind="smoke-co-alarm", capabilities=("smoke",))
    full = replace(partial, capabilities=("smoke", "carbon-monoxide"))
    first = configure(store, (partial,))
    assert not first.entries[0].selected
    ready = store.reconcile(PROFILE, (full,))
    filtered = configure(store, (full,), excluded_mappings=("smoke",))
    assert filtered.entries[0].accessory_uuid == first.entries[0].accessory_uuid
    assert filtered.entries[0].selected and ready.entries[0].selected
    assert bridge_projection(filtered, PROFILE, (full,))["devices"][0]["mappings"] == [
        "carbon_monoxide"
    ]
    excluded = configure(store, (full,), excluded_mappings=("carbon_monoxide", "smoke"))
    row = bridge_projection(excluded, PROFILE, (full,))["devices"][0]
    assert not row["selected"] and row["reason"] == "mappings-excluded"


@pytest.mark.parametrize(
    "field,reason",
    [
        ("excluded_devices", "device-excluded"),
        ("excluded_locations", "location-excluded"),
        ("excluded_kinds", "kind-excluded"),
    ],
)
def test_exclusions_override_automatic_and_explicit_import_and_keep_identity(
    field, reason, tmp_path
):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    item = record()
    first = configure(store, (item,))
    key = {
        "excluded_devices": item.key,
        "excluded_locations": item.identity.location_key,
        "excluded_kinds": item.kind,
    }[field]
    kwargs = {field: (key,)}
    if field != "excluded_devices":
        kwargs["included_devices"] = (item.key,)
    changed = configure(store, (item,), **kwargs)
    assert changed.entries[0].accessory_uuid == first.entries[0].accessory_uuid
    assert not changed.entries[0].selected
    assert bridge_projection(changed, PROFILE, (item,))["devices"][0]["reason"] == reason
    restored = configure(store, (item,))
    assert restored.entries[0] == first.entries[0]


def test_capacity_is_stable_missing_children_reserve_slots_and_exclusions_free_slots(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    devices = tuple(record(number) for number in range(MAX_BRIDGE_CHILDREN + 2))
    first = configure(store, devices)
    assert len(first.entries) == 151 and sum(item.selected for item in first.entries) == 149
    selected = {item.device_id for item in first.entries if item.selected}
    assert bridge_projection(first, PROFILE, devices)["overflow_count"] == 2
    overflow_records = tuple(item for item in devices if item.key not in selected)
    assert store.reconcile(PROFILE, overflow_records) == first
    assert store.reconcile(PROFILE, tuple(reversed(devices))) == first
    removed = sorted(selected)[0]
    changed = configure(store, devices, excluded_devices=(removed,))
    assert sum(item.selected for item in changed.entries) == 149
    assert selected - {removed} < {item.device_id for item in changed.entries if item.selected}
    disabled = configure(store, devices, enabled=False, excluded_devices=(removed,))
    assert disabled.entries == changed.entries
    assert not bridge_projection(disabled, PROFILE, devices)["enabled"]


def test_total_retained_capacity_and_account_capacity_are_explicit(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    devices = tuple(record(number) for number in range(1024))
    saved = configure(store, devices)
    extra = record(1025)
    assert store.reconcile(PROFILE, (extra,)) == saved
    rows = {item["id"]: item for item in bridge_projection(saved, PROFILE, (extra,))["devices"]}
    assert rows[extra.key]["reason"] == "registry-capacity"
    assert rows[extra.key]["accessory_uuid"] is None
    for _ in range(15):
        saved = store.configure(
            str(uuid4()), expected_revision=saved.revision, enabled=True, auto_import=True
        )
    with pytest.raises(BridgeRegistryError, match="account capacity"):
        store.configure(
            str(uuid4()), expected_revision=saved.revision, enabled=True, auto_import=True
        )
    assert store.load() == saved


def test_account_binding_requires_explicit_new_configuration(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    first = configure(store, (record(),))
    other = record(profile=OTHER)
    assert store.reconcile(OTHER, (other,)) == first
    projection = bridge_projection(first, OTHER, (other,))
    assert not projection["configured"] and projection["retained_count"] == 0
    with pytest.raises(BridgeRegistryError):
        store.reconcile(PROFILE, (other,))
    second = store.configure(
        OTHER, records=(other,), expected_revision=first.revision, enabled=True, auto_import=True
    )
    assert second.profile(OTHER).accessory_uuid != first.profile(PROFILE).accessory_uuid
    assert second.profile(OTHER).username != first.profile(PROFILE).username
    assert next(item for item in second.entries if item.profile_id == PROFILE) == first.entries[0]


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"auto_import": None},
        {"included_devices": ["a" * 64]},
        {"included_devices": ("a" * 64,), "excluded_devices": ("a" * 64,)},
        {"excluded_devices": ("a" * 64,)},
        {"excluded_locations": ("b" * 64,)},
        {"excluded_kinds": ("unknown",)},
        {"excluded_mappings": ("switch",)},
        {"excluded_mappings": ("smoke", "smoke")},
    ],
)
def test_bad_or_unknown_preferences_are_rejected_atomically(changes, tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    saved = configure(store, (record(),))
    original = store.path.read_bytes()
    with pytest.raises(BridgeRegistryError):
        configure(store, (record(),), **changes)
    assert store.path.read_bytes() == original and store.load() == saved


def test_cas_conflicts_noop_updates_and_identity_replacement_rejection(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    first = configure(store, (record(),))
    assert configure(store, (record(),)) == first
    for operation in (
        lambda: store.reconcile(PROFILE, (), expected_revision=0),
        lambda: configure(store, (record(),), expected_revision=0),
        lambda: store.save(first, expected_revision=0),
    ):
        with pytest.raises(BridgeRegistryConflictError):
            operation()
    for changed in (
        replace(first, entries=()),
        replace(first, entries=(replace(first.entries[0], accessory_uuid=str(uuid4())),)),
        replace(first, profiles=(replace(first.profiles[0], accessory_uuid=str(uuid4())),)),
        replace(first, profiles=(replace(first.profiles[0], username="02:11:22:33:44:55"),)),
    ):
        with pytest.raises(BridgeRegistryError):
            store.save(changed)
    assert store.load() == first


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(version=2),
        lambda value: value.update(revision=True),
        lambda value: value.update(panic_enabled=True),
        lambda value: value["profiles"][0].update(enabled=1),
        lambda value: value["profiles"][0].update(username="01:00:00:00:00:00"),
        lambda value: value["entries"][0].update(mappings=["switch"]),
        lambda value: value["entries"][0].update(profile_id=OTHER),
        lambda value: value["entries"][0].update(
            accessory_uuid=value["profiles"][0]["accessory_uuid"]
        ),
        lambda value: value["entries"].append(value["entries"][0]),
    ],
)
def test_corruption_fails_closed_and_is_never_replaced(mutation, tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    saved = configure(store, (record(),))
    body = saved.as_dict()
    mutation(body)
    store.path.write_text(json.dumps(body))
    original = store.path.read_bytes()
    for operation in (
        store.load,
        lambda: store.reconcile(PROFILE, (record(),)),
        lambda: store.configure(PROFILE, expected_revision=1, enabled=True, auto_import=True),
    ):
        with pytest.raises(BridgeRegistryError):
            operation()
        assert store.path.read_bytes() == original


def test_forged_nested_dataclass_is_revalidated_before_save(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    saved = configure(store, (record(),))
    forged = replace(saved)
    object.__setattr__(forged.entries[0], "mappings", ("panic",))
    with pytest.raises(BridgeRegistryError):
        store.save(forged)
    assert store.load().entries[0].mappings == ("contact",)


@pytest.mark.parametrize("removed", ["registry", "marker"])
def test_deleted_enrolled_state_never_generates_new_identities(removed, tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    configure(store, (record(),))
    target = store.path if removed == "registry" else store.enrollment_path
    target.unlink()
    for operation in (
        store.load,
        lambda: store.reconcile(PROFILE, (record(),)),
        lambda: store.configure(PROFILE, expected_revision=0, enabled=True, auto_import=True),
    ):
        with pytest.raises(BridgeRegistryError, match="missing"):
            operation()
    assert not target.exists()


def test_initial_write_failure_after_enrollment_is_conservatively_recoverable(
    tmp_path, monkeypatch
):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    original_replace = os.replace

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(BridgeRegistryError):
        configure(store, (record(),))
    assert store.enrollment_path.is_file() and not store.path.exists()
    monkeypatch.setattr(os, "replace", original_replace)
    with pytest.raises(BridgeRegistryError, match="missing"):
        store.load()


def test_pre_replace_failure_preserves_existing_registry(tmp_path, monkeypatch):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    saved = configure(store, (record(),))
    original = store.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(BridgeRegistryError) as caught:
        configure(store, (record(),), enabled=False)
    assert not caught.value.possibly_saved
    assert store.path.read_bytes() == original and store.load() == saved
    assert not tuple(tmp_path.glob("*.tmp"))


def test_post_replace_fsync_failure_reports_uncertainty_and_requires_reload(tmp_path, monkeypatch):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    saved = configure(store, (record(),))
    real_fsync = os.fsync

    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(BridgeRegistryError) as caught:
        configure(store, (record(),), enabled=False)
    assert caught.value.possibly_saved
    reloaded = store.load()
    assert reloaded.revision == saved.revision + 1 and not reloaded.profiles[0].enabled


def test_registry_rejects_symlink_permissions_oversize_and_invalid_marker(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    configure(store, (record(),))
    store.path.chmod(0o644)
    with pytest.raises(BridgeRegistryError):
        store.load()
    store.path.chmod(0o600)
    store.enrollment_path.write_bytes(b"tampered")
    with pytest.raises(BridgeRegistryError, match="marker"):
        store.load()
    store.path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(BridgeRegistryError, match="size limit"):
        store.load()
    link = tmp_path / "linked.json"
    link.symlink_to(store.path)
    with pytest.raises(BridgeRegistryError):
        BridgeRegistryStore(link).load()


def test_duplicate_json_keys_and_non_finite_constants_rejected(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    configure(store, (record(),))
    for text in ('{"version":1,"version":1}', '{"version":NaN}'):
        store.path.write_text(text)
        with pytest.raises(BridgeRegistryError):
            store.load()


def test_dto_bounds_and_forged_input_do_not_bypass_admission(tmp_path):
    store = BridgeRegistryStore(tmp_path / "registry.json")
    item = record()
    for records in ([item], (item, item), (record(profile=OTHER),), (item,) * 1025):
        with pytest.raises(BridgeRegistryError):
            store.reconcile(PROFILE, records)
    object.__setattr__(item.identity, "provider", "ring-hub\n")
    with pytest.raises(BridgeRegistryError):
        store.reconcile(PROFILE, (item,))
    with pytest.raises(BridgeRegistryError):
        BridgeProfile(
            PROFILE,
            str(uuid4()),
            "02:00:00:00:00:00",
            included_devices=tuple(f"{i:064x}" for i in range(513)),
        )
    with pytest.raises(BridgeRegistryError):
        BridgeDevice(
            PROFILE,
            "a" * 64,
            "b" * 64,
            "ring-hub",
            "contact-sensor",
            str(uuid4()),
            ("contact",),
            selected=True,
        )
