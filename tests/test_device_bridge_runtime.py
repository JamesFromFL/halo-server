"""Isolated bridge protocol/process tests; no live Ring or HAP publication."""

import asyncio
import base64
import copy
import json
import os
import shutil
import signal
import struct
import time
from pathlib import Path
from uuid import uuid4

import pytest

from halo_server.devices import ipc
from halo_server.devices.runtime import DeviceBridgeRuntime, DeviceBridgeRuntimeError
from halo_server.homekit.storage_owner import PairingDirectoryOwner, PairingStorageError

NODE = shutil.which("node")
ROOT = Path(__file__).parents[1]
ENTRY = ROOT / "homekit-adapter/dist/src/device-main.js"


def snapshot():
    return {
        "scope": {"profile_id": str(uuid4()), "account_generation": 1},
        "registry_revision": 2,
        "sequence": 1,
        "expires_at_ms": int(time.monotonic() * 1000) + 15000,
        "bridge": {"accessory_uuid": str(uuid4()), "username": "02:AB:CD:EF:12:34", "name": "Halo"},
        "children": [
            {
                "device_id": "a" * 64,
                "accessory_uuid": str(uuid4()),
                "name": "Hall sensor",
                "model": "Ring",
                "mappings": ["contact"],
                "available": True,
                "alarm": None,
                "light": None,
                "values": {**dict.fromkeys(ipc.MAPPINGS), "contact": False},
            }
        ],
    }


def envelope(kind="publish", payload=None):
    return {
        "v": 1,
        "kind": kind,
        "boot_id": str(uuid4()),
        "request_id": str(uuid4()),
        "payload": snapshot() if payload is None else payload,
    }


def test_protocol_round_trip_preserves_unknown_and_account_identity():
    value = envelope()
    frame = ipc.encode_frame(value)
    assert struct.unpack("!I", frame[:4])[0] == len(frame) - 4
    assert ipc.decode_body(frame[4:]) == value
    assert ipc.validate_envelope(value, role="python") == value
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.validate_envelope(value, role="hap")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(ring_token="forbidden"),
        lambda p: p.update(sequence=True),
        lambda p: p.update(sequence=10**500),
        lambda p: p.update(registry_revision=-1),
        lambda p: p.update(expires_at_ms=0),
        lambda p: p["scope"].update(account_generation=0),
        lambda p: p["bridge"].update(username="01:AB:CD:EF:12:34"),
        lambda p: p["bridge"].update(name="bad\u200bname"),
        lambda p: p["bridge"].update(accessory_uuid=str(uuid4()).upper()),
        lambda p: p["children"][0].update(mappings=[]),
        lambda p: p["children"][0].update(mappings=["contact", "contact"]),
        lambda p: p["children"][0].update(mappings=["panic"]),
        lambda p: p["children"][0].update(mappings=["smoke", "contact"]),
        lambda p: p["children"][0].update(available=False),
        lambda p: p["children"][0]["values"].update(motion=False),
        lambda p: p["children"][0]["values"].update(contact=0),
        lambda p: p["children"][0]["values"].update(battery_level=50),
        lambda p: p["children"].append(copy.deepcopy(p["children"][0])),
        lambda p: p["children"][0].update(accessory_uuid=p["bridge"]["accessory_uuid"]),
        lambda p: p["children"][0].update(name="A" * 65),
    ],
)
def test_protocol_rejects_unsafe_or_ambiguous_snapshot(mutation):
    value = envelope()
    mutation(value["payload"])
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.encode_frame(value)


def test_protocol_capacity_is_bounded_and_unknown_values_are_permitted():
    value = envelope()
    child = value["payload"]["children"][0]
    child["available"] = False
    child["values"] = dict.fromkeys(ipc.MAPPINGS)
    value["payload"]["children"] = [
        {**child, "device_id": f"{index:064x}", "accessory_uuid": str(uuid4())}
        for index in range(149)
    ]
    assert len(ipc.encode_frame(value)) < ipc.MAX_CONTROL_BYTES
    value["payload"]["children"].append(
        {**child, "device_id": "b" * 64, "accessory_uuid": str(uuid4())}
    )
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.encode_frame(value)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * (ipc.MAX_CONTROL_BYTES + 1),
        b"\xef\xbb\xbf{}",
        b"\xff",
        b'{"v":1,"v":1}',
        b'{"v":1,"\\u0076":1}',
        b'{"__proto__":{}}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"[" * 1000 + b"]" * 1000,
    ],
)
def test_raw_json_rejects_duplicate_fields_nonfinite_depth_and_bad_utf8(raw):
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.decode_body(raw)


