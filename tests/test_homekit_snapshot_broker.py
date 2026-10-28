"""READY, transfer reservation and terminal image delivery have exact owners."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homekit_broker import permit, request, setup_broker

from halo_server.homekit.bulk import BulkTransferError
from halo_server.homekit.ipc import validate_reply


class Prepared:
    def __init__(self, parameters):
        self.parameters = parameters
        self.chunks = []
        self.cancelled = None
        self.fail_cancel = False
        self.done = asyncio.Event()

    async def send(self, source):
        async for chunk in source:
            assert self.parameters["valid"]()
            self.chunks.append(bytes(chunk))
        self.done.set()

    async def cancel(self, code="cancelled"):
        if self.fail_cancel:
            raise RuntimeError("Injected producer cleanup failure")
        self.cancelled = code
        self.done.set()

    @property
    def retirement_complete(self):
        return self.cancelled is not None and not self.fail_cancel


class Channel:
    def __init__(self):
        self.handles = []
        self.busy = False

    def prepare_send(self, **parameters):
        if self.busy:
            raise BulkTransferError("busy")
        handle = Prepared(parameters)
        self.handles.append(handle)
        return handle


class Images:
    ready = True

    def __init__(self):
        self.cancelled = []

    async def get(self, boot, scope, identity, width, height, reason):
        return SimpleNamespace(data=b"image" * 20000)

    def cancel(self, *identity):
        self.cancelled.append(identity)


async def configured(tmp_path):
    broker, boot, peer, service, client, _ = await setup_broker(tmp_path)
    scope = await permit(broker, boot, snapshots=True)
    channel = peer.bulk_channel = Channel()
    images = broker.snapshot_manager = Images()
    data = {
        "scope": scope,
        "job_id": str(uuid4()),
        "width": 320,
        "height": 240,
        "reason": "unknown",
    }
    envelope = request(boot, "snapshot_request", data)
    return broker, boot, service, client, channel, images, envelope


@pytest.mark.asyncio
async def test_image_transfer_cannot_open_before_correlated_ready_is_flushed(tmp_path):
    broker, boot, _, client, channel, _, envelope = await configured(tmp_path)
    try:
        payload = await broker.request(envelope)
        reply = envelope | {"kind": "snapshot_result", "payload": payload}
        validate_reply(envelope, reply)
        assert payload["status"] == "ready" and len(channel.handles) == 1
        prepared = channel.handles[0]
        await asyncio.sleep(0)
        assert not prepared.chunks
        wrong = envelope | {"payload": envelope["payload"] | {"job_id": str(uuid4())}}
        await broker.replied(wrong, reply)
        await asyncio.sleep(0)
        assert not prepared.chunks
        await broker.replied(envelope, reply)
        await asyncio.wait_for(prepared.done.wait(), 1)
        assert b"".join(prepared.chunks) == b"image" * 20000
        assert all(len(chunk) <= 65536 for chunk in prepared.chunks)
        assert not broker._snapshot_jobs and client.submissions == 0
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_image_capacity_failure_rejects_before_ready(tmp_path):
    broker, boot, _, _, channel, _, envelope = await configured(tmp_path)
    channel.busy = True
    try:
        result = await broker.request(envelope)
        assert result["status"] == "rejected" and result["code"] == "busy"
        assert result["transfer_id"] is None and not broker._snapshot_jobs
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["cancel", "privacy", "boot"])
async def test_retirement_after_ready_before_open_cannot_emit_image(tmp_path, retirement):
    broker, boot, service, _, channel, images, envelope = await configured(tmp_path)
    try:
        payload = await broker.request(envelope)
        reply = envelope | {"kind": "snapshot_result", "payload": payload}
        prepared = channel.handles[0]
        if retirement == "cancel":
            await broker.notice(
                request(
                    boot,
                    "snapshot_cancel",
                    {
                        "scope": envelope["payload"]["scope"],
                        "job_id": envelope["payload"]["job_id"],
                    },
                )
            )
        elif retirement == "privacy":
            scope = service.homekit.inventory().cameras[0].scope
            assert asdict(scope) == envelope["payload"]["scope"]
            service.homekit.fence_camera(boot, scope)
        else:
            await broker.retired(boot)
        await broker.replied(envelope, reply)
        await asyncio.wait_for(prepared.done.wait(), 1)
        assert prepared.cancelled is not None and not prepared.chunks
        if retirement != "privacy":
            assert images.cancelled
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_failed_snapshot_cleanup_reports_retained_job_and_recovers_on_retry(tmp_path):
    broker, boot, _, _, channel, _, envelope = await configured(tmp_path)
    try:
        await broker.request(envelope)
        prepared = channel.handles[0]
        prepared.fail_cancel = True
        with pytest.raises(RuntimeError):
            await broker.notice(
                request(
                    boot,
                    "snapshot_cancel",
                    {
                        "scope": envelope["payload"]["scope"],
                        "job_id": envelope["payload"]["job_id"],
                    },
                )
            )
        task = broker._snapshot_jobs[envelope["payload"]["job_id"]].task
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(ExceptionGroup):
            await broker.retired(boot)
        assert broker.cleanup_failed and broker._snapshot_jobs
        prepared.fail_cancel = False
        await broker.retired(boot)
        assert prepared.retirement_complete
        assert not broker.cleanup_failed and not broker._snapshot_jobs
    finally:
        for prepared in channel.handles:
            prepared.fail_cancel = False
        await broker.retired(boot)
