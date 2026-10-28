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


def _private_fixtures():
    return json.loads(
        files("halo_server.homekit").joinpath("protocol", "v1", "fixtures.json").read_text()
    )


def test_semantic_and_raw_fixture_cases_fail_actual_python_decoder():
    fixtures = _private_fixtures()
    for envelope in fixtures["semantic_invalid"]:
        with pytest.raises(HomeKitProtocolError):
            decode_body(json.dumps(envelope).encode())
    for hexadecimal in fixtures["raw_invalid_hex"]:
        with pytest.raises(HomeKitProtocolError):
            decode_body(bytes.fromhex(hexadecimal))


def test_bulk_header_corpus_is_exact_and_rejects_before_body_allocation():
    from halo_server.homekit.ipc import (
        BULK_HEADER_BYTES,
        decode_bulk_header,
        encode_bulk_header,
    )

    fixtures = _private_fixtures()
    for fixture in fixtures["bulk_valid"]:
        body = bytes.fromhex(fixture["hex"])
        assert len(body) == BULK_HEADER_BYTES == 32
        assert encode_bulk_header(**fixture["header"]) == body
        assert decode_bulk_header(body) == fixture["header"]
    for hexadecimal in fixtures["bulk_invalid_hex"]:
        with pytest.raises(HomeKitProtocolError):
            decode_bulk_header(bytes.fromhex(hexadecimal))
    header = fixtures["bulk_valid"][0]["header"]
    for mutation in ({"sequence": True}, {"sequence": -1}, {"length": 0}, {"length": 65537}):
        with pytest.raises(HomeKitProtocolError):
            encode_bulk_header(**(header | mutation))


def test_every_supported_message_has_an_explicit_direction_and_no_runtime_side_effects():
    from halo_server.homekit.ipc import (
        BIDIRECTIONAL_KINDS,
        HAP_KINDS,
        KINDS,
        PYTHON_KINDS,
        validate_direction,
    )

    fixtures = _private_fixtures()
    assert {item["kind"] for item in fixtures["valid"]} == KINDS
    assert not (HAP_KINDS & PYTHON_KINDS)
    for message in fixtures["valid"]:
        for sender in ("python", "hap"):
            allowed = (
                message["kind"]
                in (PYTHON_KINDS if sender == "python" else HAP_KINDS) | BIDIRECTIONAL_KINDS
            )
            if message["kind"] == "transfer_open":
                allowed = sender == (
                    "hap" if message["payload"]["kind"] == "talkback_pcm" else "python"
                )
            if allowed:
                assert validate_direction(message, sender) is message
            else:
                with pytest.raises(HomeKitProtocolError):
                    validate_direction(message, sender)
        with pytest.raises(HomeKitProtocolError):
            validate_direction(message, "unknown")


def test_reply_correlation_fences_boot_request_scope_and_owned_operation_ids():
    import copy

    from halo_server.homekit.ipc import REPLY_KINDS, validate_reply

    samples = {item["kind"]: item for item in reversed(_private_fixtures()["valid"])}
    for request_kind, reply_kind in REPLY_KINDS.items():
        request, reply = samples[request_kind], samples[reply_kind]
        assert validate_reply(request, reply) is reply
        for key in ("boot_id", "request_id"):
            stale = copy.deepcopy(reply)
            stale[key] = str(uuid.uuid4())
            with pytest.raises(HomeKitProtocolError):
                validate_reply(request, stale)
        for key in ("session_id", "job_id", "operation_id", "transfer_id"):
            if key in request["payload"]:
                stale = copy.deepcopy(reply)
                stale["payload"][key] = str(uuid.uuid4())
                with pytest.raises(HomeKitProtocolError):
                    validate_reply(request, stale)
        if "scope" in request["payload"]:
            for key in request["payload"]["scope"]:
                stale = copy.deepcopy(reply)
                old = stale["payload"]["scope"][key]
                stale["payload"]["scope"][key] = (
                    old + 1 if isinstance(old, int) else str(uuid.uuid4())
                )
                with pytest.raises(HomeKitProtocolError):
                    validate_reply(request, stale)
    request, reply = copy.deepcopy(samples["inventory_request"]), samples["inventory_page"]
    request["payload"]["revision"] = 5
    with pytest.raises(HomeKitProtocolError):
        validate_reply(request, reply)
    request["payload"]["revision"] = 0
    request["payload"]["cursor"] = str(uuid.uuid4())
    with pytest.raises(HomeKitProtocolError):
        validate_reply(request, reply)


