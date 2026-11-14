"""Account-fenced reconciliation and bounded public projections for device state.

The catalog never actuates a device. Incomplete inventory is not removal evidence;
complete inventory marks absent devices unavailable but retains their identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from halo_server.account_scope import valid_profile_id
from halo_server.devices.models import (
    MAX_DEVICES,
    DeviceIdentity,
    DeviceObservation,
    DeviceRecord,
    valid_generation,
    valid_timestamp,
)


class CatalogError(ValueError):
    """A discovery batch was rejected without changing the catalog."""


@dataclass(frozen=True)
class _Entry:
    record: DeviceRecord
    seen_at: float
    present: bool = True


class DeviceCatalog:
    def __init__(self, profile_id: str, account_generation: int, provider_generation: int) -> None:
        if (
            not valid_profile_id(profile_id)
            or not valid_generation(account_generation)
            or not valid_generation(provider_generation)
        ):
            raise CatalogError("invalid device catalog scope")
        self.profile_id = profile_id
        self.account_generation = account_generation
        self.provider_generation = provider_generation
        self.revision = 0
        self.complete = False
        self.observed_at: float | None = None
        self._entries: dict[str, _Entry] = {}

    def apply(
        self,
        records: tuple[DeviceRecord, ...],
        *,
        complete: bool,
        revision: int,
        observed_at: float,
        source_keys: tuple[str, ...] | None = None,
    ) -> None:
        """Commit a validated cumulative inventory (or explicitly scoped inventory).

        A new catalog must be created after an account/provider generation change.
        Revision monotonicity fences queued snapshots. Optional source_keys limits
        absence reconciliation to successfully enumerated provider/location/assets.
        """
        if (
            not isinstance(records, tuple)
            or len(records) > MAX_DEVICES
            or type(complete) is not bool
            or not valid_generation(revision)
            or revision <= self.revision
            or not valid_timestamp(observed_at)
            or (self.observed_at is not None and observed_at < self.observed_at)
        ):
            raise CatalogError("invalid or obsolete device inventory")
        if source_keys is not None and (
            not isinstance(source_keys, tuple)
            or len(source_keys) > MAX_DEVICES
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for value in source_keys
            )
            or len(set(source_keys)) != len(source_keys)
        ):
            raise CatalogError("invalid discovery source scope")
        validated: dict[str, DeviceRecord] = {}
        for record in records:
            if not isinstance(record, DeviceRecord):
                raise CatalogError("invalid device record")
            # Reconstruct nested immutable DTOs to reject forged frozen instances.
            try:
                identity = DeviceIdentity(**vars(record.identity))
                observations = tuple(
                    DeviceObservation(**vars(item)) for item in record.observations
                )
                record = replace(record, identity=identity, observations=observations)
            except (TypeError, ValueError, AttributeError) as exc:
                raise CatalogError("invalid device record") from exc
            if (
                record.identity.profile_id != self.profile_id
                or record.account_generation != self.account_generation
                or record.provider_generation != self.provider_generation
            ):
                raise CatalogError("device inventory scope changed")
            if record.key in validated:
                raise CatalogError("duplicate device identity")
            if source_keys is not None and record.identity.source_key not in source_keys:
                raise CatalogError("device outside discovery source scope")
            validated[record.key] = record
        if len(set(self._entries).union(validated)) > MAX_DEVICES:
            raise CatalogError("device catalog capacity reached")
        entries = dict(self._entries)
        if complete:
            for key, entry in entries.items():
                if key not in validated and (
                    source_keys is None or entry.record.identity.source_key in source_keys
                ):
                    entries[key] = replace(entry, present=False)
        for key, record in validated.items():
            entries[key] = _Entry(record, observed_at)
        self._entries = entries
        self.revision = revision
        # Scoped discovery cannot establish whole-account completeness.
        self.complete = complete if source_keys is None else False
        self.observed_at = observed_at

    def records(self, *, present_only: bool = False) -> tuple[DeviceRecord, ...]:
        """Validated identity metadata for durable enrollment, never live state.

        Retained missing records are intentional: temporary absence cannot erase
        a paired accessory. Consumers must use snapshot() for current observations.
        """
        return tuple(
            entry.record
            for _, entry in sorted(self._entries.items())
            if not present_only or entry.present
        )

    def snapshot(self, *, now: float, max_age: float = 300.0) -> dict[str, Any]:
        if not valid_timestamp(now) or not valid_timestamp(max_age) or max_age == 0:
            raise CatalogError("invalid catalog freshness clock")
        rows = []
        for key, entry in sorted(self._entries.items()):
            record = entry.record
            expired = now < entry.seen_at or now - entry.seen_at > max_age
            stale = (
                not entry.present
                or expired
                or record.source_health
                in {
                    "offline",
                    "stale",
                    "unavailable",
                }
            )
            details = {
                item.name: item.as_dict(now=now, max_age=max_age, stale=stale)
                for item in record.observations
            }
            stale = stale or any(value["status"] == "stale" for value in details.values())
            available = None
            if not entry.present or record.source_health in {"offline", "unavailable"}:
                available = False
            elif not stale and record.source_health == "online":
                available = True
            reason = record.unavailable_reason
            if not entry.present:
                reason = "not_in_latest_inventory"
            elif expired:
                reason = "observation_expired"
            rows.append(
                {
                    "id": key,
                    "name": record.name,
                    "provider": record.identity.provider,
                    "family": record.family,
                    "kind": record.kind,
                    "model": record.model,
                    "location_id": record.identity.location_key,
                    "location_name": record.location_name,
                    "capabilities": list(record.capabilities),
                    "capability_evidence": list(record.capability_evidence),
                    "observed": {name: value["value"] for name, value in details.items()},
                    "observation_details": details,
                    "available": available,
                    "stale": stale,
                    "observed_at": entry.seen_at,
                    "source_health": "unavailable"
                    if not entry.present
                    else ("stale" if expired else record.source_health),
                    "unavailable_reason": reason,
                    "supported": record.supported,
                    "unsupported_reason": record.unsupported_reason,
                }
            )
        return {
            "revision": self.revision,
            "complete": self.complete,
            "observed_at": self.observed_at,
            "devices": rows,
        }
