import pytest

from halo_server.incidents.coordinator import (
    IncidentActionType,
    IncidentCoordinator,
    IncidentPhase,
)


class FakeClock:
    def __init__(self, value: float = 0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def set(self, value: float) -> None:
        self.value = value


def action_types(actions):
    return [action.type for action in actions]


def test_motion_starts_capture_before_downstream_motion():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock)

    actions = coordinator.on_motion(101)

    assert action_types(actions) == [IncidentActionType.START_CAPTURE]
    snapshot = coordinator.snapshot()
    assert snapshot.phase is IncidentPhase.WARMING
    assert snapshot.generation == 1
    assert snapshot.quiet_deadline == 30
    assert snapshot.motion_published is False


def test_media_ready_publishes_motion_for_the_current_generation():
    coordinator = IncidentCoordinator(clock=FakeClock())
    coordinator.on_motion(101)

    actions = coordinator.on_media_ready(1)

    assert action_types(actions) == [
        IncidentActionType.MEDIA_READY,
        IncidentActionType.MOTION_ON,
    ]
    assert coordinator.snapshot().phase is IncidentPhase.ACTIVE


def test_distinct_motion_resets_thirty_second_quiet_deadline():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock)
    coordinator.on_motion(101)
    coordinator.on_media_ready(1)

    clock.set(29)
    assert coordinator.on_motion(102) == []
    assert coordinator.snapshot().quiet_deadline == 59

    clock.set(30)
    assert coordinator.tick() == []

    clock.set(59)
    assert action_types(coordinator.tick()) == [
        IncidentActionType.MOTION_OFF,
        IncidentActionType.STOP_CAPTURE,
    ]
    assert coordinator.snapshot().phase is IncidentPhase.IDLE


def test_duplicate_event_update_does_not_extend_quiet_deadline():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock)
    coordinator.on_motion(101)

    clock.set(20)
    actions = coordinator.on_motion(101, is_update=True)

    assert action_types(actions) == [IncidentActionType.MOTION_ON]
    assert coordinator.snapshot().quiet_deadline == 30


def test_duplicate_event_without_update_flag_is_also_deduplicated():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock)
    coordinator.on_motion(101)

    clock.set(1)
    coordinator.on_media_ready(1)
    clock.set(20)

    assert coordinator.on_motion(101) == []
    assert coordinator.snapshot().quiet_deadline == 30


def test_unseen_update_after_process_start_is_recovered_as_an_event():
    coordinator = IncidentCoordinator(clock=FakeClock())

    actions = coordinator.on_doorbell("ding-1", is_update=True)

    assert action_types(actions) == [
        IncidentActionType.START_CAPTURE,
        IncidentActionType.DOORBELL,
    ]
    assert actions[0].reason == "ring_update_recovery"


def test_duplicate_update_after_incident_end_does_not_start_another_incident():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock)
    coordinator.on_motion(101)
    clock.set(30)
    coordinator.tick()

    clock.set(31)
    assert coordinator.on_motion(101, is_update=True) == []
    assert coordinator.snapshot().phase is IncidentPhase.IDLE


def test_doorbell_is_published_immediately_without_waiting_for_media():
    coordinator = IncidentCoordinator(clock=FakeClock())

    actions = coordinator.on_doorbell("ding-1")

    assert action_types(actions) == [
        IncidentActionType.START_CAPTURE,
        IncidentActionType.DOORBELL,
    ]
    assert coordinator.snapshot().motion_published is False


def test_doorbell_joins_an_existing_motion_incident():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock)
    coordinator.on_motion("motion-1")
    coordinator.on_media_ready(1)
    clock.set(10)

    actions = coordinator.on_doorbell("ding-1")

    assert action_types(actions) == [IncidentActionType.DOORBELL]
    assert coordinator.snapshot().generation == 1
    assert coordinator.snapshot().quiet_deadline == 40


