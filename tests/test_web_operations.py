"""Browser disconnect, receipt recovery and shared state resource regressions."""

import asyncio
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from halo_server.owner_client import AdminClientError
from halo_server.paths import ServerPaths
from halo_server.web.errors import WebError
from halo_server.web.operations import Operations, RpcPool
from halo_server.web.ring_login import RingLogin
from halo_server.web.state import StateHub


@pytest.fixture
def paths(tmp_path):
    result = ServerPaths(tmp_path / "state", tmp_path / "run")
    result.prepare()
    return result


@pytest.mark.asyncio
async def test_disconnect_and_cancelled_drain_do_not_cancel_or_repeat_mutation(paths):
    operations = Operations(paths)
    await operations.start()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def apply():
        calls.append("sent")
        entered.set()
        await release.wait()
        return {"revision": 2}

    identifier = str(uuid4())
    request = asyncio.create_task(operations.run(identifier, "selection", {"camera": "a"}, apply))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    drain = asyncio.create_task(operations.close())
    await asyncio.sleep(0)
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    assert operations.pending == 1 and calls == ["sent"]
    release.set()
    await operations.close()
    assert operations.get(identifier)["status"] == "completed"
    recovered = Operations(paths)
    await recovered.start()
    receipt = await recovered.run(identifier, "selection", {"camera": "a"}, apply)
    assert receipt["result"] == {"revision": 2} and calls == ["sent"]
    with pytest.raises(WebError) as error:
        await recovered.run(identifier, "selection", {"camera": "b"}, apply)
    assert error.value.code == "operation_conflict"
    await recovered.close()


@pytest.mark.asyncio
async def test_cancelled_startup_tracks_and_waits_for_real_journal_writer(paths, monkeypatch):
    operations = Operations(paths)
    entered, release = threading.Event(), threading.Event()
    update = operations.store.update

    def delayed(function):
        entered.set()
        release.wait(5)
        return update(function)

    monkeypatch.setattr(operations.store, "update", delayed)
    startup = asyncio.create_task(operations.start())
    await asyncio.to_thread(entered.wait, 2)
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup
    stopping = asyncio.create_task(operations.close())
    await asyncio.sleep(0)
    assert not stopping.done() and operations.workers
    release.set()
    await stopping
    assert not operations.workers


@pytest.mark.asyncio
async def test_pending_reset_requires_explicit_same_target_resume(paths):
    operations = Operations(paths)
    await operations.start()
    identifier, count = str(uuid4()), 0

    async def reset():
        nonlocal count
        count += 1
        return {"status": "pending" if count == 1 else "completed"}

    assert (await operations.run(identifier, "reset", {"target": "a"}, reset))[
        "status"
    ] == "pending"
    assert (await operations.run(identifier, "reset", {"target": "a"}, reset))[
        "status"
    ] == "pending"
    assert count == 1
    recovered = Operations(paths)
    await recovered.start()
    assert recovered.get(identifier)["status"] == "unknown"
    assert (await recovered.run(identifier, "reset", {"target": "a"}, reset, resume=True))[
        "status"
    ] == "completed"
    assert count == 2
    await operations.close()
    await recovered.close()


@pytest.mark.asyncio
async def test_lost_reply_is_unknown_and_is_never_automatically_resent(paths):
    operations = Operations(paths)
    await operations.start()
    calls = []

    async def uncertain():
        calls.append(True)
        raise AdminClientError("request_failed", "synthetic transport failure")

    identifier = str(uuid4())
    result = await operations.run(identifier, "warm", {}, uncertain)
    assert result["status"] == "unknown" and result["possibly_sent"]
    await operations.run(identifier, "warm", {}, uncertain)
    assert len(calls) == 1
    await operations.close()


class StateClient:
    def __init__(self):
        self.epoch = "account-a"
        self.available = True
        self.calls = 0
        self.gate = None

    async def owner_state(self):
        self.calls += 1
        if self.gate:
            await self.gate.wait()
        if not self.available:
            raise OSError("offline")
        return {"schema": 1, "epoch": self.epoch, "observed_at": 123, "devices": []}


