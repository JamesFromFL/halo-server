"""No-account regression for a delayed mains-only cache refresh through the SDK."""

import asyncio
import json
import time

import pytest
from test_ring_snapshots import Content, Response, diagnostic_records, mains_fixture

from halo_server.ring import snapshots

OLD = b'{"timestamps":[{"timestamp":0}]}'
JPEG = b"\xff\xd8synthetic-jpeg\xff\xd9"


def updated_timestamp():
    return json.dumps({"timestamps": [{"timestamp": (time.time() + 60) * 1000}]}).encode()


@pytest.mark.asyncio
async def test_mains_missing_cache_accepts_third_poll_with_one_trigger(caplog, monkeypatch):
    sleep = asyncio.sleep
    waits = []

    async def instant(delay):
        waits.append(delay)
        await sleep(0)

    monkeypatch.setattr("ring_doorbell.doorbot.asyncio.sleep", instant)
    replies = [
        Response(status=404),
        Response(b"{}"),
        Response(OLD),
        Response(OLD),
        Response(updated_timestamp()),
        Response(JPEG),
    ]
    reader, transport, session, device = mains_fixture(replies)
    try:
        assert await reader.fetch(device) == JPEG
        # Actual pinned SDK parameters: one trigger, three timestamp polls,
        # then an image GET. The old two-poll limit never issued this final GET.
        assert waits == [1, 1, 1]
        assert [call[0] for call in session.calls] == ["GET", "POST", "POST", "POST", "POST", "GET"]
        assert len(session.responses) == 0
        assert all(reply.closed and reply.eager_reads == 0 for reply in replies)
        assert diagnostic_records(caplog) == []
        assert not reader._tasks and snapshots._snapshot_scope.get() is None
    finally:
        await reader.close()
        await transport.close()


@pytest.mark.asyncio
async def test_extra_polling_does_not_reset_whole_operation_deadline(caplog, monkeypatch):
    sleep = asyncio.sleep
    monkeypatch.setattr(snapshots, "SNAPSHOT_TOTAL_SECONDS", 0.20)

    async def delayed(_delay):
        await sleep(0.08)

    monkeypatch.setattr("ring_doorbell.doorbot.asyncio.sleep", delayed)
    reader, transport, session, device = mains_fixture(
        [Response(status=404), Response(b"{}"), *[Response(OLD) for _ in range(5)]]
    )
    began = time.monotonic()
    try:
        assert await reader.fetch(device) is None
        assert time.monotonic() - began < 1
        record = diagnostic_records(caplog)[0]
        assert record["phase"] == "fresh" and record["reason"] == "timeout"
        assert record["error_class"] == "TimeoutError"
        assert 2 <= len(session.calls) < 7
        assert not reader._tasks and snapshots._snapshot_scope.get() is None
    finally:
        await reader.close()
        await transport.close()


@pytest.mark.asyncio
async def test_revocation_before_additional_poll_prevents_all_further_io(caplog, monkeypatch):
    sleep = asyncio.sleep
    admitted = True
    waits = 0

    async def revoke_third(_delay):
        nonlocal admitted, waits
        waits += 1
        if waits == 3:
            admitted = False
        await sleep(0)

    monkeypatch.setattr("ring_doorbell.doorbot.asyncio.sleep", revoke_third)
    reader, transport, session, device = mains_fixture(
        [Response(status=404), Response(b"{}"), Response(OLD), Response(OLD)]
    )
    try:
        assert await reader.fetch(device, admission=lambda: admitted) is None
        assert waits == 3
        assert [call[0] for call in session.calls] == ["GET", "POST", "POST", "POST"]
        record = diagnostic_records(caplog)[0]
        assert record["reason"] == "authority_retired"
        assert not reader._tasks
    finally:
        await reader.close()
        await transport.close()


@pytest.mark.asyncio
async def test_close_joins_owner_blocked_in_additional_poll(monkeypatch):
    sleep = asyncio.sleep

    async def instant(_delay):
        await sleep(0)

    monkeypatch.setattr("ring_doorbell.doorbot.asyncio.sleep", instant)
    pending = Response(content=Content(wait=asyncio.Event()))
    reader, transport, session, device = mains_fixture(
        [Response(status=404), Response(b"{}"), Response(OLD), Response(OLD), pending]
    )
    task = asyncio.create_task(reader.fetch(device))
    try:
        await asyncio.wait_for(pending.content.entered.wait(), 1)
        assert len(session.calls) == 5
        await reader.close()
        assert task.cancelled() and pending.closed and not reader._tasks
        assert await reader.fetch(device) is None
    finally:
        await reader.close()
        await transport.close()