def test_readiness_timeout_publishes_motion_without_stopping_capture():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock, readiness_timeout=2)
    coordinator.on_motion(101)

    clock.set(2)
    actions = coordinator.tick()

    assert action_types(actions) == [IncidentActionType.MOTION_ON]
    assert actions[0].reason == "readiness_timeout"
    assert coordinator.snapshot().phase is IncidentPhase.DEGRADED


def test_late_media_ready_after_timeout_does_not_publish_motion_twice():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock, readiness_timeout=2)
    coordinator.on_motion(101)
    clock.set(2)
    coordinator.tick()

    actions = coordinator.on_media_ready(1)

    assert action_types(actions) == [IncidentActionType.MEDIA_READY]
    assert coordinator.snapshot().phase is IncidentPhase.ACTIVE


def test_media_failure_does_not_suppress_doorbell_or_end_incident():
    coordinator = IncidentCoordinator(clock=FakeClock())
    coordinator.on_doorbell("ding-1")

    actions = coordinator.on_media_failed(1, "negotiation failed")

    assert action_types(actions) == [IncidentActionType.MEDIA_FAILED]
    assert coordinator.snapshot().phase is IncidentPhase.DEGRADED
    assert coordinator.snapshot().failure == "negotiation failed"


def test_stale_media_callbacks_cannot_change_a_new_incident():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock)
    coordinator.on_motion(101)
    clock.set(30)
    coordinator.tick()
    coordinator.on_motion(102)

    assert coordinator.snapshot().generation == 2
    assert coordinator.on_media_ready(1) == []
    assert coordinator.on_media_failed(1, "stale failure") == []
    assert coordinator.snapshot().phase is IncidentPhase.WARMING


def test_quiet_expiry_before_readiness_does_not_emit_motion_pulse():
    clock = FakeClock()
    coordinator = IncidentCoordinator(
        clock=clock,
        quiet_period=1,
        readiness_timeout=2,
    )
    coordinator.on_motion(101)

    clock.set(1)
    actions = coordinator.tick()

    assert action_types(actions) == [IncidentActionType.STOP_CAPTURE]
    assert coordinator.snapshot().phase is IncidentPhase.IDLE


def test_signal_at_expiry_stops_old_generation_then_starts_new_one():
    clock = FakeClock()
    coordinator = IncidentCoordinator(clock=clock)
    coordinator.on_motion(101)
    coordinator.on_media_ready(1)

    clock.set(30)
    actions = coordinator.on_motion(102)

    assert action_types(actions) == [
        IncidentActionType.MOTION_OFF,
        IncidentActionType.STOP_CAPTURE,
        IncidentActionType.START_CAPTURE,
    ]
    assert coordinator.snapshot().generation == 2


def test_explicit_stop_is_idempotent():
    coordinator = IncidentCoordinator(clock=FakeClock())
    coordinator.on_motion(101)
    coordinator.on_media_ready(1)

    assert action_types(coordinator.stop()) == [
        IncidentActionType.MOTION_OFF,
        IncidentActionType.STOP_CAPTURE,
    ]
    assert coordinator.stop() == []


def test_next_deadline_prefers_readiness_then_quiet_deadline():
    coordinator = IncidentCoordinator(clock=FakeClock(), readiness_timeout=2)
    coordinator.on_motion(101)

    assert coordinator.next_deadline() == 2
    coordinator.on_media_ready(1)
    assert coordinator.next_deadline() == 30


@pytest.mark.parametrize(
    ("quiet_period", "readiness_timeout", "dedupe_window"),
    [(0, 2, 300), (30, 0, 300), (-1, 2, 300), (30, -1, 300), (30, 2, 0)],
)
def test_invalid_durations_are_rejected(quiet_period, readiness_timeout, dedupe_window):
    with pytest.raises(ValueError, match="positive"):
        IncidentCoordinator(
            quiet_period=quiet_period,
            readiness_timeout=readiness_timeout,
            dedupe_window=dedupe_window,
        )


@pytest.mark.parametrize("now", [-1, float("inf"), float("nan")])
def test_invalid_monotonic_times_are_rejected(now):
    coordinator = IncidentCoordinator()

    with pytest.raises(ValueError, match="finite and non-negative"):
        coordinator.tick(now=now)
