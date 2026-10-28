import asyncio
import json
import struct
import uuid
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator

from halo_server.homekit.ipc import (
    MAX_CONTROL_BYTES,
    HomeKitProtocolError,
    decode_body,
    encode_frame,
    read_frame,
)
from halo_server.homekit.models import CameraPolicy, MediaScope


def hello():
    return {
        "v": 1,
        "kind": "hello",
        "boot_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "payload": {"role": "python", "build_id": "test-1", "protocol": 1},
    }


def test_private_schema_fixtures_match_actual_python_decoder():
    root = files("halo_server.homekit").joinpath("protocol", "v1")
    schema = json.loads(root.joinpath("schema.json").read_text())
    fixtures = json.loads(root.joinpath("fixtures.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for envelope in fixtures["valid"]:
        validator.validate(envelope)
        assert decode_body(json.dumps(envelope).encode()) == envelope
    for envelope in fixtures["invalid"]:
        assert not validator.is_valid(envelope)
        with pytest.raises(HomeKitProtocolError):
            decode_body(json.dumps(envelope).encode())


def test_control_roundtrip_and_duplicate_fields_are_unambiguous():
    envelope = hello()
    frame = encode_frame(envelope)
    assert struct.unpack("!I", frame[:4])[0] == len(frame) - 4
    assert decode_body(frame[4:]) == envelope
    duplicate = frame[4:].replace(b'"v":1', b'"v":1,"v":1')
    with pytest.raises(HomeKitProtocolError, match="duplicate"):
        decode_body(duplicate)


@pytest.mark.parametrize(
    "mutation",
    [
        {"v": True},
        {"v": 2},
        {"kind": "login"},
        {"boot_id": "old"},
        {"token": "never accepted"},
        {"payload": {"password": "never accepted"}},
    ],
)
def test_control_rejects_unknown_authority_and_protocol_fields(mutation):
    envelope = hello() | mutation
    with pytest.raises(HomeKitProtocolError):
        decode_body(json.dumps(envelope).encode())


@pytest.mark.asyncio
async def test_partial_frames_are_joined_and_oversized_header_fails_before_body_read():
    reader = asyncio.StreamReader()
    envelope = hello()
    frame = encode_frame(envelope)
    task = asyncio.create_task(read_frame(reader))
    for byte in frame:
        reader.feed_data(bytes([byte]))
        await asyncio.sleep(0)
    assert await task == envelope
    reader.feed_data(struct.pack("!I", MAX_CONTROL_BYTES + 1))
    with pytest.raises(HomeKitProtocolError, match="length"):
        await read_frame(reader)


@pytest.mark.asyncio
async def test_frame_total_deadline_covers_partial_header_and_body():
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x00\x00")
    with pytest.raises(TimeoutError):
        await read_frame(reader, timeout=0.01)


@pytest.mark.parametrize(
    "body",
    [
        b'{"v": NaN}',
        b"\xff",
        b"[" * 1000 + b"]" * 1000,
        b"",
        b"{}",
    ],
)
def test_control_rejects_nonfinite_invalid_utf8_deep_or_empty_json(body):
    with pytest.raises(HomeKitProtocolError):
        decode_body(body)


def test_policy_is_fail_closed_until_current_adapter_state_is_synchronized():
    assert not CameraPolicy(streaming=True).permits("streaming")
    assert CameraPolicy(synchronized=True, streaming=True).permits("streaming")
    with pytest.raises(ValueError):
        CameraPolicy(recording_audio=True)
    with pytest.raises(ValueError):
        CameraPolicy(streaming=1)


def test_scope_requires_opaque_profile_and_safe_generation_numbers():
    scope = MediaScope(str(uuid.uuid4()), "42", 1, 1, 1)
    assert scope.camera_id == "42"
    for value in (True, 0, -1, 2**53):
        with pytest.raises(ValueError):
            MediaScope(scope.profile_id, "42", value, 1, 1)
