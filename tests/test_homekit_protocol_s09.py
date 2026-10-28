"""Audio fields never widen private authority or lose reply correlation."""

import base64
import copy
import json
from importlib.resources import files

import pytest

from halo_server.homekit.ipc import HomeKitProtocolError, encode_frame, validate_reply


def sample(kind, *, audio=False):
    corpus = json.loads(
        files("halo_server.homekit").joinpath("protocol/v1/fixtures.json").read_text()
    )
    return copy.deepcopy(
        next(
            row
            for row in corpus["valid"]
            if row["kind"] == kind and (not audio or row["payload"].get("audio_config") is not None)
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("sample_rate", 48000),
        ("sample_rate", True),
        ("packet_time", 10),
        ("max_bit_rate", 5),
        ("max_bit_rate", 65),
        ("payload_type", 95),
        ("channels", 2),
        ("channels", True),
        ("rtcp_interval", 0),
        ("rtcp_interval", float("inf")),
        ("clock_mode", "rfc7587"),
        ("gain", 100),
    ],
)
def test_audio_configuration_rejects_wrong_contract_and_peer_gain(field, value):
    envelope = sample("video_start", audio=True)
    envelope["payload"]["audio_config"][field] = value
    with pytest.raises(HomeKitProtocolError):
        encode_frame(envelope)


@pytest.mark.parametrize("revision", [0, 99])
def test_accepted_settings_reply_cannot_claim_an_unrelated_revision(revision):
    request = sample("audio_settings_update")
    request["payload"]["expected_revision"] = 7
    reply = request | {
        "kind": "audio_settings_result",
        "payload": {
            "scope": request["payload"]["scope"],
            "accepted": True,
            "code": "ok",
            "revision": revision,
        },
    }
    with pytest.raises(HomeKitProtocolError):
        validate_reply(request, reply)


@pytest.mark.parametrize("field,size", [("key", 16), ("salt", 14)])
def test_audio_prepare_reply_must_echo_selected_key_material(field, size):
    corpus = json.loads(
        files("halo_server.homekit").joinpath("protocol/v1/fixtures.json").read_text()
    )
    request = copy.deepcopy(
        next(
            row
            for row in corpus["valid"]
            if row["kind"] == "video_prepare" and row["payload"]["audio"] is not None
        )
    )
    response = copy.deepcopy(
        next(
            row
            for row in corpus["valid"]
            if row["kind"] == "video_prepare_result"
            and row["payload"]["endpoint"] is not None
            and row["payload"]["endpoint"]["audio"] is not None
        )
    )
    response.update(boot_id=request["boot_id"], request_id=request["request_id"])
    response["payload"].update(
        scope=request["payload"]["scope"], session_id=request["payload"]["session_id"]
    )
    response["payload"]["endpoint"]["audio"][field] = base64.b64encode(b"\xff" * size).decode()
    with pytest.raises(HomeKitProtocolError):
        validate_reply(request, response)
