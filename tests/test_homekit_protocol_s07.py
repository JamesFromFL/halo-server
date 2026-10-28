"""S07 private configuration and event authority contract regressions."""

import copy
import json
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator

from halo_server.homekit.ipc import HomeKitProtocolError, decode_body, validate_reply

S07_KINDS = {
    "accessory_config",
    "accessory_config_ack",
    "event_sync",
    "event_sync_ack",
    "event_state",
    "camera_event",
}


def contract():
    root = files("halo_server.homekit").joinpath("protocol", "v1")
    return json.loads(root.joinpath("fixtures.json").read_text()), Draft202012Validator(
        json.loads(root.joinpath("schema.json").read_text())
    )


def samples():
    fixtures, _ = contract()
    return {item["kind"]: item for item in reversed(fixtures["valid"])}


def test_s07_shared_cases_distinguish_schema_structure_and_runtime_invariants():
    fixtures, validator = contract()
    assert {item["kind"] for item in fixtures["invalid"]} >= S07_KINDS
    for item in fixtures["valid"]:
        if item["kind"] in S07_KINDS:
            validator.validate(item)
            assert decode_body(json.dumps(item).encode()) == item
    for item in fixtures["invalid"]:
        if item["kind"] in S07_KINDS:
            assert not validator.is_valid(item)
            with pytest.raises(HomeKitProtocolError):
                decode_body(json.dumps(item).encode())
    for item in fixtures["semantic_invalid"]:
        if item["kind"] in S07_KINDS:
            # Comparing page fields or uniqueness by one identity property is
            # deliberately enforced by both decoders after structural schema.
            validator.validate(item)
            with pytest.raises(HomeKitProtocolError):
                decode_body(json.dumps(item).encode())


@pytest.mark.parametrize("request_kind", ["accessory_config", "event_sync"])
@pytest.mark.parametrize("null_scope", [False, True])
def test_s07_reply_scope_correlation_includes_explicit_null(request_kind, null_scope):
    values = samples()
    request = copy.deepcopy(values[request_kind])
    reply = copy.deepcopy(values[f"{request_kind}_ack"])
    if null_scope:
        request["payload"]["scope"] = reply["payload"]["scope"] = None
        if request_kind == "accessory_config":
            request["payload"]["entries"] = []
    assert validate_reply(request, reply) is reply
    stale = copy.deepcopy(reply)
    stale["payload"]["scope"] = values[request_kind]["payload"]["scope"] if null_scope else None
    with pytest.raises(HomeKitProtocolError, match="ownership"):
        validate_reply(request, stale)


@pytest.mark.parametrize("field", ["revision", "generation", "page_index"])
@pytest.mark.parametrize("null_scope", [False, True])
def test_configuration_acknowledges_exact_registry_revision_generation_and_page(field, null_scope):
    values = samples()
    request = copy.deepcopy(values["accessory_config"])
    reply = copy.deepcopy(values["accessory_config_ack"])
    if null_scope:
        request["payload"].update(scope=None, entries=[])
        reply["payload"]["scope"] = None
    reply["payload"][field] += 1
    # Each modified ACK is valid alone, but it belongs to different work.
    assert decode_body(json.dumps(reply).encode()) == reply
    with pytest.raises(HomeKitProtocolError, match="configuration correlation"):
        validate_reply(request, reply)


def test_event_sync_replies_may_advance_the_source_epoch_and_cursor():
    values = samples()
    request = copy.deepcopy(values["event_sync"])
    request["payload"].update(epoch="old-epoch", sequence=10)
    reply = copy.deepcopy(values["event_sync_ack"])
    reply["payload"].update(epoch="current-epoch", sequence=100)
    assert validate_reply(request, reply) is reply


@pytest.mark.parametrize("kind", ["event_state", "camera_event"])
def test_decoder_acceptance_does_not_claim_a_source_deadline_is_still_fresh(kind):
    message = copy.deepcopy(samples()[kind])
    message["payload"]["expires_at_ms"] = 0
    assert message["payload"]["ttl_ms"] > 0
    assert decode_body(json.dumps(message).encode()) == message
