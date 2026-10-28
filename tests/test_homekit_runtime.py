"""Real compiled synthetic adapter; no accessory publication or Ring account."""

import asyncio
import base64
import json
import os
import shutil
import signal
from pathlib import Path

import pytest

from halo_server.homekit.ipc import HomeKitProtocolError, decode_body
from halo_server.homekit.runtime import HomeKitRuntime, HomeKitRuntimeError

ROOT = Path(__file__).parents[1]
ADAPTER = ROOT / "homekit-adapter"
ENTRY = ADAPTER / "dist/src/main.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    NODE is None or not ENTRY.is_file(),
    reason="compiled adapter required: npm ci --ignore-scripts && npm run build",
)


async def _wait_state(runtime, state):
    async with asyncio.timeout(4):
        while runtime.state != state:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_compiled_adapter_handshake_ping_and_joined_stop():
    retired = []

    async def on_retired(boot):
        retired.append(boot)

    runtime = HomeKitRuntime(Path(NODE), ENTRY, build_id="synthetic-test-1", on_retired=on_retired)
    try:
        await asyncio.wait_for(runtime.start(), 3)
        assert runtime.state == "ready"
        process = runtime._process
        assert process is not None
        assert await runtime.request("ping", {}, expected="pong") == {}
        assert (
            await asyncio.gather(*[runtime.request("ping", {}, expected="pong") for _ in range(16)])
            == [{}] * 16
        )
    finally:
        await asyncio.wait_for(runtime.stop(), 3)
    assert runtime.state == "stopped"
    assert process.returncode is not None
    assert runtime.pid is None
    assert runtime._writer is None and runtime._bulk is None
    assert not runtime._pending
    assert len(retired) == 1
    await runtime.stop()
    assert len(retired) == 1


@pytest.mark.asyncio
async def test_adapter_crash_retires_exact_boot_without_unbounded_restart():
    retired = []

    async def on_retired(boot):
        retired.append(boot)

    runtime = HomeKitRuntime(
        Path(NODE), ENTRY, build_id="crash-test", on_retired=on_retired, max_restarts=0
    )
    try:
        await asyncio.wait_for(runtime.start(), 3)
        process = runtime._process
        process.kill()
        await _wait_state(runtime, "failed")
        assert process.returncode is not None
        assert runtime.pid is None
        assert runtime.restart_count == 0
        assert len(retired) == 1
        with pytest.raises(HomeKitRuntimeError):
            await runtime.request("ping", {}, expected="pong")
        with pytest.raises(HomeKitRuntimeError):
            await runtime.start()
    finally:
        await runtime.stop()
    assert len(retired) == 1


@pytest.mark.asyncio
async def test_adapter_restart_preserves_build_but_rotates_runtime_boot():
    retired = []

    async def on_retired(boot):
        retired.append(boot)

    runtime = HomeKitRuntime(
        Path(NODE), ENTRY, build_id="restart-test", on_retired=on_retired, max_restarts=1
    )
    try:
        await asyncio.wait_for(runtime.start(), 3)
        old_boot, process = runtime._boot_id, runtime._process
        process.kill()
        async with asyncio.timeout(4):
            while runtime.state != "ready" or runtime.pid == process.pid:
                await asyncio.sleep(0.01)
        assert runtime._boot_id != old_boot
        assert retired == [old_boot]
        assert runtime.restart_count == 1
        assert await runtime.request("ping", {}, expected="pong") == {}
    finally:
        await runtime.stop()
    assert len(retired) == 2


@pytest.mark.asyncio
async def test_mismatched_child_reply_fails_handshake_and_is_reaped(tmp_path):
    # A real isolated child uses only the inherited control FD and deliberately
    # lies about its build. It never imports HAP or creates a listening socket.
    fake = tmp_path / "wrong-build.cjs"
    fake.write_text("""
const { Socket } = require('node:net');
const socket = new Socket({fd:Number(process.env.HALO_CONTROL_FD),readable:true,writable:true});
let input = Buffer.alloc(0);
socket.on('data', chunk => {
  input = Buffer.concat([input, chunk]);
  if (input.length < 4 || input.length < input.readUInt32BE(0) + 4) return;
  const message = JSON.parse(input.subarray(4).toString());
  message.kind = 'hello_ack';
  message.payload = {role:'hap',build_id:'wrong-build',protocol:1,
    features:['classic-camera-control-v1'],pid:process.pid};
  const body = Buffer.from(JSON.stringify(message));
  const frame = Buffer.alloc(body.length + 4); frame.writeUInt32BE(body.length);body.copy(frame,4);
  socket.write(frame);
});
""")
    retired = []

    async def on_retired(boot):
        retired.append(boot)

    runtime = HomeKitRuntime(
        Path(NODE), fake, build_id="expected-build", on_retired=on_retired, max_restarts=0
    )
    try:
        with pytest.raises(HomeKitRuntimeError):
            await asyncio.wait_for(runtime.start(), 3)
        assert runtime.state == "failed"
        assert runtime.pid is None
    finally:
        await runtime.stop()
    assert len(retired) == 1