@pytest.mark.asyncio
async def test_state_coalesces_disconnect_invalidates_and_epoch_resets_replay():
    client, pool = StateClient(), RpcPool()
    client.gate = asyncio.Event()
    hub = StateHub(client, pool)
    reads = [asyncio.create_task(hub.get(force=True)) for _ in range(12)]
    await asyncio.sleep(0)
    client.gate.set()
    await asyncio.gather(*reads)
    assert client.calls == 1
    old_cursor = hub.current["cursor"]
    disconnects, epochs = [], []
    hub.on_disconnect = lambda: disconnects.append(True)
    hub.on_epoch = epochs.append
    client.available = False
    stale = await hub.get(force=True)
    assert stale["daemon"]["stale"] and stale["state"]["epoch"] == "account-a"
    assert disconnects == [True]
    client.available, client.epoch = True, "account-b"
    await hub.get(force=True)
    queue = await hub.subscribe(old_cursor)
    assert (await queue.get())[0] == "resync" and epochs == ["account-b"]
    assert len(hub.history) == 1
    hub.unsubscribe(queue)
    await hub.close()


@pytest.mark.asyncio
async def test_details_rpc_saturation_does_not_mark_state_offline():
    client, pool = StateClient(), RpcPool()
    entered, release = asyncio.Queue(), asyncio.Event()

    async def detail():
        entered.put_nowait(True)
        await release.wait()

    tasks = [asyncio.create_task(pool.call(detail)) for _ in range(4)]
    for _ in range(3):
        await entered.get()
    hub = StateHub(client, pool)
    assert (await hub.get())["daemon"]["available"]
    release.set()
    await asyncio.gather(*tasks)
    await hub.close()


@pytest.mark.asyncio
async def test_slow_subscribers_are_bounded_and_disconnected():
    hub = StateHub(StateClient(), RpcPool())
    queues = [await hub.subscribe() for _ in range(8)]
    with pytest.raises(WebError) as error:
        await hub.subscribe()
    assert error.value.code == "capacity"
    for _ in range(16):
        await hub.get(force=True)
    assert not hub.subscribers and all(queue.get_nowait() is None for queue in queues)
    await hub.close()


@pytest.mark.asyncio
async def test_parallel_mfa_submission_sends_one_fenced_request_and_retains_ownership():
    state = StateHub(StateClient(), RpcPool())
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def login(email, password, otp, *, expected_epoch):
        calls.append((otp, expected_epoch))
        if otp is None:
            raise AdminClientError("two_factor_required", "code required")
        entered.set()
        await release.wait()

    ring = RingLogin(SimpleNamespace(login=login), RpcPool(), state)
    session = SimpleNamespace(id="browser")
    challenge = await ring.begin(session, "test@example.test", "ring-test-password", "account-a")
    assert challenge["status"] == "mfa_required"
    outcomes = [
        asyncio.create_task(ring.continue_(session, challenge["challenge_id"], "123456"))
        for _ in range(2)
    ]
    await entered.wait()
    ring.cancel(session, challenge["challenge_id"])
    stopping = asyncio.create_task(ring.close())
    await asyncio.sleep(0)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert ring.task and not ring.task.done()
    release.set()
    results = await asyncio.gather(*outcomes, return_exceptions=True)
    assert sum(isinstance(row, WebError) and row.code == "busy" for row in results) == 1
    assert calls == [(None, "account-a"), ("123456", "account-a")]
    await ring.close()
    await state.close()


@pytest.mark.asyncio
async def test_mfa_is_bound_to_browser_and_expires_without_leaving_credentials():
    state = StateHub(StateClient(), RpcPool())
    now = [100.0]

    async def login(*args, **kwargs):
        raise AdminClientError("two_factor_required", "code required")

    ring = RingLogin(SimpleNamespace(login=login), RpcPool(), state, clock=lambda: now[0])
    challenge = await ring.begin(
        SimpleNamespace(id="one"), "test@example.test", "secret", "account-a"
    )
    original = ring.challenge
    with pytest.raises(WebError) as error:
        await ring.continue_(SimpleNamespace(id="two"), challenge["challenge_id"], "123456")
    assert error.value.code == "challenge_expired" and ring.challenge is original
    now[0] += 301
    with pytest.raises(WebError):
        await ring.continue_(SimpleNamespace(id="one"), challenge["challenge_id"], "123456")
    assert ring.challenge is None and original.password == original.email == ""
    await ring.close()
    await state.close()
