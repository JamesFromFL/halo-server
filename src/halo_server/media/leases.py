"""Expiring per-camera media lease coordination."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum


class LeasePurpose(StrEnum):
    EVENT = "event"
    HOMEKIT_LIVE = "homekit_live"
    SCRYPTED_LIVE = "scrypted_live"
    SNAPSHOT = "snapshot"
    INTERCOM = "intercom"


class LeaseActionType(StrEnum):
    START_UPSTREAM = "start_upstream"
    STOP_UPSTREAM = "stop_upstream"


@dataclass(frozen=True)
class LeasePolicy:
    default_ttl: float
    maximum_lifetime: float


DEFAULT_POLICIES: Mapping[LeasePurpose, LeasePolicy] = {
    LeasePurpose.EVENT: LeasePolicy(default_ttl=90.0, maximum_lifetime=600.0),
    LeasePurpose.HOMEKIT_LIVE: LeasePolicy(default_ttl=30.0, maximum_lifetime=600.0),
    LeasePurpose.SCRYPTED_LIVE: LeasePolicy(default_ttl=30.0, maximum_lifetime=600.0),
    LeasePurpose.SNAPSHOT: LeasePolicy(default_ttl=10.0, maximum_lifetime=30.0),
    LeasePurpose.INTERCOM: LeasePolicy(default_ttl=30.0, maximum_lifetime=300.0),
}

# Independent purposes share one upstream, while expensive/speaker consumers
# have smaller admission budgets. Renewals never reset absolute lifetimes.
DEFAULT_PURPOSE_LIMITS: Mapping[LeasePurpose, int] = {
    LeasePurpose.EVENT: 2,
    LeasePurpose.HOMEKIT_LIVE: 2,
    LeasePurpose.SCRYPTED_LIVE: 4,
    LeasePurpose.SNAPSHOT: 2,
    LeasePurpose.INTERCOM: 1,
}


def maximum_session_seconds() -> dict[str, float]:
    """Return the public absolute lifetime limit for every media purpose."""
    return {purpose.value: policy.maximum_lifetime for purpose, policy in DEFAULT_POLICIES.items()}


@dataclass(frozen=True)
class MediaLease:
    id: str
    purpose: LeasePurpose
    generation: int
    created_at: float
    expires_at: float
    absolute_deadline: float


@dataclass(frozen=True)
class LeaseAction:
    type: LeaseActionType
    generation: int
    reason: str


@dataclass(frozen=True)
class LeaseSnapshot:
    generation: int
    upstream_requested: bool
    leases: tuple[MediaLease, ...]


class MediaLeaseCoordinator:
    """Reference-count media demand while bounding every consumer lifetime."""

    def __init__(
        self,
        *,
        policies: Mapping[LeasePurpose, LeasePolicy] = DEFAULT_POLICIES,
        clock: Callable[[], float] = time.monotonic,
        max_leases: int = 32,
        purpose_limits: Mapping[LeasePurpose, int] = DEFAULT_PURPOSE_LIMITS,
    ) -> None:
        self._policies = dict(policies)
        if set(self._policies) != set(LeasePurpose):
            raise ValueError("a lease policy is required for every purpose")
        for policy in self._policies.values():
            _validate_duration(policy.default_ttl, "default lease TTL")
            _validate_duration(policy.maximum_lifetime, "maximum lease lifetime")
            if policy.default_ttl > policy.maximum_lifetime:
                raise ValueError("default lease TTL cannot exceed maximum lifetime")
        if isinstance(max_leases, bool) or not isinstance(max_leases, int) or max_leases <= 0:
            raise ValueError("maximum lease count must be a positive integer")
        self._clock = clock
        self._purpose_limits = dict(purpose_limits)
        if set(self._purpose_limits) != set(LeasePurpose) or any(
            type(limit) is not int or limit <= 0 for limit in self._purpose_limits.values()
        ):
            raise ValueError("positive consumer limits are required for every purpose")
        self._max_leases = max_leases
        self._lock = threading.RLock()
        self._generation = 0
        self._upstream_requested = False
        self._leases: dict[str, MediaLease] = {}

    def acquire(
        self,
        purpose: LeasePurpose,
        *,
        ttl: float | None = None,
        lease_id: str | None = None,
        now: float | None = None,
    ) -> tuple[MediaLease, list[LeaseAction]]:
        current_time = self._now(now)
        policy = self._policies[purpose]
        requested_ttl = policy.default_ttl if ttl is None else ttl
        _validate_duration(requested_ttl, "lease TTL")
        with self._lock:
            actions = self._expire_locked(current_time)
            if lease_id is not None and lease_id in self._leases:
                existing = self._leases[lease_id]
                if existing.purpose is not purpose:
                    raise ValueError("lease id is already used for another purpose")
                return self._renew_locked(existing, requested_ttl, current_time), actions

            normalized_id = lease_id or uuid.uuid4().hex
            if not isinstance(normalized_id, str) or not normalized_id or len(normalized_id) > 128:
                raise ValueError("lease id must be a non-empty string of at most 128 characters")
            if normalized_id in self._leases:
                raise ValueError("lease id already exists")
            if len(self._leases) >= self._max_leases:
                raise RuntimeError("camera media lease limit reached")
            if (
                sum(lease.purpose == purpose for lease in self._leases.values())
                >= self._purpose_limits[purpose]
            ):
                raise RuntimeError("camera media purpose limit reached")
            if not self._upstream_requested:
                self._generation += 1
                self._upstream_requested = True
                actions.append(
                    LeaseAction(
                        LeaseActionType.START_UPSTREAM,
                        self._generation,
                        purpose,
                    )
                )
            absolute_deadline = current_time + policy.maximum_lifetime
            lease = MediaLease(
                id=normalized_id,
                purpose=purpose,
                generation=self._generation,
                created_at=current_time,
                expires_at=min(current_time + requested_ttl, absolute_deadline),
                absolute_deadline=absolute_deadline,
            )
            self._leases[normalized_id] = lease
            return lease, actions

    def renew(
        self,
        lease_id: str,
        *,
        ttl: float | None = None,
        now: float | None = None,
    ) -> tuple[MediaLease | None, list[LeaseAction]]:
        current_time = self._now(now)
        with self._lock:
            actions = self._expire_locked(current_time)
            lease = self._leases.get(lease_id)
            if lease is None:
                return None, actions
            policy = self._policies[lease.purpose]
            requested_ttl = policy.default_ttl if ttl is None else ttl
            _validate_duration(requested_ttl, "lease TTL")
            return self._renew_locked(lease, requested_ttl, current_time), actions

    def release(
        self,
        lease_id: str,
        *,
        now: float | None = None,
    ) -> list[LeaseAction]:
        current_time = self._now(now)
        with self._lock:
            actions = self._expire_locked(current_time)
            if self._leases.pop(lease_id, None) is not None and not self._leases:
                actions.extend(self._stop_locked("last_lease_released"))
            return actions

    def expire(self, *, now: float | None = None) -> list[LeaseAction]:
        with self._lock:
            return self._expire_locked(self._now(now))

    def stop_all(self, reason: str = "service_stop") -> list[LeaseAction]:
        with self._lock:
            self._leases.clear()
            return self._stop_locked(reason)

    def restart_upstream(self, reason: str) -> list[LeaseAction]:
        """Move active demand to a new upstream generation after receiver failure."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("upstream restart reason must be a non-empty string")
        with self._lock:
            if not self._leases:
                return []
            self._generation += 1
            self._upstream_requested = True
            self._leases = {
                lease_id: replace(lease, generation=self._generation)
                for lease_id, lease in self._leases.items()
            }
            return [
                LeaseAction(
                    LeaseActionType.START_UPSTREAM,
                    self._generation,
                    reason,
                )
            ]

    def snapshot(self) -> LeaseSnapshot:
        with self._lock:
            leases = tuple(sorted(self._leases.values(), key=lambda lease: lease.id))
            return LeaseSnapshot(self._generation, self._upstream_requested, leases)

    def next_deadline(self) -> float | None:
        with self._lock:
            return min(
                (lease.expires_at for lease in self._leases.values()),
                default=None,
            )

    def _renew_locked(
        self,
        lease: MediaLease,
        ttl: float,
        now: float,
    ) -> MediaLease:
        renewed = MediaLease(
            id=lease.id,
            purpose=lease.purpose,
            generation=lease.generation,
            created_at=lease.created_at,
            expires_at=min(now + ttl, lease.absolute_deadline),
            absolute_deadline=lease.absolute_deadline,
        )
        self._leases[lease.id] = renewed
        return renewed

    def _expire_locked(self, now: float) -> list[LeaseAction]:
        expired = [lease_id for lease_id, lease in self._leases.items() if now >= lease.expires_at]
        for lease_id in expired:
            del self._leases[lease_id]
        if expired and not self._leases:
            return self._stop_locked("leases_expired")
        return []

    def _stop_locked(self, reason: str) -> list[LeaseAction]:
        if not self._upstream_requested:
            return []
        self._upstream_requested = False
        return [
            LeaseAction(
                LeaseActionType.STOP_UPSTREAM,
                self._generation,
                reason,
            )
        ]

    def _now(self, supplied: float | None) -> float:
        value = self._clock() if supplied is None else supplied
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("monotonic time must be numeric")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError("monotonic time must be finite and non-negative")
        return normalized


def _validate_duration(value: float, label: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be finite and positive")
