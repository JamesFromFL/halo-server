"""Strict bounded framing for the inherited private HomeKit control channel."""

from __future__ import annotations

import asyncio
import json
import math
import struct
import uuid
from typing import Any

PROTOCOL_VERSION = 1
MAX_CONTROL_BYTES = 65536
MAX_JSON_DEPTH = 32
IO_TIMEOUT_SECONDS = 5.0
KINDS = frozenset(
    {"hello", "hello_ack", "ping", "pong", "shutdown", "shutdown_ack", "status", "error"}
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


def _safe_integer(value: Any) -> bool:
    return (
        type(value) in {int, float}
        and abs(value) < 2**53
        and math.isfinite(value)
        and value == int(value)
    )


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
    kind, payload = value["kind"], value["payload"]
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(payload, dict):
        raise HomeKitProtocolError("unsupported control message")
    valid = False
    if kind in {"ping", "pong", "shutdown", "shutdown_ack"}:
        valid = not payload
    elif kind in {"hello", "hello_ack"}:
        expected = {"role", "build_id", "protocol"}
        if kind == "hello_ack":
            expected |= {"features", "pid"}
        valid = (
            set(payload) == expected
            and payload["role"] == ("python" if kind == "hello" else "hap")
            and _bounded_text(payload["build_id"], 128)
            and _safe_integer(payload["protocol"])
            and payload["protocol"] == 1
        )
        if valid and kind == "hello_ack":
            valid = (
                payload["features"] == ["classic-camera-control-v1"]
                and _safe_integer(payload["pid"])
                and 1 <= payload["pid"] < 2**53
            )
    elif kind == "status":
        valid = (
            set(payload) == {"state", "reason"}
            and isinstance(payload["state"], str)
            and payload["state"] in {"ready", "degraded"}
            and _bounded_text(payload["reason"], 256, empty=True)
        )
    elif kind == "error":
        valid = (
            set(payload) == {"code", "message"}
            and _bounded_text(payload["code"], 64)
            and _bounded_text(payload["message"], 256, empty=True)
        )
    if not valid:
        raise HomeKitProtocolError("invalid control payload")
    return value


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
    return struct.pack("!I", len(body)) + body


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HomeKitProtocolError("duplicate JSON field")
        result[key] = value
    return result


def _check_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise HomeKitProtocolError("control JSON nesting is too deep")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, depth + 1)


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
