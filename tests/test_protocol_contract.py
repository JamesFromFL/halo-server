import json
from dataclasses import fields
from importlib.resources import files

from jsonschema import Draft202012Validator

from halo_server.api.events import JournalEvent
from halo_server.api.plugin import PLUGIN_PROTOCOL_VERSION
from halo_server.media.leases import maximum_session_seconds
from halo_server.ring.devices import DeviceSummary

PROTOCOL_ROOT = files("halo_server.api").joinpath("protocol", f"v{PLUGIN_PROTOCOL_VERSION}")


def _load(name):
    return json.loads(PROTOCOL_ROOT.joinpath(name).read_text(encoding="utf-8"))


def test_canonical_plugin_protocol_schema_and_fixtures_are_valid():
    schema = _load("schema.json")
    fixtures = _load("fixtures.json")

    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == f"urn:halo:plugin-protocol:v{PLUGIN_PROTOCOL_VERSION}"
    assert schema["x-halo-owner"] == "halo-server"
    assert schema["x-halo-protocol-version"] == PLUGIN_PROTOCOL_VERSION
    assert fixtures["protocol_version"] == PLUGIN_PROTOCOL_VERSION
    assert fixtures["schema_id"] == schema["$id"]

    validator = Draft202012Validator(schema)
    valid_names = []
    for case in fixtures["valid_cases"]:
        validator.validate(case["value"])
        valid_names.append(case["name"])
    assert len(valid_names) == len(set(valid_names))

    invalid_names = []
    for case in fixtures["invalid_cases"]:
        assert validator.is_valid(case["value"]) is case.get("schema_valid", False), case["name"]
        invalid_names.append(case["name"])
    assert len(invalid_names) == len(set(invalid_names))


def test_canonical_fixtures_match_server_serializer_boundaries():
    fixtures = _load("fixtures.json")
    cases = {case["name"]: case["value"] for case in fixtures["valid_cases"]}

    device = cases["device_snapshot"]["devices"][0]
    assert set(device) == {field.name for field in fields(DeviceSummary)}
    assert cases["capabilities"]["capabilities"] == [
        "devices",
        "events",
        "motion",
        "doorbell",
        "battery",
        "online",
        "intercom",
        "snapshot",
        "light",
        "siren",
    ]
    assert cases["capabilities"]["media"]["maximum_session_seconds"] == maximum_session_seconds()

    event = cases["event_envelope"]["event"]
    assert (
        JournalEvent(
            epoch=event["epoch"],
            sequence=event["sequence"],
            emitted_at=event["emitted_at"],
            type=event["type"],
            camera_id=event["camera_id"],
            payload=event["payload"],
        ).as_dict()
        == event
    )
    assert cases["event_hello"]["protocol_version"] == PLUGIN_PROTOCOL_VERSION
    assert cases["intercom_ready"]["protocol_version"] == PLUGIN_PROTOCOL_VERSION
