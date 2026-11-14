import json

import pytest

from halo_server.devices.catalog import CatalogError, DeviceCatalog
from halo_server.devices.models import DeviceIdentity, DeviceObservation, DeviceRecord

PROFILE = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def record(device="contact", *, location="private-location", **changes):
    return DeviceRecord(
        **(
            {
                "identity": DeviceIdentity(PROFILE, "ring_hub", location, "private-hub", device),
                "account_generation": 1,
                "provider_generation": 1,
                "name": "Door",
                "family": "contact",
                "kind": "sensor.contact",
                "source_health": "online",
                "supported": True,
                "observations": (DeviceObservation("contact_open", False, 100),),
            }
            | changes
        )
    )


def catalog(*records):
    value = DeviceCatalog(PROFILE, 1, 1)
    value.apply(tuple(records), complete=True, revision=1, observed_at=100)
    return value


def test_partial_missing_rows_are_retained_and_complete_absence_becomes_unavailable():
    value = catalog(record("a"), record("b"))
    value.apply((record("a"),), complete=False, revision=2, observed_at=101)
    assert len(value.snapshot(now=101)["devices"]) == 2
    assert all(row["available"] for row in value.snapshot(now=101)["devices"])
    value.apply((record("a"),), complete=True, revision=3, observed_at=102)
    missing = next(
        row for row in value.snapshot(now=102)["devices"] if row["id"] == record("b").key
    )
    assert missing["available"] is False and missing["stale"]
    assert missing["observed"]["contact_open"] is None
    assert missing["observation_details"]["contact_open"]["last_known_value"] is False
    value.apply((record("b", name="Renamed"),), complete=False, revision=4, observed_at=103)
    returned = next(row for row in value.snapshot(now=103)["devices"] if row["id"] == missing["id"])
    assert returned["name"] == "Renamed" and returned["available"] is True


def test_expired_unknown_offline_and_future_data_never_report_closed():
    value = catalog(record())
    row = value.snapshot(now=401)["devices"][0]
    assert row["available"] is None and row["stale"] and row["observed"]["contact_open"] is None
    for state in ("offline", "unavailable", "stale"):
        row = catalog(record(source_health=state)).snapshot(now=100)["devices"][0]
        assert row["stale"] and row["observed"]["contact_open"] is None
    row = catalog(
        record(observations=(DeviceObservation("contact_open", None, None, "unknown"),))
    ).snapshot(now=100)["devices"][0]
    assert row["observed"]["contact_open"] is None
    assert row["observation_details"]["contact_open"]["status"] == "unknown"


def test_scope_and_revision_fences_leave_catalog_atomically_unchanged():
    value = catalog(record())
    before = value.snapshot(now=101)
    changes = (
        {"identity": DeviceIdentity(OTHER, "ring_hub", "l", "h", "contact")},
        {"account_generation": 2},
        {"provider_generation": 2},
    )
    for change in changes:
        with pytest.raises(CatalogError):
            value.apply((record(**change),), complete=True, revision=2, observed_at=101)
        assert value.snapshot(now=101) == before
    for revision, timestamp in ((1, 101), (0, 101), (True, 101), (2, 99), (2, float("nan"))):
        with pytest.raises(CatalogError):
            value.apply((), complete=True, revision=revision, observed_at=timestamp)
        assert value.snapshot(now=101) == before


def test_scoped_complete_inventory_cannot_mark_other_location_missing():
    first, second = record("a"), record("b", location="other-location")
    value = catalog(first, second)
    value.apply(
        (), complete=True, revision=2, observed_at=101, source_keys=(first.identity.source_key,)
    )
    rows = {row["id"]: row for row in value.snapshot(now=101)["devices"]}
    assert rows[first.key]["available"] is False
    assert rows[second.key]["available"] is True
    assert value.complete is False


def test_duplicate_oversize_and_forged_batches_are_rejected_without_partial_commit(monkeypatch):
    import halo_server.devices.catalog as module

    monkeypatch.setattr(module, "MAX_DEVICES", 2)
    value = catalog(record("a"))
    before = value.snapshot(now=100)
    for items in ((record("b"), record("b")), (record("b"), record("c")), (record("a"),) * 3):
        with pytest.raises(CatalogError):
            value.apply(items, complete=True, revision=2, observed_at=101)
        assert value.snapshot(now=100) == before
    forged = record("b")
    object.__setattr__(forged, "source_health", "made-up")
    with pytest.raises(CatalogError):
        value.apply((forged,), complete=True, revision=2, observed_at=101)
    assert value.snapshot(now=100) == before


def test_public_projection_has_opaque_identity_and_no_raw_location_hardware_or_account():
    value = catalog(record())
    body = json.dumps(value.snapshot(now=101))
    for sensitive in (PROFILE, "private-location", "private-hub"):
        assert sensitive not in body
    row = value.snapshot(now=101)["devices"][0]
    assert len(row["id"]) == len(row["location_id"]) == 64
