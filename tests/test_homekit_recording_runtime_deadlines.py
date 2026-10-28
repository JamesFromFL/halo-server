"""Recording handler budgets and cleanup replies remain usable during shutdown."""

import asyncio
import copy
import json
from importlib.resources import files
from pathlib import Path

import pytest

from halo_server.homekit import runtime as module
from halo_server.homekit.ipc import decode_body
from halo_server.homekit.runtime import HomeKitRuntime


def fixture(kind, status=None):
    values = json.loads(
        files("halo_server.homekit").joinpath("protocol/v1/fixtures.json").read_text()
    )["valid"]
    return copy.deepcopy(
        next(
            value
            for value in values
            if value["kind"] == kind
            and (status is None or value["payload"].get("status") == status)
        )
    )


class Writer:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["recording_open", "recording_next", "recording_stop"])
async def test_owned_recording_handler_uses_its_native_cleanup_budget(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(module, "REQUEST_SECONDS", 0.005)
    for name in ("RECORDING_OPEN_SECONDS", "RECORDING_PULL_SECONDS", "RECORDING_STOP_SECONDS"):
        monkeypatch.setattr(module, name, 0.1)
    request = fixture(kind)
    reply = fixture(kind + "_result", "rejected")

    async def handler(message):
        await asyncio.sleep(0.02)
        return reply["payload"]

    async def retired(boot):
        pass

    runtime = HomeKitRuntime(
        Path("/node"),
        tmp_path / "unused.mjs",
        build_id="deadlines",
        on_request=handler,
        on_retired=retired,
    )
    sent = []

    async def send(message, **options):
        sent.append((message, options))

    monkeypatch.setattr(runtime, "_send_envelope", send)
    await runtime._dispatch(request)
    assert sent[0][0]["kind"] == kind + "_result"
    assert sent[0][1]["retirement_reply"] == (kind == "recording_stop")
    with pytest.raises(TimeoutError):
        await runtime._dispatch(fixture("inventory_request"))


@pytest.mark.asyncio
async def test_recording_stop_writes_owned_cleanup_reply_while_stopping(monkeypatch, tmp_path):
    request = fixture("recording_stop")
    monkeypatch.setattr(module, "REQUEST_SECONDS", 0.1)

    async def handler(message):
        return {**message["payload"], "status": "stopped", "code": "ok"}

    async def retired(boot):
        pass

    runtime = HomeKitRuntime(
        Path("/node"),
        tmp_path / "unused.mjs",
        build_id="cleanup",
        on_request=handler,
        on_retired=retired,
    )
    writer = Writer()
    runtime._boot_id, runtime._writer = request["boot_id"], writer
    runtime._active_child_ids.add(request["request_id"])
    runtime._stopping.set()
    await runtime._dispatch(request)
    reply = decode_body(writer.data[4:])
    assert reply["kind"] == "recording_stop_result" and reply["payload"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_stopping_bulk_callback_allows_terminal_fences_and_denies_new_admission(tmp_path):
    async def retired(boot):
        pass

    runtime = HomeKitRuntime(
        Path("/node"), tmp_path / "unused.mjs", build_id="bulk-cleanup", on_retired=retired
    )
    writer = Writer()
    runtime._boot_id, runtime._writer = fixture("transfer_cancel")["boot_id"], writer
    runtime._stopping.set()
    for kind in ("transfer_cancel", "transfer_fence", "transfer_fence_ack", "transfer_result"):
        message = fixture(kind)
        writer.data = b""
        await runtime._send_bulk_control(kind, message["payload"], message["request_id"])
        assert decode_body(writer.data[4:])["kind"] == kind
        # The generic public sender cannot claim this internal cleanup path.
        with pytest.raises(module.HomeKitRuntimeError, match="stopping"):
            await runtime.send(kind, message["payload"], message["request_id"])
    for kind in ("transfer_open", "transfer_credit", "transfer_end"):
        message = fixture(kind)
        with pytest.raises(module.HomeKitRuntimeError, match="stopping"):
            await runtime._send_bulk_control(kind, message["payload"], message["request_id"])
    message = fixture("recording_open_result")
    runtime._active_child_ids.add(message["request_id"])
    with pytest.raises(module.HomeKitRuntimeError, match="stopping"):
        await runtime._send_envelope(message, retirement_reply=True)