@pytest.mark.asyncio
async def test_partial_frame_deadline_and_oversize_prefix():
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x00\x00")
    with pytest.raises(TimeoutError):
        await ipc.read_frame(reader, timeout=0.01)
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", ipc.MAX_CONTROL_BYTES + 1))
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        await ipc.read_frame(reader)


def test_reply_must_match_exact_scope_revision_sequence_and_request():
    request = envelope()
    response = {
        **request,
        "kind": "publish_result",
        "payload": {
            **{key: request["payload"][key] for key in ("scope", "registry_revision", "sequence")},
            "accepted": True,
            "code": "ok",
        },
    }
    ipc.validate_reply(request, response)
    for field, value in (
        ("scope", {"profile_id": str(uuid4()), "account_generation": 1}),
        ("registry_revision", 3),
        ("sequence", 2),
    ):
        changed = copy.deepcopy(response)
        changed["payload"][field] = value
        with pytest.raises(ipc.DeviceBridgeProtocolError):
            ipc.validate_reply(request, changed)
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.validate_reply(request, {**response, "boot_id": str(uuid4())})


def identity():
    value = snapshot()
    return {
        "scope": value["scope"],
        "registry_revision": 2,
        "accessory_uuid": value["bridge"]["accessory_uuid"],
        "username": value["bridge"]["username"],
    }


def test_pairing_metadata_validates_exact_manifest_and_never_accepts_partial_secret():
    from halo_server.homekit.ipc import pairing_manifest_revision

    manifest = dict.fromkeys(("accessory_info", "controller_storage", "identifier_cache"))
    core = {
        **identity(),
        "accepted": True,
        "code": "ok",
        "published": True,
        "paired": False,
        "paired_clients": 0,
        "pairing_revision": pairing_manifest_revision(manifest),
    }
    value = envelope("status_result", {**core, "pairing_manifest": manifest})
    ipc.encode_frame(value)
    value["payload"]["pairing_revision"] = "a" * 64
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.encode_frame(value)
    reveal = envelope(
        "reveal_result",
        {**core, "pincode": "123-45-679", "setup_uri": "X-HM://0123456789ABC", "ttl_ms": 60000},
    )
    ipc.encode_frame(reveal)
    reveal["payload"]["paired"], reveal["payload"]["paired_clients"] = True, 1
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.encode_frame(reveal)
    reveal["payload"].update(accepted=False, code="policy-denied")
    with pytest.raises(ipc.DeviceBridgeProtocolError):
        ipc.encode_frame(reveal)
    reveal["payload"].update(pincode=None, setup_uri=None, ttl_ms=0)
    ipc.encode_frame(reveal)


def fake_child(tmp_path, *, hello="", ping="", before="", publish="", command_result=""):
    path = tmp_path / "fake.cjs"
    path.write_text(
        """
const { Socket } = require('node:net');
const control = new Socket({fd:Number(process.env.HALO_DEVICE_CONTROL_FD),
  readable:true, writable:true});
const send = message => {
  const body = Buffer.from(JSON.stringify(message));
  const frame = Buffer.alloc(body.length+4); frame.writeUInt32BE(body.length); body.copy(frame,4);
  control.write(frame);
};
let buffer = Buffer.alloc(0);
control.on('close', () => process.exit(0));
"""
        + before
        + """
control.on('data', chunk => {
  buffer = Buffer.concat([buffer, chunk]);
  while (buffer.length >= 4 && buffer.length >= buffer.readUInt32BE(0)+4) {
    const size = buffer.readUInt32BE(0), message = JSON.parse(buffer.subarray(4,4+size));
    buffer = buffer.subarray(4+size);
    if (message.kind === 'hello') {
      message.kind = 'hello_ack';
      message.payload = {role:'device-hap',build_id:message.payload.build_id,
        protocol:1,pid:process.pid};
"""
        + hello
        + """
      send(message);
    } else if (message.kind === 'ping') {
"""
        + ping
        + """
      message.kind = 'pong'; send(message);
    } else if (message.kind === 'publish') {
      const p = message.payload;
      message.kind = 'publish_result';
      message.payload = {scope:p.scope,registry_revision:p.registry_revision,
        sequence:p.sequence,accepted:true,code:'ok'};
      send(message);
"""
        + publish
        + """
    } else if (['alarm_set_result','light_set_result'].includes(message.kind)) {
"""
        + command_result
        + """
    } else if (message.kind === 'shutdown') {
      message.kind = 'shutdown_ack'; send(message); control.end();
    }
  }
});
"""
    )
    return path


