# SPDX-License-Identifier: GPL-3.0-or-later
# Schema adapted from JamesFromFL/halo-gtk light_group_models.py.
"""Strict bounded REST light-group inventory and independent aggregate reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from halo_server.homekit.text import literal_text, safe_display_text
from halo_server.ring.control_routes import group_url

MAX_LOCATIONS = 16
MAX_GROUPS_PER_LOCATION = 128
MAX_GROUPS = 512
MAX_MEMBERS = 128
MAX_TOTAL_MEMBERS = 8192


class GroupSchemaError(ValueError):
    def __init__(self, code="invalid-group-schema"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GroupMember:
    api_id: str
    kind: str
    name: str


@dataclass(frozen=True)
class GroupInventory:
    location_id: str
    group_id: str
    name: str
    members: tuple[GroupMember, ...]

    @property
    def membership(self):
        return tuple((member.api_id, member.kind) for member in self.members)


@dataclass(frozen=True)
class GroupReport:
    on: bool | None
    members: tuple[tuple[str, bool | None], ...]


def identifier(value):
    try:
        group_url(value)
    except (ValueError, UnicodeError):
        raise GroupSchemaError("invalid-group-identity") from None
    return value


def api_id(value, *, report=False):
    if report:
        if (
            type(value) is not str
            or not value.isascii()
            or not value.isdigit()
            or not 1 <= len(value) <= 19
            or value.startswith("0")
        ):
            raise GroupSchemaError("invalid-group-member")
        value = int(value)
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise GroupSchemaError("invalid-group-member")
    return str(value)


def _row(value):
    if type(value) is not dict:
        raise GroupSchemaError()
    return value


def _rows(value, key, limit, *, depth=0):
    row = _row(value)
    rows = row.get(key)
    if type(rows) is not list or len(rows) > limit:
        raise GroupSchemaError("group-response-limit")
    require_complete_inventory(row, len(rows), depth)
    return rows


def require_complete_inventory(row, count, depth=0):
    for key in (
        "has_more",
        "hasMore",
        "has_next_page",
        "truncated",
        "is_truncated",
        "partial",
        "incomplete",
    ):
        if key in row and row[key] is not False:
            raise GroupSchemaError("incomplete-group-inventory")
    for key in ("complete", "is_complete"):
        if key in row and row[key] is not True:
            raise GroupSchemaError("incomplete-group-inventory")
    for key in (
        "next",
        "next_page",
        "next_page_token",
        "next_cursor",
        "cursor",
        "continuation_token",
        "next_page_url",
        "nextPage",
    ):
        if key in row and row[key] not in (None, ""):
            raise GroupSchemaError("incomplete-group-inventory")
    for key in ("total", "total_count", "total_entries", "total_results", "count"):
        if key in row and (type(row[key]) is not int or row[key] != count):
            raise GroupSchemaError("incomplete-group-inventory")
    for key in ("offset", "page"):
        if key in row and (type(row[key]) is not int or row[key] != (1 if key == "page" else 0)):
            raise GroupSchemaError("incomplete-group-inventory")
    for key in ("pagination", "paging", "links"):
        if key in row and row[key] not in (None, {}, []):
            raise GroupSchemaError("incomplete-group-inventory")
    if any(key in row for key in ("limit", "page_size", "pageSize", "total_pages", "totalPages")):
        raise GroupSchemaError("incomplete-group-inventory")
    if row.get("meta") is not None:
        if depth >= 4:
            raise GroupSchemaError("incomplete-group-inventory")
        require_complete_inventory(_row(row["meta"]), count, depth + 1)


def _deleted(row):
    stamp = row.get("deleted_at")
    if stamp is None:
        return False
    if type(stamp) is not str or len(stamp) > 80:
        raise GroupSchemaError("invalid-group-deletion")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise GroupSchemaError("invalid-group-deletion") from None
    return True


def parse_groups(payload, location_id):
    location_id = identifier(location_id)
    payload = _row(payload)
    if "location_id" in payload and payload["location_id"] != location_id:
        raise GroupSchemaError("group-location-mismatch")
    output, seen = [], set()
    for value in _rows(payload, "device_groups", MAX_GROUPS_PER_LOCATION):
        row = _row(value)
        group_id = identifier(row.get("device_group_id"))
        if group_id in seen or row.get("location_id") != location_id:
            raise GroupSchemaError("ambiguous-group-identity")
        seen.add(group_id)
        if _deleted(row):
            continue
        members, member_ids = [], set()
        for value in _rows(row, "devices", MAX_MEMBERS):
            member = _row(value)
            member_id = api_id(member.get("doorbot_id"))
            if member_id in member_ids or member.get("location_id") != location_id:
                raise GroupSchemaError("ambiguous-group-member")
            member_ids.add(member_id)
            kind = member.get("type")
            if not literal_text(kind, 128) or not kind:
                raise GroupSchemaError("invalid-group-member-kind")
            if not _deleted(member):
                members.append(GroupMember(member_id, kind, _name(member.get("name"), "Device")))
        output.append(
            GroupInventory(
                location_id,
                group_id,
                _name(row.get("name"), "Light group"),
                tuple(sorted(members, key=lambda row: row.api_id)),
            )
        )
    return tuple(sorted(output, key=lambda row: row.group_id))


def parse_report(payload, inventory):
    row = _row(payload)
    if row.get("device_group_id") != inventory.group_id or (
        "location_id" in row and row["location_id"] != inventory.location_id
    ):
        raise GroupSchemaError("group-report-identity-mismatch")
    members, seen = [], set()
    for value in _rows(row, "devices", MAX_MEMBERS):
        member = _row(value)
        member_id = api_id(member.get("id"), report=True)
        if member_id in seen or (
            "location_id" in member and member["location_id"] != inventory.location_id
        ):
            raise GroupSchemaError("ambiguous-group-member")
        seen.add(member_id)
        if not _deleted(member):
            members.append((member_id, _boolean(member.get("lights_on"))))
    if {member_id for member_id, _ in members} != {member.api_id for member in inventory.members}:
        raise GroupSchemaError("group-membership-mismatch")
    return GroupReport(_boolean(row.get("lights_on")), tuple(sorted(members)))


def _boolean(value):
    return value if type(value) is bool else None


def _name(value, fallback):
    return safe_display_text(value, 256) if type(value) is str and value.strip() else fallback