@pytest.mark.asyncio
async def test_python_node_raw_json_acceptance_is_identical():
    hello = {
        "v": 1,
        "kind": "hello",
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "request_id": "00000000-0000-0000-0000-000000000002",
        "payload": {"role": "python", "build_id": "test", "protocol": 1},
    }
    basic = json.dumps(hello, separators=(",", ":")).encode()
    cases = [
        basic,
        basic.replace(b'"v":1,', b'"v":1.0,'),
        basic.replace(b'"protocol":1', b'"protocol":1.0'),
        basic.replace(b'"v":1,', b'"v":true,'),
        basic.replace(b'"v":1,', b'"v":1,"\\u0076":1,'),
        basic.replace(b'"test"', b'"\\ud800"'),
        basic.replace(b'"test"', b'"\\udc00"'),
        basic.replace(b'"test"', b'"line\\nbreak"'),
        b"\xef\xbb\xbf" + basic,
        b"\xff",
        b'{"x":1e999}',
    ]
    for count in (128, 129):
        value = {**hello, "payload": {**hello["payload"], "build_id": "😀" * count}}
        cases.append(json.dumps(value, ensure_ascii=False).encode())
    expected = []
    for body in cases:
        try:
            decode_body(body)
            expected.append(True)
        except HomeKitProtocolError:
            expected.append(False)
    process = await asyncio.create_subprocess_exec(
        NODE,
        str(ADAPTER / "dist/test/decode-corpus.js"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"LANG": os.environ.get("LANG", "C.UTF-8")},
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(
                json.dumps([base64.b64encode(body).decode() for body in cases]).encode()
            ),
            3,
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    assert json.loads(stdout) == expected
    assert expected[:3] == [True, True, True]
    assert expected[3:11] == [False] * 8
    assert expected[-2:] == [True, False]


@pytest.mark.asyncio
async def test_cancel_start_joins_late_spawn_without_waiting_for_child_idle(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr("halo_server.homekit.runtime.asyncio.create_subprocess_exec", delayed_spawn)

    async def on_retired(_boot):
        pass

    runtime = HomeKitRuntime(
        Path(NODE), ENTRY, build_id="cancel-start-test", on_retired=on_retired, max_restarts=0
    )
    starting = asyncio.create_task(runtime.start())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        starting.cancel()
        await asyncio.sleep(0)
        assert not starting.done()
        release.set()
        done, _pending = await asyncio.wait({starting}, timeout=1)
        completed_promptly = starting in done
    finally:
        release.set()
        if runtime._process is not None and runtime._process.returncode is None:
            runtime._process.kill()
        await asyncio.gather(starting, return_exceptions=True)
        await runtime.stop()
    assert completed_promptly, "late spawn survived stop until the child's idle timeout"
    assert runtime.pid is None


@pytest.mark.asyncio
async def test_concurrent_stop_reaps_once_without_future_exception_logs():
    loop = asyncio.get_running_loop()
    errors = []
    original = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    retired = []

    async def on_retired(boot):
        retired.append(boot)

    runtime = HomeKitRuntime(Path(NODE), ENTRY, build_id="parallel-stop", on_retired=on_retired)
    try:
        await asyncio.wait_for(runtime.start(), 3)
        await asyncio.wait_for(asyncio.gather(runtime.stop(), runtime.stop()), 3)
        await asyncio.sleep(0)
    finally:
        await runtime.stop()
        loop.set_exception_handler(original)
    assert runtime.pid is None
    assert len(retired) == 1
    assert not errors


def _silent_child(tmp_path):
    entry = tmp_path / "silent-child.cjs"
    entry.write_text("""
const { Socket } = require('node:net');
const socket = new Socket({fd:Number(process.env.HALO_CONTROL_FD),readable:true,writable:true});
process.on('SIGTERM', () => {});
let input = Buffer.alloc(0);
socket.on('data', chunk => {
  input = Buffer.concat([input, chunk]);
  while (input.length >= 4 && input.length >= input.readUInt32BE(0) + 4) {
    const size = input.readUInt32BE(0);
    const message = JSON.parse(input.subarray(4, size + 4).toString());
    input = input.subarray(size + 4);
    if (message.kind !== 'hello') continue;
    message.kind = 'hello_ack';
    message.payload = {role:'hap',build_id:message.payload.build_id,protocol:1,
      features:['classic-camera-control-v1'],pid:process.pid};
    const body = Buffer.from(JSON.stringify(message));
    const frame = Buffer.alloc(body.length + 4);frame.writeUInt32BE(body.length);body.copy(frame,4);
    socket.write(frame);
  }
});
""")
    return entry


@pytest.mark.asyncio
async def test_unresponsive_child_escalates_to_kill_and_is_reaped(monkeypatch, tmp_path):
    monkeypatch.setattr("halo_server.homekit.runtime.REQUEST_SECONDS", 0.15)
    monkeypatch.setattr("halo_server.homekit.runtime.STOP_SECONDS", 0.03)
    retired = []

    async def on_retired(boot):
        retired.append(boot)

    runtime = HomeKitRuntime(
        Path(NODE),
        _silent_child(tmp_path),
        build_id="kill-test",
        on_retired=on_retired,
        max_restarts=0,
    )
    try:
        await asyncio.wait_for(runtime.start(), 3)
        process = runtime._process
    finally:
        await asyncio.wait_for(runtime.stop(), 2)
    assert process.returncode == -signal.SIGKILL
    assert runtime.pid is None
    assert len(retired) == 1


@pytest.mark.asyncio
async def test_child_failure_settles_pending_requests_without_future_logs(tmp_path):
    errors = []
    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    async def on_retired(_boot):
        pass

    runtime = HomeKitRuntime(
        Path(NODE),
        _silent_child(tmp_path),
        build_id="pending-test",
        on_retired=on_retired,
        max_restarts=0,
    )
    pending = None
    try:
        await asyncio.wait_for(runtime.start(), 3)
        pending = asyncio.create_task(runtime.request("ping", {}, expected="pong"))
        async with asyncio.timeout(1):
            while not runtime._pending:
                await asyncio.sleep(0)
        runtime._process.kill()
        with pytest.raises(HomeKitRuntimeError):
            await asyncio.wait_for(pending, 1)
        await _wait_state(runtime, "failed")
    finally:
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await runtime.stop()
        loop.set_exception_handler(original)
    assert not runtime._pending
    assert not errors


@pytest.mark.asyncio
async def test_retirement_callback_failure_keeps_boot_and_stop_retries_same_identity():
    attempts = []

    async def on_retired(boot):
        attempts.append(boot)
        if len(attempts) == 1:
            raise RuntimeError("injected retirement failure")

    runtime = HomeKitRuntime(
        Path(NODE), ENTRY, build_id="retirement-retry", on_retired=on_retired, max_restarts=3
    )
    try:
        await asyncio.wait_for(runtime.start(), 3)
        boot, process = runtime._boot_id, runtime._process
        process.kill()
        async with asyncio.timeout(2):
            while not runtime._supervisor.done():
                await asyncio.sleep(0.01)
        await asyncio.gather(runtime._supervisor, return_exceptions=True)
        assert process.returncode is not None
        assert runtime.state == "failed"
        assert runtime._boot_id == boot
        assert attempts == [boot]
        assert runtime.restart_count == 0
        assert runtime.pid in (None, process.pid)
        with pytest.raises(HomeKitRuntimeError):
            await runtime.start()
        await asyncio.wait_for(runtime.stop(), 2)
        assert attempts == [boot, boot]
        assert runtime._boot_id is None
        assert runtime.pid is None
        assert runtime.state == "stopped"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_persistent_retirement_failure_stays_visible_and_blocks_replacement():
    attempts = []
    fail = True

    async def on_retired(boot):
        attempts.append(boot)
        if fail:
            raise RuntimeError("injected persistent retirement failure")

    runtime = HomeKitRuntime(
        Path(NODE), ENTRY, build_id="retirement-persistent", on_retired=on_retired, max_restarts=3
    )
    try:
        await asyncio.wait_for(runtime.start(), 3)
        boot, process = runtime._boot_id, runtime._process
        process.kill()
        async with asyncio.timeout(2):
            while not runtime._supervisor.done():
                await asyncio.sleep(0.01)
        await asyncio.gather(runtime._supervisor, return_exceptions=True)
        assert runtime.state == "failed"
        assert runtime._boot_id == boot
        assert process.returncode is not None
        assert runtime.restart_count == 0
        assert runtime.pid in (None, process.pid)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(runtime.stop(), 2)
            assert runtime._boot_id == boot
            assert runtime.state == "failed"
            with pytest.raises(HomeKitRuntimeError):
                await runtime.start()
            assert runtime.restart_count == 0
        assert attempts == [boot, boot, boot]
    finally:
        fail = False
        await runtime.stop()
    assert runtime._boot_id is None
    assert runtime.pid is None
    assert runtime.state == "stopped"
