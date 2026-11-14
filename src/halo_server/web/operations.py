"""Bounded, owned mutations; uncertain requests are recorded and never replayed."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from uuid import UUID

from halo_server.owner_client import AdminClientError
from halo_server.private_json import PrivateJsonError, PrivateJsonStore
from halo_server.web.errors import WebError
from halo_server.web.settings import web_directory

MAX_OPERATIONS = 4096
MAX_RESULT_BYTES = 16384


def operation_id(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise WebError("bad_request", "Operation ID must be a canonical UUID") from None
    return value


class JournalError(PrivateJsonError):
    pass


@dataclass(frozen=True)
class Journal:
    version: int = 1
    revision: int = 0
    entries: tuple = ()

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise JournalError("Web operation journal version is invalid")
        if type(self.revision) is not int or not 0 <= self.revision < 2**53:
            raise JournalError("Web operation journal revision is invalid")
        if not isinstance(self.entries, tuple) or len(self.entries) > MAX_OPERATIONS:
            raise JournalError("Web operation journal capacity is invalid")
        seen = set()
        for row in self.entries:
            if not isinstance(row, dict) or set(row) != {
                "operation_id",
                "kind",
                "digest",
                "status",
                "result",
                "code",
                "possibly_sent",
            }:
                raise JournalError("Web operation journal entry is invalid")
            try:
                identity = operation_id(row["operation_id"])
            except WebError as error:
                raise JournalError("Web operation identity is invalid") from error
            if (
                identity in seen
                or not isinstance(row["kind"], str)
                or row["kind"]
                not in {"selection", "reset", "control", "warm", "configuration", "ring_logout"}
                or not isinstance(row["digest"], str)
                or len(row["digest"]) != 64
                or row["status"] not in {"pending", "completed", "rejected", "unknown"}
                or type(row["possibly_sent"]) is not bool
                or row["code"] is not None
                and not isinstance(row["code"], str)
                or len(json.dumps(row["result"]).encode()) > MAX_RESULT_BYTES
            ):
                raise JournalError("Web operation journal entry is invalid")
            seen.add(identity)

    def as_dict(self):
        return {"version": self.version, "revision": self.revision, "entries": list(self.entries)}


class JournalStore(PrivateJsonStore[Journal]):
    def __init__(self, paths):
        super().__init__(
            web_directory(paths) / "operations.json",
            snapshot_type=Journal,
            decode=lambda value: Journal(**(value | {"entries": tuple(value["entries"])})),
            default=Journal,
            max_bytes=4 * 1024 * 1024,
            label="Web operation journal",
            noun="web operations",
            error_type=JournalError,
            conflict_type=JournalError,
        )


class RpcPool:
    def __init__(self, maximum=4):
        self.semaphore = asyncio.Semaphore(maximum - 1)
        self.state_semaphore = asyncio.Semaphore(1)
        self.waiting = 0

    async def call(self, function, *args, **kwargs):
        if self.waiting >= 8:
            raise WebError("busy", "Daemon administration is busy", status=429)
        self.waiting += 1
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=5)
            except TimeoutError:
                raise WebError("busy", "Daemon administration is busy", status=429) from None
        finally:
            self.waiting -= 1
        try:
            return await function(*args, **kwargs)
        finally:
            self.semaphore.release()

    async def state(self, function):
        async with self.state_semaphore:
            return await function()


class Operations:
    def __init__(self, paths):
        self.store = JournalStore(paths)
        self.rows = {}
        self.tasks = {}
        self.workers = set()
        self.lock = asyncio.Lock()
        self.accepting = True

    async def start(self):
        def reconcile(current):
            return replace(
                current,
                entries=tuple(
                    row | {"status": "unknown", "code": "interrupted", "possibly_sent": True}
                    if row["status"] == "pending"
                    else row
                    for row in current.entries
                ),
            )

        journal = await self._persist(reconcile)
        self.rows = {row["operation_id"]: row for row in journal.entries}

    async def _persist(self, update):
        task = asyncio.create_task(asyncio.to_thread(self.store.update, update))
        self.workers.add(task)

        def finished(done):
            self.workers.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    @property
    def pending(self):
        return sum(not task.done() for task in self.tasks.values())

    async def _save(self, row):
        def save(current):
            rows = {item["operation_id"]: item for item in current.entries}
            rows[row["operation_id"]] = row
            return replace(current, entries=tuple(rows.values()))

        await self._persist(save)
        self.rows[row["operation_id"]] = row

    def get(self, identifier):
        row = self.rows.get(operation_id(identifier))
        if row is None:
            raise WebError("not_found", "Operation was not found", status=404)
        return {key: value for key, value in row.items() if key not in {"kind", "digest"}}

    async def run(self, identifier, kind, parameters, function, *, resume=False):
        identifier = operation_id(identifier)
        digest = hashlib.sha256(
            json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = self.rows.get(identifier)
        if existing is not None:
            if (existing["kind"], existing["digest"]) != (kind, digest):
                raise WebError(
                    "operation_conflict", "Operation ID was used for another request", status=409
                )
            task = self.tasks.get(identifier)
            if task is not None and not task.done():
                return await asyncio.shield(task)
            if not (resume and kind == "reset" and existing["status"] in {"pending", "unknown"}):
                return self.get(identifier)
        if not self.accepting:
            raise WebError("service_stopping", "Web mutations are draining", status=503)
        if self.lock.locked():
            raise WebError("busy", "Another owner operation is pending", status=429)
        if existing is None and len(self.rows) >= MAX_OPERATIONS:
            raise WebError("capacity", "Operation history is full; use local recovery", status=429)
        await self.lock.acquire()
        row = {
            "operation_id": identifier,
            "kind": kind,
            "digest": digest,
            "status": "pending",
            "result": None,
            "code": None,
            "possibly_sent": False,
        }
        # Publish ownership before the first await so concurrent identical requests join it.
        self.rows[identifier] = row

        async def apply():
            try:
                await self._save(row)
                result = await function()
                if len(json.dumps(result).encode()) > MAX_RESULT_BYTES:
                    raise WebError(
                        "response_too_large",
                        "Operation outcome requires a refresh",
                        possibly_sent=True,
                    )
                status = (
                    "unknown"
                    if isinstance(result, dict) and result.get("status") in {"unknown", "uncertain"}
                    else "completed"
                )
                if isinstance(result, dict) and result.get("status") == "pending":
                    status = "pending"
                if isinstance(result, dict) and result.get("status") in {"rejected", "busy"}:
                    status = "rejected"
                completed = row | {
                    "status": status,
                    "result": result,
                    "possibly_sent": status == "unknown",
                    "code": result.get("code") if isinstance(result, dict) else None,
                }
            except (AdminClientError, WebError) as error:
                certain = error.code in {
                    "bad_params",
                    "bad_request",
                    "configuration_conflict",
                    "account_changed",
                    "homekit_disabled",
                    "capability_unsupported",
                    "unsupported",
                    "managed_mode_unsupported",
                    "busy",
                    "capacity",
                    "operation_conflict",
                    "recent_auth_required",
                }
                completed = row | {
                    "status": "rejected" if certain else "unknown",
                    "code": error.code,
                    "possibly_sent": not certain,
                }
            except Exception:
                completed = row | {
                    "status": "unknown",
                    "code": "unavailable",
                    "possibly_sent": True,
                }
            try:
                await self._save(completed)
            except Exception:
                self.rows[identifier] = row | {
                    "status": "unknown",
                    "code": "journal_unavailable",
                    "possibly_sent": True,
                }
                self.accepting = False
            return self.get(identifier)

        async def owned():
            try:
                return await apply()
            finally:
                self.lock.release()

        task = asyncio.create_task(owned(), name="web-owned-mutation")
        self.tasks[identifier] = task
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    async def close(self):
        self.accepting = False
        pending = [task for task in self.tasks.values() if not task.done()]
        if pending:
            # A timed-out health drainer does not own cancellation of mutations.
            await asyncio.wait(pending)
        if self.workers:
            await asyncio.wait(tuple(self.workers))
