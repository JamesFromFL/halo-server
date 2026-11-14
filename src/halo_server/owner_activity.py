"""Bounded, content-free owner activity fenced by the Ring account epoch."""

from __future__ import annotations

import contextlib

from halo_server.api.events import EventJournal, EventJournalError

CAPACITY = 256
OWNER_ACTIONS = frozenset(
    {
        "login",
        "logout",
        "configuration_set",
        "devices_monitoring_set",
        "devices_alarm_policy_set",
        "devices_alarm_mode_set",
        "devices_bridge_set",
        "devices_bridge_publication_set",
        "devices_bridge_pairing_reveal",
        "devices_bridge_pairing_reset",
        "camera_control",
        "homekit_select",
        "homekit_recording_warm",
        "homekit_pairing_reveal",
        "homekit_pairing_reset",
        "plugin_pair",
        "plugin_revoke",
        "shutdown",
    }
)
OUTCOMES = frozenset({"accepted", "rejected", "unknown", "pending", "completed"})


class OwnerActivity:
    """No arbitrary messages, arguments, identifiers, or credentials are accepted."""

    def __init__(self, scope_provider):
        self._journal = EventJournal(max_events=CAPACITY)
        self._journal.bind_scope(scope_provider)

    def reset(self):
        self._journal.reset()

    @property
    def epoch(self):
        return self._journal.epoch

    def record(self, category, action, outcome, *, camera_id=None, expected_scope=None):
        allowed = (
            category == "owner"
            and action in OWNER_ACTIONS
            and outcome in OUTCOMES
            or category == "connection"
            and action == "ring_events"
            and outcome in {"offline", "connecting", "connected", "disconnected", "error"}
            or category == "event"
            and action in {"motion", "doorbell"}
            and outcome == "received"
        )
        if not allowed:
            raise ValueError("Unknown owner activity category")
        if camera_id is not None and (
            not isinstance(camera_id, str) or not 1 <= len(camera_id) <= 128
        ):
            camera_id = None
        # A retired account must never publish into its successor's feed.
        with contextlib.suppress(EventJournalError):
            self._journal.publish(
                category,
                camera_id=camera_id,
                payload={"action": action, "outcome": outcome},
                expected_scope=expected_scope,
            )

    def snapshot(self):
        journal = self._journal
        journal.sync_authority()
        # All access is on the daemon event loop. Copy only the bounded allowlist.
        return {
            "epoch": journal.epoch,
            "cursor": journal.latest_sequence,
            "capacity": CAPACITY,
            "truncated": journal.latest_sequence > CAPACITY,
            "entries": [
                {
                    "sequence": event.sequence,
                    "at": event.emitted_at,
                    "category": event.type,
                    "action": event.payload["action"],
                    "outcome": event.payload["outcome"],
                    "camera_id": event.camera_id,
                }
                for event in journal._events
            ],
        }
