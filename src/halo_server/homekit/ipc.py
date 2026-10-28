"""Strict bounded framing for the inherited private HomeKit control channel."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
import struct
import uuid
from typing import Any

from halo_server.homekit.text import literal_text

PROTOCOL_VERSION = 1
FEATURES = ("classic-camera-control-v1", "bulk-cancel-v1", "hksv-recording-v1")
MAX_CONTROL_BYTES = 65536
MAX_JSON_DEPTH = 32
IO_TIMEOUT_SECONDS = 5.0
# These are format/admission ceilings, not claims of implemented media support.
MAX_INVENTORY_DEVICES = 256
MAX_INVENTORY_PAGE = 32
MAX_BULK_CHUNK_BYTES = 65536
MAX_BULK_WINDOW_CHUNKS = 2
MAX_BULK_WINDOW_BYTES = 131072
BULK_HEADER_BYTES = 32
BULK_LIMITS = {
    "snapshot": 8 * 1024 * 1024,
    "recording_init": 1024 * 1024,
    "recording_fragment": 8 * 1024 * 1024,
    "talkback_pcm": 1920,
}
BULK_KINDS = {name: index for index, name in enumerate(BULK_LIMITS, 1)}
POLICY_FIELDS = (
    "synchronized",
    "streaming",
    "live_audio",
    "snapshots",
    "recording",
    "recording_audio",
    "talkback",
    "controls",
)
CAPABILITY_FIELDS = (
    "video",
    "audio",
    "snapshot",
    "talkback",
    "light",
    "siren",
    "doorbell",
    "battery",
)
RESULT_CODES = frozenset(
    {
        "ok",
        "unavailable",
        "unsupported",
        "unauthorized",
        "stale-scope",
        "policy-denied",
        "busy",
        "timeout",
        "cancelled",
        "invalid-request",
        "not-found",
        "conflict",
        "source-failed",
        "expired",
        "protocol-error",
        "invalidated",
        "not-ready",
    }
)


class HomeKitProtocolError(RuntimeError):
    """Private IPC rejected a malformed, incompatible or oversized message."""


def _uuid(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _bounded_text(value: Any, maximum: int, *, empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (empty or bool(value))
        and len(value) <= maximum
        and all(ord(char) >= 32 and not 0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


def _display_text(value: Any, maximum: int, *, empty: bool = False) -> bool:
    return literal_text(value, maximum, empty=empty)


def _safe_integer(value: Any) -> bool:
    return (
        type(value) in {int, float}
        and abs(value) < 2**53
        and math.isfinite(value)
        and value == int(value)
    )


def _integer(minimum: int = 1, maximum: int = 2**53 - 1):
    return lambda value: _safe_integer(value) and minimum <= value <= maximum


def _enum(*values):
    return lambda value: isinstance(value, str) and value in values


def _nullable(check):
    return lambda value: value is None or check(value)


def _object(**fields):
    return lambda value: (
        isinstance(value, dict)
        and set(value) == set(fields)
        and all(check(value[key]) for key, check in fields.items())
    )


def _boolean(value: Any) -> bool:
    return type(value) is bool


_positive = _integer()
_revision = _integer(0)
_code = _enum(*RESULT_CODES)
_account = _object(profile_id=_uuid, account_generation=_positive)
_scope = _object(
    profile_id=_uuid,
    camera_id=lambda value: _display_text(value, 128),
    account_generation=_positive,
    device_generation=_positive,
    privacy_generation=_positive,
)
_policy_fields = _object(**dict.fromkeys(POLICY_FIELDS, _boolean))


def _policy(value: Any) -> bool:
    return _policy_fields(value) and (not value["recording_audio"] or value["recording"])


def _capabilities(value: Any) -> bool:
    if not _object(**dict.fromkeys(CAPABILITY_FIELDS, _boolean), evidence=lambda _: True)(value):
        return False
    evidence = value["evidence"]
    labels = {f"{source}:{name}" for source in ("sdk", "halo") for name in CAPABILITY_FIELDS}
    return (
        isinstance(evidence, list)
        and len(evidence) <= 16
        and all(isinstance(item, str) and item in labels for item in evidence)
        and len(set(evidence)) == len(evidence)
        and all(
            not value[name] or any(f"{source}:{name}" in evidence for source in ("sdk", "halo"))
            for name in CAPABILITY_FIELDS
        )
    )


def _ascii32(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 32
        and all(32 <= ord(char) <= 126 for char in value)
    )


_video_fields = _object(
    codec=_enum("h264"),
    width=_nullable(_integer(1, 8192)),
    height=_nullable(_integer(1, 8192)),
    profile=_nullable(_ascii32),
    level=_nullable(_ascii32),
    frame_rate_num=_nullable(_integer(1, 1_000_000)),
    frame_rate_den=_nullable(_integer(1, 1_000_000)),
)


def _video(value: Any) -> bool:
    return _video_fields(value) and (
        value["frame_rate_num"] is None
        and value["frame_rate_den"] is None
        or value["frame_rate_num"] is not None
        and value["frame_rate_den"] is not None
        and value["frame_rate_num"] <= 240 * value["frame_rate_den"]
    )


def _packet_time(value: Any) -> bool:
    return type(value) in {int, float} and 0 < value <= 120 and math.isfinite(value)


_audio = _object(
    codec=_enum("opus", "aac", "pcm_s16le"),
    sample_rate=_nullable(_integer(1, 192000)),
    channels=_nullable(_integer(1, 2)),
    rtp_clock_rate=_nullable(_integer(1, 192000)),
    packet_time_ms=_nullable(_packet_time),
)
_source_format = _nullable(
    _object(
        video=_nullable(_video),
        audio=_nullable(_audio),
        capture_attempt=_nullable(_positive),
        config_generation=_nullable(_positive),
    )
)


_device = _object(
    scope=_scope,
    name=lambda value: _display_text(value, 256, empty=True),
    family=lambda value: _display_text(value, 128, empty=True),
    kind=lambda value: _display_text(value, 128, empty=True),
    model=lambda value: _display_text(value, 128, empty=True),
    capabilities=_capabilities,
    connection_state=_nullable(_boolean),
    battery_percent=_nullable(_integer(0, 100)),
    observed_format=_source_format,
)


def _inventory(value: Any) -> bool:
    if not _object(
        scope=_account,
        revision=_revision,
        cursor=_nullable(_uuid),
        next_cursor=_nullable(_uuid),
        total=_integer(0, MAX_INVENTORY_DEVICES),
        complete=_boolean,
        devices=lambda devices: (
            isinstance(devices, list)
            and len(devices) <= MAX_INVENTORY_PAGE
            and all(_device(device) for device in devices)
        ),
    )(value):
        return False
    devices = value["devices"]
    identities = [item["scope"]["camera_id"] for item in devices]
    return (
        len(devices) <= value["total"]
        and len(set(identities)) == len(identities)
        and all(
            item["scope"][key] == expected
            for item in devices
            for key, expected in value["scope"].items()
        )
        and (value["next_cursor"] is None or bool(devices))
        and (value["next_cursor"] is None or value["next_cursor"] != value["cursor"])
    )


def _snapshot_request(value: Any) -> bool:
    return (
        _object(
            scope=_scope,
            job_id=_uuid,
            width=_integer(1, 4096),
            height=_integer(1, 4096),
            reason=_enum("periodic", "event", "explicit", "unknown"),
        )(value)
        and value["width"] * value["height"] <= 8_000_000
    )


def _snapshot_result(value: Any) -> bool:
    return _object(
        scope=_scope,
        job_id=_uuid,
        status=_enum("ready", "rejected", "cancelled"),
        code=_code,
        transfer_id=_nullable(_uuid),
    )(value) and (
        (value["status"] == "ready") == (value["transfer_id"] is not None)
        and (value["status"] == "ready") == (value["code"] == "ok")
    )


def _lease_result(value: Any) -> bool:
    return _object(
        scope=_scope,
        session_id=_uuid,
        status=_enum("accepted", "rejected", "expired"),
        code=_code,
        ttl_ms=_integer(0, 30000),
    )(value) and ((value["status"] == "accepted") == (value["ttl_ms"] > 0))


def _transfer_open(value: Any) -> bool:
    return _object(
        scope=_scope,
        operation_id=_uuid,
        transfer_id=_uuid,
        kind=_enum(*BULK_LIMITS),
        total_bytes=_integer(1, 8 * 1024 * 1024),
        chunk_bytes=_integer(1, MAX_BULK_CHUNK_BYTES),
        window_chunks=_integer(1, MAX_BULK_WINDOW_CHUNKS),
    )(value) and (
        value["total_bytes"] <= BULK_LIMITS[value["kind"]]
        and value["chunk_bytes"] <= min(MAX_BULK_CHUNK_BYTES, value["total_bytes"])
        and value["chunk_bytes"] * value["window_chunks"] <= MAX_BULK_WINDOW_BYTES
    )


def _epoch(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None


def _username(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9A-F][26AE](?::[0-9A-F]{2}){5}", value) is not None
    )


_audio_preferences = _object(
    microphone_muted=_boolean,
    speaker_muted=_boolean,
    microphone_volume=_integer(0, 100),
    speaker_volume=_integer(0, 100),
)
_audio_birth_fields = _object(
    microphone=_boolean,
    speaker=_boolean,
    revision=_revision,
    microphone_muted=_boolean,
    speaker_muted=_boolean,
    microphone_volume=_integer(0, 100),
    speaker_volume=_integer(0, 100),
)


def _audio_birth(value):
    return _audio_birth_fields(value) and (not value["speaker"] or value["microphone"])


_recording_video_fields = _object(
    width=lambda value: _integer(2, 1920)(value) and value % 2 == 0,
    height=lambda value: _integer(2, 1080)(value) and value % 2 == 0,
    fps=_integer(1, 30),
    profile=_enum("baseline", "main", "high"),
    level=_enum("3.1", "3.2", "4.0"),
    max_bit_rate=_integer(1, 20000),
)


def _recording_video(value):
    if not _recording_video_fields(value):
        return False
    maximum_frame, maximum_rate = {
        "3.1": (3600, 108000),
        "3.2": (5120, 216000),
        "4.0": (8192, 245760),
    }[value["level"]]
    macroblocks = math.ceil(value["width"] / 16) * math.ceil(value["height"] / 16)
    return (
        macroblocks <= maximum_frame
        and macroblocks * value["fps"] <= maximum_rate
        and (
            value["level"] != "3.1"
            or value["max_bit_rate"] <= (17500 if value["profile"] == "high" else 14000)
        )
    )


_recording_audio = _object(
    sample_rate=lambda value: _integer(16000, 48000)(value) and value in {16000, 24000, 48000},
    bit_rate=_integer(16000, 128000),
    channels=_integer(1, 1),
    bitrate_mode=_enum("vbr"),
)
_recording_configuration_fields = _object(
    video=_recording_video,
    audio=_nullable(_recording_audio),
    fragment_length_ms=_integer(4000, 4000),
    i_frame_interval_ms=_integer(1, 4000),
)


def _recording_configuration(value):
    return _recording_configuration_fields(value) and (
        value["i_frame_interval_ms"] * value["video"]["fps"] >= 1000
    )


_accessory_recording = _object(
    revision=_revision,
    enabled=_boolean,
    active=_boolean,
    audio_active=_boolean,
    configuration=_nullable(_recording_configuration),
)
_accessory_entry_fields = _object(
    camera_id=lambda value: _display_text(value, 128),
    accessory_id=_uuid,
    username=_username,
    doorbell=_boolean,
    audio=_nullable(_audio_birth),
)


def _accessory_entry(value):
    if not isinstance(value, dict):
        return False
    if "recording" not in value:
        return _accessory_entry_fields(value)
    return _accessory_recording(value["recording"]) and _accessory_entry_fields(
        {key: item for key, item in value.items() if key != "recording"}
    )


_accessory_fields = _object(
    scope=_nullable(_account),
    revision=_revision,
    generation=_positive,
    page_index=_integer(0, 7),
    total_pages=_integer(1, 8),
    entries=lambda value: (
        isinstance(value, list)
        and len(value) <= 32
        and all(_accessory_entry(item) for item in value)
    ),
    media_ready=_boolean,
)


def _accessory_config(value):
    return (
        _accessory_fields(value)
        and value["page_index"] < value["total_pages"]
        and (
            value["scope"] is not None
            or not value["entries"]
            and value["page_index"] == 0
            and value["total_pages"] == 1
        )
        and all(
            len({item[key] for item in value["entries"]}) == len(value["entries"])
            for key in ("camera_id", "accessory_id", "username")
        )
    )


_RESOURCE = dict(scope=_scope, session_id=_uuid)
_TRANSFER = dict(scope=_scope, operation_id=_uuid, transfer_id=_uuid)


def _key_bytes(length):
    def valid(value):
        if not isinstance(value, str) or len(value) != 4 * ((length + 2) // 3):
            return False
        try:
            raw = base64.b64decode(value, validate=True)
            return len(raw) == length and base64.b64encode(raw).decode("ascii") == value
        except (ValueError, binascii.Error):
            return False

    return valid


def _address(value):
    # Structural only: LiveManager must validate literal unicast/family/interface.
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(33 <= ord(char) <= 126 for char in value)
    )


_video_configuration = _object(
    width=_integer(2, 1920),
    height=_integer(2, 1080),
    fps=_integer(1, 30),
    profile=_enum("baseline", "main", "high"),
    level=_enum("3.1", "3.2", "4.0"),
    max_bit_rate=_integer(1, 8000),
    payload_type=_integer(96, 127),
    rtcp_interval=lambda value: (
        type(value) in {int, float} and math.isfinite(value) and 0.1 <= value <= 10
    ),
    mtu=_integer(256, 1378),
)
_audio_endpoint_request = _object(
    target_port=_integer(1, 65535),
    key=_key_bytes(16),
    salt=_key_bytes(14),
)
_audio_endpoint = _object(
    port=_integer(1, 65535),
    ssrc=_integer(1, 2**32 - 1),
    key=_key_bytes(16),
    salt=_key_bytes(14),
)
_audio_configuration = _object(
    sample_rate=lambda value: _integer(8000, 24000)(value) and value in {8000, 16000, 24000},
    packet_time=lambda value: _integer(20, 60)(value) and value in {20, 30, 40, 60},
    bitrate_mode=_enum("cbr", "vbr"),
    max_bit_rate=_integer(6, 64),
    payload_type=_integer(96, 127),
    channels=lambda value: _integer(1, 1)(value),
    rtcp_interval=lambda value: (
        type(value) in {int, float} and math.isfinite(value) and 0.1 <= value <= 10
    ),
    clock_mode=_enum("classic_hap"),
)
_video_endpoint = _object(
    address=_address,
    port=_integer(1, 65535),
    ssrc=_integer(1, 2**32 - 1),
    key=_key_bytes(16),
    salt=_key_bytes(14),
    audio=_nullable(_audio_endpoint),
)
_video_result_fields = _object(**_RESOURCE, status=_enum("accepted", "rejected"), code=_code)
_video_prepared = _object(
    **_RESOURCE,
    status=_enum("accepted", "rejected"),
    code=_code,
    endpoint=_nullable(_video_endpoint),
)


def _prepared_result(value):
    return _video_prepared(value) and (
        value["status"] == "accepted"
        and value["code"] == "ok"
        and value["endpoint"] is not None
        or value["status"] == "rejected"
        and value["code"] != "ok"
        and value["endpoint"] is None
    )


def _video_result(value):
    return _video_result_fields(value) and (
        (value["status"] == "accepted") == (value["code"] == "ok")
    )


_recording_job = dict(scope=_scope, job_id=_uuid)
_recording_next_fields = _object(
    **_recording_job,
    status=_enum("ready", "eos", "rejected"),
    code=_code,
    transfer_id=_nullable(_uuid),
    kind=_nullable(_enum("init", "fragment")),
    sequence=_integer(0, 2**32 - 1),
    total_bytes=_integer(0, BULK_LIMITS["recording_fragment"]),
    duration_ms=_integer(0, 4000),
)


def _recording_next_result(value):
    if not _recording_next_fields(value):
        return False
    if value["status"] != "ready":
        return (
            (value["status"] == "eos") == (value["code"] == "ok")
            and value["transfer_id"] is None
            and value["kind"] is None
            and value["total_bytes"] == value["duration_ms"] == 0
        )
    if value["code"] != "ok" or value["transfer_id"] is None or value["kind"] is None:
        return False
    if value["kind"] == "init":
        return (
            value["sequence"] == 0
            and 1 <= value["total_bytes"] <= BULK_LIMITS["recording_init"]
            and value["duration_ms"] == 0
        )
    return value["sequence"] >= 1 and value["total_bytes"] >= 1 and value["duration_ms"] >= 1


PAYLOAD_VALIDATORS = {
    "recording_settings_update": _object(
        scope=_scope,
        expected_revision=_revision,
        active=_boolean,
        audio_active=_boolean,
        configuration=_nullable(_recording_configuration),
    ),
    "recording_settings_result": lambda value: (
        _object(scope=_scope, accepted=_boolean, code=_code, revision=_revision)(value)
        and value["accepted"] == (value["code"] == "ok")
    ),
    "recording_open": _object(**_recording_job, configuration=_recording_configuration),
    "recording_open_result": lambda value: (
        _object(**_recording_job, status=_enum("accepted", "rejected"), code=_code)(value)
        and (value["status"] == "accepted") == (value["code"] == "ok")
    ),
    "recording_next": _object(**_recording_job, sequence=_integer(0, 2**32 - 1)),
    "recording_next_result": _recording_next_result,
    "recording_stop": _object(**_recording_job),
    "recording_stop_result": lambda value: (
        _object(**_recording_job, status=_enum("stopped", "rejected"), code=_code)(value)
        and (value["status"] == "stopped") == (value["code"] == "ok")
    ),
    "audio_settings_update": _object(
        **dict(scope=_scope), expected_revision=_revision, settings=_audio_preferences
    ),
    "audio_settings_result": lambda value: (
        _object(scope=_scope, accepted=_boolean, code=_code, revision=_revision)(value)
        and (value["accepted"] == (value["code"] == "ok"))
    ),
    "video_prepare": _object(
        **_RESOURCE,
        source_address=_address,
        target_address=_address,
        address_version=_enum("ipv4", "ipv6"),
        target_port=_integer(1, 65535),
        key=_key_bytes(16),
        salt=_key_bytes(14),
        audio=_nullable(_audio_endpoint_request),
    ),
    "video_prepare_result": _prepared_result,
    "video_start": _object(
        **_RESOURCE, config=_video_configuration, audio_config=_nullable(_audio_configuration)
    ),
    "video_start_result": _video_result,
    "video_reconfigure": _object(**_RESOURCE, config=_video_configuration),
    "video_reconfigure_result": _video_result,
    "video_stop": _object(**_RESOURCE),
    "video_stop_result": _video_result,
    "video_ended": _object(**_RESOURCE, code=_code),
    "accessory_config": _accessory_config,
    "accessory_config_ack": _object(
        scope=_nullable(_account),
        revision=_revision,
        generation=_positive,
        page_index=_integer(0, 7),
        accepted=_boolean,
        code=_code,
    ),
    "event_sync": _object(scope=_nullable(_account), epoch=_nullable(_epoch), sequence=_revision),
    "event_sync_ack": _object(
        scope=_nullable(_account), epoch=_epoch, sequence=_revision, accepted=_boolean, code=_code
    ),
    "event_state": _object(
        scope=_scope,
        epoch=_epoch,
        sequence=_revision,
        motion=_nullable(_boolean),
        ttl_ms=_integer(0, 30000),
        expires_at_ms=_revision,
        source_state=_enum("connected", "connecting", "offline"),
        synchronized=_boolean,
    ),
    "camera_event": _object(
        scope=_scope,
        epoch=_epoch,
        sequence=_revision,
        event_id=_uuid,
        type=_enum("doorbell"),
        ttl_ms=_integer(1, 2000),
        expires_at_ms=_revision,
    ),
    "hello": _object(
        role=_enum("python"),
        build_id=lambda value: _bounded_text(value, 128),
        protocol=_integer(1, 1),
    ),
    "hello_ack": _object(
        role=_enum("hap"),
        build_id=lambda value: _bounded_text(value, 128),
        protocol=_integer(1, 1),
        features=lambda value: value == list(FEATURES),
        pid=_positive,
    ),
    "ping": _object(),
    "pong": _object(),
    "shutdown": _object(),
    "shutdown_ack": _object(),
    "status": _object(
        state=_enum("ready", "degraded"), reason=lambda value: _bounded_text(value, 256, empty=True)
    ),
    "error": _object(
        code=lambda value: _bounded_text(value, 64),
        message=lambda value: _bounded_text(value, 256, empty=True),
    ),
    "inventory_reset": _object(scope=_nullable(_account), revision=_revision),
    "inventory_request": _object(scope=_account, revision=_revision, cursor=_nullable(_uuid)),
    "inventory_page": _inventory,
    "camera_status": _object(
        scope=_scope, revision=_revision, state=_enum("online", "offline", "unknown"), code=_code
    ),
    "policy_update": _object(scope=_scope, policy=_policy),
    "policy_ack": _object(scope=_scope, accepted=_boolean, code=_code),
    "lease_request": _object(**_RESOURCE, purpose=_enum("homekit_live"), ttl_ms=_integer(1, 30000)),
    "lease_result": _lease_result,
    "lease_renew": _object(**_RESOURCE, ttl_ms=_integer(1, 30000)),
    "lease_renew_result": _lease_result,
    "lease_stop": _object(**_RESOURCE),
    "lease_stop_result": _object(**_RESOURCE, status=_enum("stopped", "rejected"), code=_code),
    "snapshot_request": _snapshot_request,
    "snapshot_result": _snapshot_result,
    "snapshot_cancel": _object(scope=_scope, job_id=_uuid),
    "talkback_request": _object(**_RESOURCE, action=_enum("acquire", "stop")),
    "talkback_result": _object(
        **_RESOURCE, status=_enum("accepted", "stopped", "rejected"), code=_code
    ),
    "control_request": _object(
        scope=_scope, operation_id=_uuid, control=_enum("light", "siren"), enabled=_boolean
    ),
    "control_result": _object(
        scope=_scope,
        operation_id=_uuid,
        status=_enum("accepted", "rejected", "unknown"),
        code=_code,
    ),
    "transfer_open": _transfer_open,
    "transfer_credit": _object(
        **_TRANSFER,
        next_sequence=_integer(0, 2**32 - 1),
        credits=_integer(1, MAX_BULK_WINDOW_CHUNKS),
    ),
    "transfer_end": _object(
        **_TRANSFER, sequence_count=_integer(1, 2**32 - 1), total_bytes=_integer(1, 8 * 1024 * 1024)
    ),
    "transfer_cancel": _object(
        **_TRANSFER, code=_enum("cancelled", "invalidated", "timeout", "protocol-error")
    ),
    "transfer_fence": _object(
        **_TRANSFER,
        sequence_count=_integer(0, 2**32 - 1),
        sent_bytes=_integer(0, 8 * 1024 * 1024),
        code=_enum("cancelled", "invalidated", "timeout"),
    ),
    "transfer_fence_ack": _object(
        **_TRANSFER,
        sequence_count=_integer(0, 2**32 - 1),
        sent_bytes=_integer(0, 8 * 1024 * 1024),
        code=_enum("cancelled", "invalidated", "timeout"),
    ),
    "transfer_result": _object(
        **_TRANSFER, status=_enum("complete", "cancelled", "rejected"), code=_code
    ),
}
KINDS = frozenset(PAYLOAD_VALIDATORS)
PYTHON_KINDS = frozenset(
    {
        "recording_settings_result",
        "recording_open_result",
        "recording_next_result",
        "recording_stop_result",
        "audio_settings_result",
        "video_prepare_result",
        "video_start_result",
        "video_reconfigure_result",
        "video_stop_result",
        "video_ended",
        "accessory_config",
        "event_sync_ack",
        "event_state",
        "camera_event",
        "hello",
        "ping",
        "shutdown",
        "inventory_reset",
        "inventory_page",
        "camera_status",
        "policy_ack",
        "lease_result",
        "lease_renew_result",
        "lease_stop_result",
        "snapshot_result",
        "talkback_result",
        "control_result",
    }
)
HAP_KINDS = frozenset(
    {
        "recording_settings_update",
        "recording_open",
        "recording_next",
        "recording_stop",
        "audio_settings_update",
        "video_prepare",
        "video_start",
        "video_reconfigure",
        "video_stop",
        "accessory_config_ack",
        "event_sync",
        "hello_ack",
        "pong",
        "shutdown_ack",
        "inventory_request",
        "policy_update",
        "lease_request",
        "lease_renew",
        "lease_stop",
        "snapshot_request",
        "snapshot_cancel",
        "talkback_request",
        "control_request",
    }
)
BIDIRECTIONAL_KINDS = KINDS - PYTHON_KINDS - HAP_KINDS


def validate_scope(value: Any) -> dict[str, Any]:
    """Validate shape only; the controller must compare current authority at admission/delivery."""
    if not _scope(value):
        raise HomeKitProtocolError("invalid media scope")
    return value


def validate_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "v",
        "kind",
        "boot_id",
        "request_id",
        "payload",
    }:
        raise HomeKitProtocolError("invalid control envelope")
    if not _safe_integer(value["v"]) or value["v"] != PROTOCOL_VERSION:
        raise HomeKitProtocolError("unsupported control protocol")
    if not _uuid(value["boot_id"]) or not _uuid(value["request_id"]):
        raise HomeKitProtocolError("invalid control correlation identity")
    kind = value["kind"]
    if not isinstance(kind, str) or kind not in KINDS:
        raise HomeKitProtocolError("unsupported control message")
    if not PAYLOAD_VALIDATORS[kind](value["payload"]):
        raise HomeKitProtocolError("invalid control payload")
    return value


def validate_direction(envelope: dict[str, Any], sender: str) -> dict[str, Any]:
    """Additional S06 admission check, independent of structural decoding."""
    validate_envelope(envelope)
    allowed = PYTHON_KINDS if sender == "python" else HAP_KINDS if sender == "hap" else None
    if allowed is None or envelope["kind"] not in allowed | BIDIRECTIONAL_KINDS:
        raise HomeKitProtocolError("invalid control direction")
    if envelope["kind"] == "transfer_open":
        producer = "hap" if envelope["payload"]["kind"] == "talkback_pcm" else "python"
        if sender != producer:
            raise HomeKitProtocolError("invalid bulk direction")
    return envelope


REPLY_KINDS = {
    "recording_settings_update": "recording_settings_result",
    "recording_open": "recording_open_result",
    "recording_next": "recording_next_result",
    "recording_stop": "recording_stop_result",
    "audio_settings_update": "audio_settings_result",
    "video_prepare": "video_prepare_result",
    "video_start": "video_start_result",
    "video_reconfigure": "video_reconfigure_result",
    "video_stop": "video_stop_result",
    "accessory_config": "accessory_config_ack",
    "event_sync": "event_sync_ack",
    "hello": "hello_ack",
    "ping": "pong",
    "shutdown": "shutdown_ack",
    "inventory_request": "inventory_page",
    "policy_update": "policy_ack",
    "lease_request": "lease_result",
    "lease_renew": "lease_renew_result",
    "lease_stop": "lease_stop_result",
    "snapshot_request": "snapshot_result",
    "talkback_request": "talkback_result",
    "control_request": "control_result",
    "transfer_open": "transfer_credit",
    "transfer_end": "transfer_result",
    "transfer_fence": "transfer_fence_ack",
}


def validate_reply(request: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
    """Pure correlation check; S06 must also own/retire a bounded pending-request map.

    A structurally valid unsolicited result is never authority. In particular a
    policy ACK cannot acknowledge another privacy epoch or a subset of its scope.
    """
    validate_envelope(request)
    validate_envelope(reply)
    if (
        REPLY_KINDS.get(request["kind"]) != reply["kind"]
        or request["boot_id"] != reply["boot_id"]
        or request["request_id"] != reply["request_id"]
    ):
        raise HomeKitProtocolError("invalid control reply correlation")
    sent, received = request["payload"], reply["payload"]
    if (
        request["kind"] in {"audio_settings_update", "recording_settings_update"}
        and received["accepted"]
        and received["revision"]
        not in (
            sent["expected_revision"],
            sent["expected_revision"] + 1,
        )
    ):
        raise HomeKitProtocolError("invalid audio settings revision correlation")
    if request["kind"] == "recording_next" and sent["sequence"] != received["sequence"]:
        raise HomeKitProtocolError("invalid recording sequence correlation")
    if request["kind"] == "video_prepare" and received["status"] == "accepted":
        endpoint = received["endpoint"]
        if (
            any(endpoint[key] != sent[key] for key in ("key", "salt"))
            or ((sent["audio"] is None) != (endpoint["audio"] is None))
            or (
                sent["audio"] is not None
                and any(endpoint["audio"][key] != sent["audio"][key] for key in ("key", "salt"))
            )
        ):
            raise HomeKitProtocolError("invalid negotiated track key correlation")
    if request["kind"] == "transfer_fence" and any(
        sent[key] != received[key] for key in ("sequence_count", "sent_bytes", "code")
    ):
        raise HomeKitProtocolError("invalid drained frontier correlation")
    for key in ("scope", "session_id", "job_id", "operation_id", "transfer_id"):
        if key in sent and sent[key] != received.get(key):
            raise HomeKitProtocolError("invalid control reply ownership")
    if request["kind"] == "accessory_config" and any(
        sent[key] != received[key] for key in ("revision", "generation", "page_index")
    ):
        raise HomeKitProtocolError("invalid accessory configuration correlation")
    if request["kind"] == "inventory_request" and (
        sent["cursor"] != received["cursor"]
        or sent["revision"] != 0
        and sent["revision"] != received["revision"]
    ):
        raise HomeKitProtocolError("invalid inventory reply revision")
    return reply


def encode_bulk_header(kind: str, transfer_id: str, sequence: int, length: int) -> bytes:
    """Metadata only; no bulk sender/receiver or allocation of the advertised body."""
    if (
        not isinstance(kind, str)
        or kind not in BULK_KINDS
        or not _uuid(transfer_id)
        or not _integer(0, 2**32 - 1)(sequence)
        or not _integer(1, min(MAX_BULK_CHUNK_BYTES, BULK_LIMITS[kind]))(length)
    ):
        raise HomeKitProtocolError("invalid bulk header")
    return struct.pack(
        "!4sBBH16sII",
        b"HBLK",
        1,
        BULK_KINDS[kind],
        0,
        uuid.UUID(transfer_id).bytes,
        int(sequence),
        int(length),
    )


def decode_bulk_header(header: bytes) -> dict[str, Any]:
    if len(header) != BULK_HEADER_BYTES:
        raise HomeKitProtocolError("invalid bulk header length")
    magic, version, kind_id, reserved, identity, sequence, length = struct.unpack(
        "!4sBBH16sII", header
    )
    kinds = {index: name for name, index in BULK_KINDS.items()}
    if magic != b"HBLK" or version != 1 or reserved != 0 or kind_id not in kinds:
        raise HomeKitProtocolError("invalid bulk header")
    kind, transfer_id = kinds[kind_id], str(uuid.UUID(bytes=identity))
    encode_bulk_header(kind, transfer_id, sequence, length)
    return {"kind": kind, "transfer_id": transfer_id, "sequence": sequence, "length": length}


def encode_frame(envelope: dict[str, Any]) -> bytes:
    validate_envelope(envelope)
    try:
        body = json.dumps(
            envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (ValueError, TypeError, UnicodeError) as exc:
        raise HomeKitProtocolError("control message is not finite UTF-8 JSON") from exc
    if not 0 < len(body) <= MAX_CONTROL_BYTES:
        raise HomeKitProtocolError("control message is too large")
    decode_body(body)
    return struct.pack("!I", len(body)) + body


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HomeKitProtocolError("duplicate JSON field")
        result[key] = value
    return result


def _check_depth(value: Any, depth: int = 0) -> None:
    if isinstance(value, (dict, list)):
        depth += 1
        if depth > MAX_JSON_DEPTH:
            raise HomeKitProtocolError("control JSON nesting is too deep")
        for item in value.values() if isinstance(value, dict) else value:
            _check_depth(item, depth)
    elif isinstance(value, float) and not math.isfinite(value):
        raise HomeKitProtocolError("non-finite control JSON")


def decode_body(body: bytes) -> dict[str, Any]:
    if not 0 < len(body) <= MAX_CONTROL_BYTES:
        raise HomeKitProtocolError("invalid control frame length")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
        _check_depth(value)
        return validate_envelope(value)
    except (ValueError, UnicodeError, RecursionError, TypeError) as exc:
        raise HomeKitProtocolError("invalid control JSON") from exc


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    timeout: float = IO_TIMEOUT_SECONDS,
    first_byte: bytes | None = None,
) -> dict:
    async with asyncio.timeout(timeout):
        if first_byte is not None and len(first_byte) != 1:
            raise ValueError("frame prefix must contain exactly one byte")
        header = (
            await reader.readexactly(4)
            if first_byte is None
            else first_byte + await reader.readexactly(3)
        )
        length = struct.unpack("!I", header)[0]
        if not 0 < length <= MAX_CONTROL_BYTES:
            raise HomeKitProtocolError("invalid control frame length")
        return decode_body(await reader.readexactly(length))


async def write_frame(
    writer: asyncio.StreamWriter, envelope: dict, *, timeout: float = IO_TIMEOUT_SECONDS
) -> None:
    frame = encode_frame(envelope)
    async with asyncio.timeout(timeout):
        writer.write(frame)
        await writer.drain()
