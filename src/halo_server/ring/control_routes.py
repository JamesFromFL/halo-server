"""Exact REST route/body schemas for qualified Chime and lighting controls.

This module grants no device authority. Callers must qualify current ownership,
model and account state before the shared transport admits a request.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote, unquote

API_ORIGIN = "https://api.ring.com"
ON_DURATIONS = frozenset({30, 60, 300, 900})
_CHIME = re.compile(
    r"https://api\.ring\.com/clients_api/chimes/([1-9][0-9]{0,18})/(health|play_sound|do_not_disturb)?"
)
_SETTINGS = re.compile(r"https://api\.ring\.com/devices/v1/devices/([1-9][0-9]{0,18})/settings")
_GROUP = re.compile(
    r"https://api\.ring\.com/groups/v1/locations/([^/]+)/groups(?:/([^/]+)/devices)?"
)


def device_url(api_id):
    if type(api_id) is not int or not 1 <= api_id <= 2**63 - 1:
        raise ValueError("invalid-request")
    return f"{API_ORIGIN}/clients_api/chimes/{api_id}/"


def settings_url(api_id):
    device_url(api_id)
    return f"{API_ORIGIN}/devices/v1/devices/{api_id}/settings"


def _segment(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or value in {".", ".."}
        or any(char in "/\\%" or unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
        or len(value.encode("utf-8")) > 512
    ):
        raise ValueError("invalid-request")
    return quote(value, safe="", encoding="utf-8", errors="strict")


def group_url(location_id, group_id=None):
    url = f"{API_ORIGIN}/groups/v1/locations/{_segment(location_id)}/groups"
    if group_id is not None:
        url += f"/{_segment(group_id)}/devices"
    return url


def _route(url):
    if type(url) is not str or len(url) > 4096:
        return None
    match = _CHIME.fullmatch(url)
    if match and int(match[1]) <= 2**63 - 1:
        return "chime", match[2]
    match = _SETTINGS.fullmatch(url)
    if match and int(match[1]) <= 2**63 - 1:
        return "settings", None
    match = _GROUP.fullmatch(url)
    if match:
        try:
            location = unquote(match[1], encoding="utf-8", errors="strict")
            group = unquote(match[2], encoding="utf-8", errors="strict") if match[2] else None
            if group_url(location, group) == url:
                return "group", "report" if group is not None else "list"
        except (ValueError, UnicodeError):
            pass
    return None


def allowed_read(url):
    return _route(url) in {
        ("chime", "health"),
        ("settings", None),
        ("group", "list"),
        ("group", "report"),
    }


def allowed_write(url, method, payload):
    """Return only for a complete minimal known body, never arbitrary settings."""
    route = _route(url)
    if route == ("chime", None) and method == "PUT":
        if type(payload) is not dict or set(payload) != {"chime"}:
            return False
        chime = payload["chime"]
        if type(chime) is not dict or set(chime) != {"settings"}:
            return False
        settings = chime["settings"]
        return (
            type(settings) is dict
            and set(settings) == {"volume"}
            and type(settings["volume"]) is int
            and 0 <= settings["volume"] <= 11
        )
    if route == ("chime", "play_sound") and method == "POST":
        return (
            type(payload) is dict
            and set(payload) == {"kind"}
            and type(payload["kind"]) is str
            and payload["kind"] in {"ding", "motion"}
        )
    if route == ("chime", "do_not_disturb") and method == "POST":
        # The upstream clear operation deliberately has no JSON body.
        return payload is None or (
            type(payload) is dict
            and set(payload) == {"time"}
            and type(payload["time"]) is int
            and 1 <= payload["time"] <= 1440
        )
    if route == ("settings", None) and method == "PATCH":
        if type(payload) is not dict or set(payload) != {"night_light_settings"}:
            return False
        settings = payload["night_light_settings"]
        return (
            type(settings) is dict
            and set(settings) == {"light_sensor_enabled"}
            and type(settings["light_sensor_enabled"]) is bool
        )
    if route == ("group", "report") and method == "POST":
        if type(payload) is not dict or set(payload) != {"lights_on"}:
            return False
        settings = payload["lights_on"]
        if type(settings) is not dict or type(settings.get("enabled")) is not bool:
            return False
        if settings["enabled"]:
            return (
                set(settings) == {"enabled", "duration_seconds"}
                and type(settings["duration_seconds"]) is int
                and settings["duration_seconds"] in ON_DURATIONS
            )
        return set(settings) == {"enabled"}
    return False