def runtime(tmp_path, **kwargs):
    if NODE is None:
        pytest.skip("Node is required for real subprocess supervision tests")
    entry = kwargs.pop("entry", None) or fake_child(tmp_path)
    return DeviceBridgeRuntime(Path(NODE), entry, build_id="synthetic-device-1", **kwargs)


async def wait_state(child, state):
    async with asyncio.timeout(4):
        while child.state != state:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_real_subprocess_handshake_publication_backpressure_and_joined_stop(tmp_path):
    retired = []

    async def on_retired(boot):
        retired.append(boot)

    child = runtime(tmp_path, on_retired=on_retired)
    try:
        await child.start()
        boot, process = child.boot_id, child._process
        assert child.state == "ready"
        assert (await child.publish(snapshot()))["accepted"]
        assert await asyncio.gather(*[child.request("ping", {}) for _ in range(8)]) == [{}] * 8
    finally:
        await child.stop()
    assert process.returncode is not None
    assert child.pid is None and not child.retirement_pending
    assert child._writer is None and not child._pending
    assert retired == [boot]
    await child.stop()
    assert retired == [boot]


@pytest.mark.asyncio
async def test_graceful_shutdown_eof_allows_child_to_finish_before_exit(tmp_path, monkeypatch):
    release_exit = tmp_path / "release-child-exit"
    before = (
        """
control.removeAllListeners('close');
control.on('finish', () => {
  control.destroy();
  const release = """
        + json.dumps(str(release_exit))
        + """;
  setInterval(() => {
    if (require('node:fs').existsSync(release)) process.exit(0);
  }, 5);
});
"""
    )
    child = runtime(tmp_path, entry=fake_child(tmp_path, before=before))
    read = child._read

    async def release_after_eof(reader):
        try:
            await read(reader)
        finally:
            # Hold the child alive until the reader has handled its shutdown EOF.
            # A premature termination therefore cannot race a natural exit.
            release_exit.write_text("release")

    monkeypatch.setattr(child, "_read", release_after_eof)
    try:
        await child.start()
        process = child._process
        await asyncio.wait_for(child.stop(), 2)
        assert release_exit.is_file()
        assert process.returncode == 0
        assert child.state == "stopped" and not child.retirement_pending
        assert child.pid is None and not child._pending
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_unexpected_control_eof_terminates_child_and_fails_pending_request(tmp_path):
    script = fake_child(
        tmp_path,
        before="control.removeAllListeners('close'); setInterval(() => {}, 1000);",
        ping="control.destroy(); continue;",
    )
    child = runtime(tmp_path, entry=script, max_restarts=0)
    try:
        await child.start()
        process = child._process
        with pytest.raises(DeviceBridgeRuntimeError, match="channel closed"):
            await child.request("ping", {})
        await wait_state(child, "failed")
        assert process.returncode == -signal.SIGTERM
        assert not child.retirement_pending and not child._pending
    finally:
        await child.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "message.payload.build_id='wrong';",
        "message.payload.pid+=1;",
        "message.boot_id='00000000-0000-0000-0000-000000000001';",
    ],
)
async def test_handshake_mismatch_reaps_child_without_publication(tmp_path, change):
    child = runtime(tmp_path, entry=fake_child(tmp_path, hello=change), max_restarts=0)
    try:
        with pytest.raises(DeviceBridgeRuntimeError):
            await child.start()
        assert child.state == "failed" and child.pid is None
    finally:
        await child.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "message.request_id='00000000-0000-0000-0000-000000000001';",
        "message.kind='panic'; send(message); continue;",
        "message.payload={credentials:'forbidden'};",
    ],
)
async def test_unowned_or_invalid_reply_reaps_only_owned_child(tmp_path, change):
    child = runtime(tmp_path, entry=fake_child(tmp_path, ping=change), max_restarts=0)
    try:
        await child.start()
        process = child._process
        with pytest.raises(DeviceBridgeRuntimeError):
            await child.request("ping", {})
        await wait_state(child, "failed")
        assert process.returncode is not None and not child.retirement_pending
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_crash_rotates_only_its_owned_boot_and_respects_restart_budget(tmp_path):
    retired, ready = [], []

    async def on_retired(boot):
        retired.append(boot)

    async def on_ready(boot):
        ready.append(boot)

    child = runtime(tmp_path, on_ready=on_ready, on_retired=on_retired, max_restarts=1)
    try:
        await child.start()
        old = child.boot_id
        child._process.kill()
        async with asyncio.timeout(4):
            while child.boot_id == old or child.state != "ready":
                await asyncio.sleep(0.01)
        assert child.restart_count == 1 and ready == [old, child.boot_id]
        assert retired == [old]
        child._process.kill()
        await wait_state(child, "failed")
        assert retired == ready and child.pid is None
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_sync_invalidate_blocks_new_work_and_never_restarts(tmp_path):
    child = runtime(tmp_path)
    try:
        await child.start()
        process = child._process
        child.invalidate()
        with pytest.raises(DeviceBridgeRuntimeError):
            await child.publish(snapshot())
        await child.stop()
        assert process.returncode is not None and child.restart_count == 0
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_cancelled_request_retires_ambiguous_child_and_releases_pending(tmp_path):
    child = runtime(tmp_path, entry=fake_child(tmp_path, ping="continue;"), max_restarts=0)
    try:
        await child.start()
        process = child._process
        pending = asyncio.create_task(child.request("ping", {}))
        await asyncio.sleep(0.03)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await wait_state(child, "failed")
        assert process.returncode is not None and not child._pending
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_timeout_terminates_child_and_queue_has_finite_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr("halo_server.devices.runtime.REQUEST_SECONDS", 0.1)
    child = runtime(tmp_path, entry=fake_child(tmp_path, ping="continue;"), max_restarts=0)
    try:
        await child.start()
        pending = [asyncio.create_task(child.request("ping", {})) for _ in range(8)]
        await asyncio.sleep(0.02)
        with pytest.raises(DeviceBridgeRuntimeError, match="queue is full"):
            await child.request("ping", {})
        results = await asyncio.gather(*pending, return_exceptions=True)
        assert all(
            isinstance(result, (TimeoutError, DeviceBridgeRuntimeError)) for result in results
        )
        await wait_state(child, "failed")
        assert not child._pending and child.pid is None
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_admission_fence_prevents_queued_snapshot_without_retiring_healthy_child(tmp_path):
    child = runtime(tmp_path)
    try:
        await child.start()
        with pytest.raises(DeviceBridgeRuntimeError, match="retired"):
            await child.publish(snapshot(), admission=lambda: False)
        assert await child.request("ping", {}) == {}
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_child_environment_has_no_ring_credentials_or_node_injection(tmp_path, monkeypatch):
    output = tmp_path / "environment.json"
    script = fake_child(
        tmp_path,
        before=(
            f"require('node:fs').writeFileSync({json.dumps(str(output))}, "
            "JSON.stringify(process.env));"
        ),
    )
    for key in ("RING_TOKEN", "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "HALO_BULK_FD", "HOME"):
        monkeypatch.setenv(key, "sensitive-test-value")
    child = runtime(tmp_path, entry=script)
    try:
        await child.start()
        environment = json.loads(output.read_text())
        assert "HALO_DEVICE_CONTROL_FD" in environment
        assert environment["HALO_PARENT_PID"] == str(os.getpid())
        assert not set(environment).intersection(
            {"RING_TOKEN", "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "HALO_BULK_FD", "HOME"}
        )
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_pairing_store_owned_separately_until_child_reaped(tmp_path):
    camera = tmp_path / "cameras" / "pairings"
    devices = tmp_path / "devices" / "pairings"
    for path in (camera, devices):
        path.parent.mkdir(mode=0o700)
        path.mkdir(mode=0o700)
    sentinel = camera / "camera-unchanged"
    sentinel.write_bytes(b"camera-state")
    camera_owner = PairingDirectoryOwner.acquire(camera)
    child = runtime(tmp_path, pairing_directory=devices)
    try:
        await child.start()
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(devices)
        assert camera_owner.directory_fd >= 0
        assert sentinel.read_bytes() == b"camera-state"
        await child.stop()
        fresh = PairingDirectoryOwner.acquire(devices)
        fresh.close()
        assert sentinel.read_bytes() == b"camera-state"
    finally:
        await child.stop()
        camera_owner.close()


@pytest.mark.asyncio
async def test_stop_escalates_unresponsive_child_and_joins_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr("halo_server.devices.runtime.STOP_SECONDS", 0.05)
    script = fake_child(tmp_path, before="process.on('SIGTERM', () => {});")
    child = runtime(tmp_path, entry=script)
    try:
        await child.start()
        process = child._process
        os.kill(process.pid, signal.SIGSTOP)
        child.invalidate()
        await asyncio.wait_for(child.stop(), 2)
        assert process.returncode == -signal.SIGKILL
        assert not child.retirement_pending
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_missing_artifacts_fail_without_spending_restart_budget(tmp_path):
    child = runtime(tmp_path, entry=tmp_path / "absent.js")
    try:
        with pytest.raises(DeviceBridgeRuntimeError):
            await child.start()
        assert child.reason == "device_adapter_artifacts_unavailable"
        assert child.restart_count == 0 and child.state == "failed"
    finally:
        await child.stop()


@pytest.mark.asyncio
async def test_cancelling_start_joins_spawn_and_pairing_ownership(tmp_path):
    pairings = tmp_path / "devices" / "pairings"
    pairings.parent.mkdir(mode=0o700)
    pairings.mkdir(mode=0o700)
    child = runtime(
        tmp_path,
        entry=fake_child(tmp_path, hello="continue;"),
        pairing_directory=pairings,
    )
    pending = asyncio.create_task(child.start())
    async with asyncio.timeout(2):
        while child.pid is None:
            await asyncio.sleep(0.005)
    process = child._process
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert process.returncode is not None
    assert child.state == "stopped" and not child.retirement_pending
    owner = PairingDirectoryOwner.acquire(pairings)
    owner.close()


@pytest.mark.asyncio
async def test_cancelled_stop_still_reaps_child(tmp_path, monkeypatch):
    monkeypatch.setattr("halo_server.devices.runtime.STOP_SECONDS", 0.05)
    child = runtime(tmp_path)
    await child.start()
    process = child._process
    os.kill(process.pid, signal.SIGSTOP)
    child.invalidate()
    pending = asyncio.create_task(child.stop())
    await asyncio.sleep(0.01)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert process.returncode is not None and not child.retirement_pending


@pytest.mark.asyncio
async def test_child_retains_shared_release_guard_until_reaped(tmp_path, monkeypatch):
    from halo_server.release_guard import ReleaseGuard, ReleaseGuardError

    state = tmp_path / "release-state"
    guard = ReleaseGuard.acquire(state, create=True)
    for key, value in guard.environment().items():
        monkeypatch.setenv(key, value)
    child = runtime(tmp_path)
    try:
        await child.start()
        guard.close()
        with pytest.raises(ReleaseGuardError):
            ReleaseGuard.acquire(state, exclusive=True, nonblocking=True)
        await child.stop()
        exclusive = ReleaseGuard.acquire(state, exclusive=True, nonblocking=True)
        exclusive.close()
    finally:
        await child.stop()
        guard.close()


@pytest.mark.asyncio
@pytest.mark.skipif(NODE is None or not ENTRY.is_file(), reason="Compiled device adapter required")
async def test_python_and_node_accept_identical_bounded_protocol_corpus():
    hello = envelope("hello", {"role": "python", "build_id": "test", "protocol": 1})
    basic = json.dumps(hello, separators=(",", ":")).encode()
    cases = [
        basic,
        basic.replace(b'"v":1,', b'"v":1.0,'),
        basic.replace(b'"v":1,', b'"v":true,'),
        basic.replace(b'"v":1,', b'"v":1,"\\u0076":1,'),
        basic.replace(b'"test"', b'"\\ud800"'),
        basic.replace(b'"test"', b'"\\u200b"'),
        b"\xef\xbb\xbf" + basic,
        b"\xff",
        b'{"x":1e999}',
        ipc.encode_frame(envelope())[4:],
    ]
    for count in (64, 65):
        value = envelope()
        value["payload"]["children"][0]["name"] = "😀" * count
        cases.append(json.dumps(value, ensure_ascii=False).encode())
    for field, bad in (("sequence", True), ("sequence", 10**500), ("expires_at_ms", -1)):
        value = envelope()
        value["payload"][field] = bad
        cases.append(json.dumps(value).encode())
    for kind, payload in (
        ("status", identity()),
        ("reveal", {**identity(), "pairing_revision": "a" * 64}),
        ("reset_prepare", {**identity(), "pairing_revision": "a" * 64}),
        ("panic", {}),
    ):
        cases.append(json.dumps(envelope(kind, payload)).encode())
    from halo_server.homekit.ipc import pairing_manifest_revision

    manifest = dict.fromkeys(("accessory_info", "controller_storage", "identifier_cache"))
    core = {
        **identity(),
        "accepted": True,
        "code": "ok",
        "published": True,
        "paired": False,
        "paired_clients": 0,
        "pairing_revision": pairing_manifest_revision(manifest),
    }
    status = envelope("status_result", {**core, "pairing_manifest": manifest})
    cases.append(json.dumps(status).encode())
    status["payload"]["pairing_manifest"] = None
    cases.append(json.dumps(status).encode())
    for pin, uri, ttl, paired, count in (
        ("123-45-679", "X-HM://0123456789ABC", 60000, False, 0),
        (None, "X-HM://0123456789ABC", 60000, False, 0),
        ("123-45-679", "X-HM://ABC", 60000, False, 0),
        ("123-45-679", "X-HM://0123456789ABC", 30000, False, 0),
        ("123-45-679", "X-HM://0123456789ABC", 60000, True, 1),
        ("123-45-679", "X-HM://0123456789ABC", 60000, None, None),
    ):
        cases.append(
            json.dumps(
                envelope(
                    "reveal_result",
                    {
                        **core,
                        "pincode": pin,
                        "setup_uri": uri,
                        "ttl_ms": ttl,
                        "paired": paired,
                        "paired_clients": count,
                    },
                )
            ).encode()
        )
    from test_device_alarm_runtime import alarm_snapshot, command

    alarm = alarm_snapshot()
    cases.append(json.dumps(envelope("publish", alarm)).encode())
    for change in ({"mode": "night"}, {"mode": []}, {"control_enabled": 1}, {"triggered": None}):
        changed = copy.deepcopy(alarm)
        changed["children"][0]["alarm"].update(change)
        cases.append(json.dumps(envelope("publish", changed)).encode())
    for target in ("home", "away", "disarmed", "night", "panic", "bypass", None, []):
        request = envelope("alarm_set", {**command(alarm), "target": target})
        cases.append(json.dumps(request).encode())
        cases.append(
            json.dumps(
                {
                    **request,
                    "kind": "alarm_set_result",
                    "payload": {**request["payload"], "accepted": False, "code": "uncertain"},
                }
            ).encode()
        )
    from test_device_light_runtime import light_command, light_snapshot

    light = light_snapshot()
    cases.append(json.dumps(envelope("publish", light)).encode())
    for change in ({"on": None}, {"on": 1}, {"on": "on"}, {"control_enabled": 1}):
        changed = copy.deepcopy(light)
        changed["children"][0]["light"].update(change)
        cases.append(json.dumps(envelope("publish", changed)).encode())
    for target in (True, False, 0, 1, "on", "default", None, []):
        request = envelope("light_set", {**light_command(light), "target": target})
        cases.append(json.dumps(request).encode())
        for observed in (True, False, 1, None):
            cases.append(
                json.dumps(
                    {
                        **request,
                        "kind": "light_set_result",
                        "payload": {
                            **request["payload"],
                            "accepted": True,
                            "code": "ok",
                            "observed_on": observed,
                        },
                    }
                ).encode()
            )
    expected = []
    for body in cases:
        try:
            ipc.decode_body(body)
            expected.append(True)
        except ipc.DeviceBridgeProtocolError:
            expected.append(False)
    script = f"""
import {{ decodeDeviceEnvelope }} from {json.dumps(ENTRY.with_name("device-protocol.js").as_uri())};
let input=''; for await (const chunk of process.stdin) input+=chunk;
process.stdout.write(JSON.stringify(JSON.parse(input).map(encoded=>{{
  try {{ decodeDeviceEnvelope(Buffer.from(encoded,'base64')); return true; }}
  catch {{return false;}}
}})));
"""
    process = await asyncio.create_subprocess_exec(
        NODE,
        "--input-type=module",
        "-e",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate(
        json.dumps([base64.b64encode(body).decode() for body in cases]).encode()
    )
    assert process.returncode == 0, error.decode()
    assert json.loads(output) == expected


@pytest.mark.asyncio
@pytest.mark.skipif(NODE is None or not ENTRY.is_file(), reason="Compiled device adapter required")
async def test_real_compiled_device_adapter_handshake_without_publication(tmp_path):
    pairings = tmp_path / "devices" / "pairings"
    pairings.parent.mkdir(mode=0o700)
    pairings.mkdir(mode=0o700)
    child = runtime(tmp_path, entry=ENTRY, pairing_directory=pairings, max_restarts=0)
    try:
        await child.start()
        assert await child.request("ping", {}) == {}
    finally:
        await child.stop()
