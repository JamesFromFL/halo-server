"""Versioned metadata-only protocol for the isolated non-camera HAP child.

Only explicitly enabled, scope-bound alarm modes can be requested. Panic, bypass,
credentials, camera sessions and media bytes cannot be expressed. Cumulative
snapshots are bounded to one HomeKit bridge capacity.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import struct
import uuid

from halo_server.homekit.ipc import pairing_manifest_revision
from halo_server.homekit.text import literal_text

MAX_CONTROL_BYTES = 256 * 1024
MAX_CHILDREN = 149
IO_TIMEOUT_SECONDS = 5.0
MAPPINGS = frozenset({"contact", "motion", "leak", "smoke", "carbon_monoxide"})
DEVICE_SERVICE_MAPPINGS = MAPPINGS | {"security_system"}
ALARM_MODES = frozenset({"disarmed", "home", "away"})
CODES = frozenset(
    {
        "ok",
        "not-ready",
        "stale-scope",
        "conflict",
        "storage-recovery-required",
        "unavailable",
        "invalid-request",
        "busy",
        "policy-denied",
        "uncertain",
    }
)
REPLIES = {
    "hello": "hello_ack",
    "ping": "pong",
    "publish": "publish_result",
    "status": "status_result",
    "reveal": "reveal_result",
    "reset_prepare": "reset_prepare_result",
    "shutdown": "shutdown_ack",
}


class DeviceBridgeProtocolError(RuntimeError):
    """The private bridge channel violated its bounded metadata contract."""


def _integer(value, minimum=0, maximum=2**53 - 1):
    return (
        type(value) in (int, float)
        and abs(value) < 2**53
        and math.isfinite(value)
        and value == int(value)
        and minimum <= value <= maximum
    )


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _uuid4(value):
    return _uuid(value) and uuid.UUID(value).version == 4


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _username(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9A-F][26AE](?::[0-9A-F]{2}){5}", value) is not None
    )


def _fields(value, expected):
    return isinstance(value, dict) and set(value) == set(expected)


def _scope(value):
    return (
        _fields(value, {"profile_id", "account_generation"})
        and _uuid(value["profile_id"])
        and _integer(value["account_generation"], 1)
    )


IDENTITY_FIELDS = {"scope", "registry_revision", "accessory_uuid", "username"}


def _identity(value):
    return (
        _scope(value["scope"])
        and _integer(value["registry_revision"])
        and _uuid4(value["accessory_uuid"])
        and _username(value["username"])
    )


def _child(value):
    if not _fields(
        value,
        {
            "device_id",
            "accessory_uuid",
            "name",
            "model",
            "mappings",
            "available",
            "values",
            "alarm",
        },
    ):
        return False
    mappings, values = value["mappings"], value["values"]
    alarm = value["alarm"]
    if isinstance(mappings, list) and "security_system" in mappings:
        if mappings != ["security_system"] or not _fields(
            alarm, {"mode", "triggered", "control_enabled"}
        ):
            return False
        if (
            alarm["mode"] is not None
            and (not isinstance(alarm["mode"], str) or alarm["mode"] not in ALARM_MODES)
            or alarm["triggered"] is not None
            and type(alarm["triggered"]) is not bool
            or type(alarm["control_enabled"]) is not bool
            or not value["available"]
            and (
                alarm["mode"] is not None
                or alarm["triggered"] is not None
                or alarm["control_enabled"]
            )
        ):
            return False
    elif alarm is not None:
        return False
    return (
        _hash(value["device_id"])
        and _uuid4(value["accessory_uuid"])
        and literal_text(value["name"], 64, empty=False)
        and literal_text(value["model"], 64)
        and isinstance(mappings, list)
        and 1 <= len(mappings) <= len(DEVICE_SERVICE_MAPPINGS)
        and all(isinstance(item, str) and item in DEVICE_SERVICE_MAPPINGS for item in mappings)
        and sorted(set(mappings)) == mappings
        and type(value["available"]) is bool
        and _fields(values, MAPPINGS)
        and all(item is None or type(item) is bool for item in values.values())
        and all(values[key] is None for key in MAPPINGS.difference(mappings))
        and (value["available"] or all(item is None for item in values.values()))
    )


def _publish(value):
    if not _fields(
        value, {"scope", "registry_revision", "sequence", "expires_at_ms", "bridge", "children"}
    ):
        return False
    bridge, children = value["bridge"], value["children"]
    return (
        _scope(value["scope"])
        and _integer(value["registry_revision"])
        and _integer(value["sequence"], 1)
        and _integer(value["expires_at_ms"], 1)
        and _fields(bridge, {"accessory_uuid", "username", "name"})
        and _uuid4(bridge["accessory_uuid"])
        and _username(bridge["username"])
        and literal_text(bridge["name"], 64, empty=False)
        and isinstance(children, list)
        and len(children) <= MAX_CHILDREN
        and all(_child(child) for child in children)
        and len({child["device_id"] for child in children}) == len(children)
        and len({child["accessory_uuid"] for child in children}) == len(children)
        and all(child["accessory_uuid"] != bridge["accessory_uuid"] for child in children)
    )


def _accepted(value):
    return (
        type(value["accepted"]) is bool
        and isinstance(value["code"], str)
        and value["code"] in CODES
        and value["accepted"] == (value["code"] == "ok")
    )


PAIRING_RESULT_FIELDS = IDENTITY_FIELDS | {
    "accepted",
    "code",
    "published",
    "paired",
    "paired_clients",
    "pairing_revision",
}


def _pairing_result(value):
    paired, count = value["paired"], value["paired_clients"]
    return (
        _identity(value)
        and _accepted(value)
        and type(value["published"]) is bool
        and (paired is None or type(paired) is bool)
        and (count is None or _integer(count, 0, 256))
        and (paired is None and count is None or paired is not None and count is not None)
        and (count is None or paired == (count > 0))
        and (value["pairing_revision"] is None or _hash(value["pairing_revision"]))
        and (not value["accepted"] or paired is not None and _hash(value["pairing_revision"]))
    )


def _manifest(value, revision):
    if not _fields(value, {"accessory_info", "identifier_cache", "controller_storage"}):
        return False
    if any(item is not None and not _hash(item) for item in value.values()):
        return False
    return revision == pairing_manifest_revision(value)


ALARM_FIELDS = {"scope", "registry_revision", "sequence", "accessory_uuid", "device_id", "target"}


def _alarm(value, result=False):
    return (
        _fields(value, ALARM_FIELDS | ({"accepted", "code"} if result else set()))
        and _scope(value["scope"])
        and _integer(value["registry_revision"])
        and _integer(value["sequence"], 1)
        and _uuid4(value["accessory_uuid"])
        and _hash(value["device_id"])
        and isinstance(value["target"], str)
        and value["target"] in ALARM_MODES
        and (not result or _accepted(value))
    )


def _payload(kind, value):
    if not isinstance(value, dict):
        return False
    if kind in {"alarm_set", "alarm_set_result"}:
        return _alarm(value, kind == "alarm_set_result")
    if kind in {"ping", "pong", "shutdown", "shutdown_ack"}:
        return not value
    if kind in {"hello", "hello_ack"}:
        fields = {"role", "build_id", "protocol"} | ({"pid"} if kind == "hello_ack" else set())
        return (
            _fields(value, fields)
            and value["role"] == ("python" if kind == "hello" else "device-hap")
            and literal_text(value["build_id"], 128, empty=False)
            and _integer(value["protocol"], 1, 1)
            and (kind == "hello" or _integer(value["pid"], 1))
        )
    if kind == "publish":
        return _publish(value)
    if kind == "publish_result":
        return (
            _fields(value, {"scope", "registry_revision", "sequence", "accepted", "code"})
            and _scope(value["scope"])
            and _integer(value["registry_revision"])
            and _integer(value["sequence"], 1)
            and _accepted(value)
        )
    if kind in {"status", "reveal", "reset_prepare"}:
        return (
            _fields(value, IDENTITY_FIELDS | ({"pairing_revision"} if kind != "status" else set()))
            and _identity(value)
            and (kind == "status" or _hash(value["pairing_revision"]))
        )
    extra = {
        "status_result": {"pairing_manifest"},
        "reveal_result": {"pincode", "setup_uri", "ttl_ms"},
        "reset_prepare_result": {"pairing_manifest", "quiesced"},
    }.get(kind)
    if (
        extra is None
        or not _fields(value, PAIRING_RESULT_FIELDS | extra)
        or not _pairing_result(value)
    ):
        return False
    if kind == "reveal_result":
        return (
            value["accepted"]
            and value["paired"] is False
            and isinstance(value["pincode"], str)
            and re.fullmatch(r"[0-9]{3}-[0-9]{2}-[0-9]{3}", value["pincode"]) is not None
            and isinstance(value["setup_uri"], str)
            and re.fullmatch(r"X-HM://[0-9A-Z]{13}", value["setup_uri"]) is not None
            and _integer(value["ttl_ms"], 60000, 60000)
            or not value["accepted"]
            and value["pincode"] is None
            and value["setup_uri"] is None
            and _integer(value["ttl_ms"], 0, 0)
        )
    if kind == "reset_prepare_result" and (
        type(value["quiesced"]) is not bool or value["accepted"] and not value["quiesced"]
    ):
        return False
    return (
        _manifest(value["pairing_manifest"], value["pairing_revision"])
        if value["accepted"]
        else value["pairing_manifest"] is None
    )


def validate_envelope(value, *, role=None):
    if (
        not _fields(value, {"v", "kind", "boot_id", "request_id", "payload"})
        or not _integer(value["v"], 1, 1)
        or not _uuid(value["boot_id"])
        or not _uuid(value["request_id"])
        or not isinstance(value["kind"], str)
        or not _payload(value["kind"], value["payload"])
    ):
        raise DeviceBridgeProtocolError("Invalid device bridge control message")
    if role is not None and value["kind"] not in (
        set(REPLIES) | {"alarm_set_result"}
        if role == "python"
        else set(REPLIES.values()) | {"alarm_set"}
    ):
        raise DeviceBridgeProtocolError("Invalid device bridge message direction")
    return value


def validate_reply(request, reply):
    validate_envelope(reply, role="hap")
    if (
        reply["kind"] != REPLIES[request["kind"]]
        or reply["boot_id"] != request["boot_id"]
        or reply["request_id"] != request["request_id"]
    ):
        raise DeviceBridgeProtocolError("Invalid device bridge reply correlation")
    for key in ("scope", "registry_revision", "sequence", "accessory_uuid", "username"):
        if key in request["payload"] and request["payload"][key] != reply["payload"].get(key):
            raise DeviceBridgeProtocolError("Device bridge reply changed authority")
    if (
        "pairing_revision" in request["payload"]
        and reply["payload"].get("accepted")
        and request["payload"]["pairing_revision"] != reply["payload"].get("pairing_revision")
    ):
        raise DeviceBridgeProtocolError("Device bridge reply changed pairing authority")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result or key in {"__proto__", "prototype", "constructor"}:
            raise DeviceBridgeProtocolError("Duplicate or forbidden JSON field")
        result[key] = value
    return result


def decode_body(body: bytes):
    if not 0 < len(body) <= MAX_CONTROL_BYTES:
        raise DeviceBridgeProtocolError("Invalid device bridge frame length")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        pending = [(value, 0)]
        nodes = 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 16 or nodes > 20000:
                raise ValueError()
            if isinstance(item, (dict, list)):
                pending.extend(
                    (child, depth + 1)
                    for child in (item.values() if isinstance(item, dict) else item)
                )
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError()
        return validate_envelope(value)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise DeviceBridgeProtocolError("Invalid device bridge JSON") from error


def encode_frame(envelope):
    validate_envelope(envelope)
    try:
        body = json.dumps(
            envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError, UnicodeError) as error:
        raise DeviceBridgeProtocolError("Invalid device bridge JSON") from error
    decode_body(body)
    return struct.pack("!I", len(body)) + body


async def read_frame(reader, *, first_byte=None, timeout=IO_TIMEOUT_SECONDS):
    async with asyncio.timeout(timeout):
        if first_byte is not None and len(first_byte) != 1:
            raise ValueError("Expected one frame prefix byte")
        header = (
            await reader.readexactly(4)
            if first_byte is None
            else first_byte + await reader.readexactly(3)
        )
        length = struct.unpack("!I", header)[0]
        if not 0 < length <= MAX_CONTROL_BYTES:
            raise DeviceBridgeProtocolError("Invalid device bridge frame length")
        return decode_body(await reader.readexactly(length))


async def write_frame(writer, envelope, *, timeout=IO_TIMEOUT_SECONDS):
    frame = encode_frame(envelope)
    async with asyncio.timeout(timeout):
        writer.write(frame)
        await writer.drain()
