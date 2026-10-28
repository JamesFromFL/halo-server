import asyncio
from enum import StrEnum

import pytest

from halo_server.api.events import EventJournal, EventJournalError


def test_journal_sequences_and_deep_copies_payloads():
    journal = EventJournal(max_events=3)
    payload = {"nested": {"value": 1}}

    event = journal.publish("motion_on", camera_id="42", payload=payload, emitted_at=100)
    payload["nested"]["value"] = 2

    assert event.sequence == 1
    assert event.epoch == journal.epoch
    assert event.payload == {"nested": {"value": 1}}
    assert journal.replay(epoch=journal.epoch, after_sequence=0) == (event,)
    with pytest.raises(TypeError):
        event.payload["nested"]["value"] = 3


def test_journal_requires_resync_after_bounded_history_gap():
    journal = EventJournal(max_events=2)
    journal.publish("one", camera_id=None, payload={})
    journal.publish("two", camera_id=None, payload={})
    journal.publish("three", camera_id=None, payload={})

    with pytest.raises(EventJournalError, match="gap"):
        journal.replay(epoch=journal.epoch, after_sequence=0)

    assert [event.sequence for event in journal.replay(epoch=journal.epoch, after_sequence=1)] == [
        2,
        3,
    ]


def test_journal_requires_resync_for_restart_epoch_or_future_sequence():
    journal = EventJournal()

    with pytest.raises(EventJournalError, match="epoch"):
        journal.replay(epoch="old", after_sequence=0)
    with pytest.raises(EventJournalError, match="gap"):
        journal.replay(epoch=journal.epoch, after_sequence=1)


@pytest.mark.asyncio
async def test_account_retirement_resets_journal_in_place_and_wakes_old_waiters():
    journal = EventJournal()
    previous_epoch = journal.epoch
    waiter = asyncio.create_task(
        journal.wait_for_events(epoch=previous_epoch, after_sequence=0, timeout=10)
    )
    await asyncio.sleep(0)

    journal.reset()

    with pytest.raises(EventJournalError, match="epoch"):
        await asyncio.wait_for(waiter, 1)
    assert journal.latest_sequence == 0
    assert journal.publish("doorbell", camera_id="new", payload={}).sequence == 1


def test_account_publication_invalidates_prebuilt_replay_before_service_handoff():
    account = ["first-profile"]
    journal = EventJournal()
    journal.bind_scope(lambda: account[0])
    event = journal.publish("motion_on", camera_id="old-camera", payload={})
    previous_epoch = event.epoch
    assert journal.replay(epoch=previous_epoch, after_sequence=0) == (event,)

    account[0] = "second-profile"

    assert journal.epoch != previous_epoch
    assert journal.latest_sequence == 0
    with pytest.raises(EventJournalError, match="epoch"):
        journal.replay(epoch=previous_epoch, after_sequence=0)


def test_account_publication_during_serialization_cannot_relabel_old_source_fact(monkeypatch):
    from halo_server.api import events

    account = ["first"]
    journal = EventJournal()
    journal.bind_scope(lambda: account[0])
    copy_payload = events._bounded_json_copy

    def replace_account(payload):
        account[0] = "second"
        return copy_payload(payload)

    monkeypatch.setattr(events, "_bounded_json_copy", replace_account)
    with pytest.raises(EventJournalError, match="authority"):
        journal.publish("doorbell", camera_id="old", payload={}, expected_scope="first")
    assert journal.latest_sequence == 0
    assert journal.replay(epoch=journal.epoch, after_sequence=0) == ()


@pytest.mark.asyncio
async def test_waiter_receives_next_event_without_polling():
    journal = EventJournal()
    waiter = asyncio.create_task(
        journal.wait_for_events(
            epoch=journal.epoch,
            after_sequence=0,
            timeout=1,
        )
    )
    await asyncio.sleep(0)

    published = journal.publish("doorbell", camera_id="42", payload={"event_id": "ding-1"})

    assert await waiter == (published,)


@pytest.mark.asyncio
async def test_waiter_timeout_returns_empty_replay():
    journal = EventJournal()

    result = await journal.wait_for_events(
        epoch=journal.epoch,
        after_sequence=0,
        timeout=0.01,
    )

    assert result == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"bad": float("nan")},
        {"bad": object()},
        {"large": "x" * (64 * 1024)},
    ],
)
def test_journal_rejects_non_json_or_oversized_payload(payload):
    journal = EventJournal()

    with pytest.raises(ValueError):
        journal.publish("event", camera_id=None, payload=payload)


def test_journal_normalizes_string_enums_at_contract_boundary():
    class EventType(StrEnum):
        MOTION = "motion_on"

    journal = EventJournal()

    event = journal.publish(EventType.MOTION, camera_id="42", payload={})

    assert event.type == "motion_on"
    assert type(event.type) is str
