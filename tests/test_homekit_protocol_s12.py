"""Scoped recording wire admission and reply authority are independently bounded."""

import copy
import json
from importlib.resources import files
from uuid import uuid4

import pytest

from halo_server.homekit.ipc import (
    FEATURES,
    HomeKitProtocolError,
    validate_direction,
    validate_envelope,
    validate_reply,
)


def fixture(kind, *, status=None):
    values = json.loads(
        files("halo_server.homekit").joinpath("protocol/v1/fixtures.json").read_text()
    )["valid"]
    return copy.deepcopy(
        next(
            item
            for item in values
            if item["kind"] == kind and (status is None or item["payload"].get("status") == status)
        )
    )


@pytest.mark.parametrize(
    "kind", ["recording_settings_update", "recording_open", "recording_next", "recording_stop"]
)
def test_recording_requests_require_current_HAP_direction_and_exact_reply_ownership(kind):
    request = fixture(kind)
    reply = fixture(kind.replace("settings_update", "settings") + "_result")
    if kind == "recording_next":
        reply["payload"]["sequence"] = request["payload"]["sequence"]
    assert validate_direction(request, "hap") is request
    assert validate_direction(reply, "python") is reply
    assert validate_reply(request, reply) is reply
    with pytest.raises(HomeKitProtocolError):
        validate_direction(request, "python")
    with pytest.raises(HomeKitProtocolError):
        validate_direction(reply, "hap")
    for change in ["boot_id", "request_id", "scope", "job_id"]:
        if change == "job_id" and change not in reply["payload"]:
            continue
        changed = copy.deepcopy(reply)
        if change in {"boot_id", "request_id"}:
            changed[change] = str(uuid4())
        elif change == "scope":
            changed["payload"]["scope"]["privacy_generation"] += 1
        else:
            changed["payload"][change] = str(uuid4())
        with pytest.raises(HomeKitProtocolError):
            validate_reply(request, changed)


def test_recording_next_sequence_and_settings_revision_cannot_acknowledge_other_work():
    request, reply = fixture("recording_next"), fixture("recording_next_result")
    reply["payload"].update(kind="fragment", sequence=1, duration_ms=1000)
    with pytest.raises(HomeKitProtocolError):
        validate_reply(request, reply)
    request, reply = fixture("recording_settings_update"), fixture("recording_settings_result")
    reply["payload"]["revision"] = 2
    with pytest.raises(HomeKitProtocolError):
        validate_reply(request, reply)


@pytest.mark.parametrize("change", ["odd", "level", "bitrate", "gop", "audio-mode", "audio-clock"])
def test_selected_recording_geometry_rate_level_audio_and_GOP_are_coupled(change):
    item = fixture("recording_open")
    config = item["payload"]["configuration"]
    if change == "odd":
        config["video"]["width"] = 1279
    if change == "level":
        config["video"].update(width=1920, height=1080, level="3.1")
    if change == "bitrate":
        config["video"]["max_bit_rate"] = 14001
    if change == "gop":
        config["i_frame_interval_ms"] = 66
    if change == "audio-mode":
        config["audio"]["bitrate_mode"] = "cbr"
    if change == "audio-clock":
        config["audio"]["sample_rate"] = 8000
    with pytest.raises(HomeKitProtocolError):
        validate_envelope(item)


def test_exact_feature_marker_is_required_for_recording_compatibility():
    item = fixture("hello_ack")
    assert item["payload"]["features"] == list(FEATURES)
    for features in [list(FEATURES[:-1]), list(reversed(FEATURES)), [*FEATURES, "unknown"]]:
        changed = copy.deepcopy(item)
        changed["payload"]["features"] = features
        with pytest.raises(HomeKitProtocolError):
            validate_envelope(changed)


def test_optional_accessory_recording_metadata_absence_remains_disabled_compatible():
    item = fixture("accessory_config")
    assert validate_envelope(item) is item
    item = next(
        value
        for value in json.loads(
            files("halo_server.homekit").joinpath("protocol/v1/fixtures.json").read_text()
        )["valid"]
        if value["kind"] == "accessory_config"
        and value["payload"]["entries"]
        and "recording" in value["payload"]["entries"][0]
    )
    assert validate_envelope(item) is item
    item["payload"]["entries"][0]["recording"]["owner_opt_in"] = True
    with pytest.raises(HomeKitProtocolError):
        validate_envelope(item)
