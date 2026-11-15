"""The local keypad lock can revoke a dimmer while shared receipts stay durable."""

import asyncio
from uuid import uuid4

import pytest
from test_web_http import console as console  # noqa: F401
from test_web_operations import paths as paths  # noqa: F401

from halo_server.web.errors import WebError
from halo_server.web.operations import Operations


@pytest.mark.asyncio
async def test_independent_lock_lane_preserves_journal_dedup_and_global_serialization(paths):  # noqa: F811
    operations = Operations(paths)
    await operations.start()
    entered, release = asyncio.Event(), asyncio.Event()
    lock_entered, lock_release = asyncio.Event(), asyncio.Event()
    calls = []
    dimmer_id, lock_id = str(uuid4()), str(uuid4())

    async def dimmer():
        calls.append("dimmer")
        entered.set()
        await release.wait()
        return {"status": "rejected", "code": "superseded", "possibly_sent": False}

    async def lock():
        calls.append("lock")
        lock_entered.set()
        await lock_release.wait()
        return {"enabled": True, "locked": True, "policy_revision": 2}

    async def forbidden():
        pytest.fail("A busy request must not execute")

    dimmer_task = asyncio.create_task(operations.run(dimmer_id, "control", {"target": 0}, dimmer))
    await entered.wait()
    lock_parameters = {"target": "keypad_lock", "locked": True}
    lock_task = asyncio.create_task(
        operations.run(lock_id, "configuration", lock_parameters, lock, keypad_lock=True)
    )
    try:
        await asyncio.wait_for(lock_entered.wait(), 2)
        assert operations.pending == 2 and calls == ["dimmer", "lock"]
        for kwargs in ({}, {"keypad_lock": True}):
            with pytest.raises(WebError) as error:
                await operations.run(
                    str(uuid4()), "configuration", lock_parameters, forbidden, **kwargs
                )
            assert error.value.code == "busy"
        duplicate = asyncio.create_task(
            operations.run(lock_id, "configuration", lock_parameters, lock, keypad_lock=True)
        )
        lock_release.set()
        assert (await lock_task)["result"]["locked"]
        assert await duplicate == operations.get(lock_id)
        assert not dimmer_task.done() and calls == ["dimmer", "lock"]
        release.set()
        assert (await dimmer_task)["status"] == "rejected"
        persisted = {row["operation_id"]: row for row in operations.store.load().entries}
        assert persisted[lock_id]["result"]["locked"]
        assert persisted[dimmer_id]["status"] == "rejected"
        recovered = Operations(paths)
        await recovered.start()
        assert (
            await recovered.run(
                lock_id, "configuration", lock_parameters, forbidden, keypad_lock=True
            )
        )["result"]["locked"]
        await recovered.close()
    finally:
        release.set()
        lock_release.set()
        await operations.close()


@pytest.mark.asyncio
async def test_lock_disconnect_and_drain_still_own_both_lanes(paths):  # noqa: F811
    operations = Operations(paths)
    await operations.start()
    entered, release = asyncio.Queue(), asyncio.Event()

    async def apply():
        entered.put_nowait(True)
        await release.wait()
        return {"locked": True}

    tasks = [
        asyncio.create_task(operations.run(str(uuid4()), "control", {}, apply)),
        asyncio.create_task(
            operations.run(
                str(uuid4()), "configuration", {"target": "keypad_lock"}, apply, keypad_lock=True
            )
        ),
    ]
    for _ in range(2):
        await asyncio.wait_for(entered.get(), 2)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    drain = asyncio.create_task(operations.close())
    await asyncio.sleep(0)
    assert not drain.done() and operations.pending == 2
    with pytest.raises(WebError) as error:
        await operations.run(
            str(uuid4()), "configuration", {"target": "keypad_lock"}, apply, keypad_lock=True
        )
    assert error.value.code == "service_stopping"
    release.set()
    await drain
    assert operations.pending == 0
    assert all(row["status"] == "completed" for row in operations.store.load().entries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,parameters,flag",
    [
        ("control", {"target": "keypad_lock"}, True),
        ("configuration", {"target": "keypad_policy"}, True),
        ("configuration", {"target": "keypad_lock"}, "true"),
    ],
)
async def test_lock_lane_cannot_be_reused_for_other_operations(paths, kind, parameters, flag):  # noqa: F811
    operations = Operations(paths)
    await operations.start()

    async def forbidden():
        pytest.fail("Invalid lane request executed")

    try:
        with pytest.raises(WebError) as error:
            await operations.run(str(uuid4()), kind, parameters, forbidden, keypad_lock=flag)
        assert error.value.code == "bad_request" and not operations.rows
    finally:
        await operations.close()


@pytest.mark.asyncio
async def test_authenticated_lock_route_completes_while_brightness_rpc_is_pending(console):  # noqa: F811
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def brightness(**params):
        calls.append("brightness")
        entered.set()
        await release.wait()
        return {
            "status": "rejected",
            "code": "superseded",
            "brightness": 42,
            "possibly_sent": False,
        }

    async def lock(**params):
        calls.append("lock")
        return {"enabled": True, "locked": params["locked"], "policy_revision": 2}

    console.owner.devices_keypad_brightness_set = brightness
    console.owner.devices_keypad_lock_set = lock
    headers = await console.enroll()
    scope = {"profile_id": str(uuid4()), "account_generation": 1, "device_id": "a" * 64}
    dimmer = scope | {
        "provider_generation": 1,
        "expected_epoch": 1,
        "expected_revision": 4,
        "expected_policy_revision": 1,
        "target": 0,
        "operation_id": str(uuid4()),
    }
    request = asyncio.create_task(
        console.call("POST", "/devices/keypad/brightness", json=dimmer, headers=headers)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        locking = scope | {
            "expected_policy_revision": 1,
            "locked": True,
            "operation_id": str(uuid4()),
        }
        response = await asyncio.wait_for(
            console.call("POST", "/devices/keypad/lock", json=locking, headers=headers), 2
        )
        assert response.status == 200
        receipt = (await response.json())["result"]
        assert receipt["status"] == "completed" and receipt["result"]["locked"]
        assert not request.done() and calls == ["brightness", "lock"]
        duplicate = await console.call(
            "POST", "/devices/keypad/lock", json=locking, headers=headers
        )
        assert duplicate.status == 200 and calls == ["brightness", "lock"]
    finally:
        release.set()
        await request
