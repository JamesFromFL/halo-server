from dataclasses import FrozenInstanceError, replace

import pytest

from halo_server.devices.models import DeviceIdentity, DeviceObservation, DeviceRecord

PROFILE = "11111111-1111-4111-8111-111111111111"


def identity(**changes):
    return DeviceIdentity(
        **(
            {
                "profile_id": PROFILE,
                "provider": "ring_hub",
                "location_id": "location",
                "asset_id": "hub",
                "device_id": "contact",
                "endpoint": "",
            }
            | changes
        )
    )


def record(**changes):
    return DeviceRecord(
        **(
            {
                "identity": identity(),
                "account_generation": 1,
                "provider_generation": 1,
                "name": "Door",
                "family": "contact",
                "kind": "sensor.contact",
            }
            | changes
        )
    )


def test_identity_is_stable_under_rename_and_bounded_to_endpoint_and_account():
    first = record()
    assert replace(first, name="Renamed", model="New label").key == first.key
    for change in (
        {"profile_id": "22222222-2222-4222-8222-222222222222"},
        {"provider": "ring_rest"},
        {"location_id": "other"},
        {"asset_id": "other"},
        {"device_id": "other"},
        {"endpoint": "zone1"},
    ):
        assert identity(**change).key != first.key
    # Unambiguous structured hashing, not delimiter joining.
    assert (
        identity(asset_id="a:b", device_id="c").key != identity(asset_id="a", device_id="b:c").key
    )
    assert len(first.key) == 64
    with pytest.raises(FrozenInstanceError):
        first.name = "modified"


def test_metadata_is_sanitized_but_identity_is_not_lossily_changed():
    value = record(name="Front\x00\u202e door" + "a" * 300, location_name="Home\nWest")
    assert "\x00" not in value.name and "\u202e" not in value.name and len(value.name) == 256
    assert value.location_name == "HomeWest"
    with pytest.raises(ValueError):
        identity(device_id="id\nvalue")
    with pytest.raises(ValueError):
        record(name="x" * 4097)


def test_unknown_false_and_stale_are_distinct_and_last_value_never_becomes_current():
    known = DeviceObservation("contact_open", False, 100)
    assert known.as_dict(now=101, max_age=30)["value"] is False
    unknown = DeviceObservation("contact_open", None, None, status="unknown")
    assert unknown.as_dict(now=101, max_age=30)["status"] == "unknown"
    expired = known.as_dict(now=131, max_age=30)
    assert expired["value"] is None and expired["last_known_value"] is False
    assert expired["status"] == "stale"
    assert known.as_dict(now=99, max_age=30)["status"] == "stale"
    assert known.as_dict(now=101, max_age=30, stale=True)["value"] is None


@pytest.mark.parametrize(
    "value", [[], {}, float("nan"), float("inf"), 2**53, 10**1000, "x" * 129, "a\nb"]
)
def test_malformed_observations_are_rejected(value):
    with pytest.raises(ValueError):
        DeviceObservation("contact_open", value, 100)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": []},
        {"value": None},
        {"observed_at": True},
        {"observed_at": -1},
        {"observed_at": float("nan")},
        {"observed_at": 10**1000},
        {"source": "x" * 129},
        {"name": "bad name"},
        {"status": "unknown", "value": False},
    ],
)
def test_invalid_observation_authority_is_rejected(changes):
    with pytest.raises(ValueError):
        DeviceObservation(
            **({"name": "contact_open", "value": False, "observed_at": 100} | changes)
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"account_generation": True},
        {"provider_generation": 0},
        {"capabilities": ("contact",)},
        {"source_health": []},
        {"supported": 1},
        {"capabilities": ["contact"]},
        {"capability_evidence": ("rest:contact",) * 2},
        {"observations": (DeviceObservation("contact_open", False, 100),) * 2},
    ],
)
def test_records_reject_ambiguous_or_unbounded_contracts(changes):
    with pytest.raises(ValueError):
        record(**changes)


@pytest.mark.parametrize(
    "name,value",
    [
        ("contact_open", "false"),
        ("motion_detected", 0),
        ("tampered", "no"),
        ("ac_power", 1),
        ("battery_percent", True),
        ("battery_percent", -1),
        ("battery_percent", 101),
        ("battery_percent", 25.5),
        ("alarm_mode", False),
        ("temperature_celsius", "freeze"),
    ],
)
def test_canonical_observation_types_cannot_coerce_unknown_to_false(name, value):
    with pytest.raises(ValueError):
        DeviceObservation(name, value, 100)