def test_nested_dto_objects_reject_unknown_fields_at_every_boundary():
    import copy

    def paths(value, trail=()):
        if isinstance(value, dict):
            yield trail
            for key, child in value.items():
                yield from paths(child, (*trail, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from paths(child, (*trail, index))

    samples = _private_fixtures()["valid"]
    for sample in samples:
        for path in paths(sample):
            mutation = copy.deepcopy(sample)
            target = mutation
            for key in path:
                target = target[key]
            target["unrecognized"] = None
            with pytest.raises(HomeKitProtocolError):
                decode_body(json.dumps(mutation).encode())


def test_actual_python_and_built_node_decoders_agree_on_complete_shared_corpus():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    protocol = Path(__file__).resolve().parents[1] / "homekit-adapter/dist/src/protocol.js"
    if node is None or not protocol.is_file():
        pytest.skip("Node and the separately built adapter are required for cross-language probe")
    fixtures = _private_fixtures()
    bodies = [
        json.dumps(value).encode().hex()
        for key in ("valid", "invalid", "semantic_invalid")
        for value in fixtures[key]
    ] + fixtures["raw_invalid_hex"]
    expected = []
    for hexadecimal in bodies:
        try:
            decode_body(bytes.fromhex(hexadecimal))
        except HomeKitProtocolError:
            expected.append(False)
        else:
            expected.append(True)
    script = (
        "import {readFileSync} from 'node:fs';"
        f"import {{decodeEnvelope}} from {json.dumps(protocol.as_uri())};"
        "const inputs=JSON.parse(readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify(inputs.map(hex=>{try{"
        "decodeEnvelope(Buffer.from(hex,'hex'));return true}catch{return false}})));"
    )
    process = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        input=json.dumps(bodies),
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert len(process.stdout) < 10_000
    assert json.loads(process.stdout) == expected
    assert sum(expected) == len(fixtures["valid"])


def test_control_cap_is_utf8_bytes_and_inventory_pages_can_be_smaller_than_32():
    import copy

    samples = _private_fixtures()["valid"]
    ping = next(item for item in samples if item["kind"] == "ping")
    body = json.dumps(ping).encode()
    assert decode_body(body + b" " * (MAX_CONTROL_BYTES - len(body))) == ping
    with pytest.raises(HomeKitProtocolError, match="length"):
        decode_body(body + b" " * (MAX_CONTROL_BYTES + 1 - len(body)))
    maximal = next(
        item
        for item in samples
        if item["kind"] == "inventory_page"
        and item["payload"]["devices"]
        and item["payload"]["devices"][0]["observed_format"] is not None
    )
    page = copy.deepcopy(maximal)
    device = page["payload"]["devices"][0]
    device["family"] = device["kind"] = device["model"] = "😀" * 128
    page["payload"]["total"] = 32
    page["payload"]["devices"] = [copy.deepcopy(device) for _ in range(32)]
    for index, item in enumerate(page["payload"]["devices"]):
        item["scope"]["camera_id"] = str(index)
    assert len(json.dumps(page, ensure_ascii=False).encode()) > MAX_CONTROL_BYTES
    with pytest.raises(HomeKitProtocolError, match="large"):
        encode_frame(page)


def test_literal_text_uses_frozen_unicode_ranges_on_every_supported_python():
    from halo_server.homekit.text import (
        FORBIDDEN_TEXT_RANGES,
        literal_text,
        safe_display_text,
    )

    expected = _private_fixtures()["unicode16_forbidden_ranges"]
    assert list(map(list, FORBIDDEN_TEXT_RANGES)) == expected
    for start, end in expected:
        for point in {start, end}:
            value = "a" + chr(point) + "b"
            assert not literal_text(value, 128)
            assert safe_display_text(value) == "ab"
    # These Egyptian format controls are unassigned in Python3.11's Unicode14.
    for point in (0x13439, 0x1343F):
        assert not literal_text(chr(point), 128)
    assert literal_text("literal 😀 <front>", 128)
    assert literal_text("", 128)
    assert not literal_text("", 128, empty=False)
    assert safe_display_text(None) == ""
