"""Small management projection; never traverses media diagnostics or opens media."""

from __future__ import annotations

import json
import time

from halo_server import __version__
from halo_server.api.admin import AdminProtocolError
from halo_server.homekit.models import CAPABILITIES
from halo_server.release_execution import release_status

MAX_OWNER_STATE_BYTES = 128 * 1024


def project_owner_state(service):
    context = service._current_account_scope()
    inventory = service._owner_admin.control_backend.inventory()
    scope = {"profile_id": inventory.profile_id, "account_generation": inventory.account_generation}
    bridge = service._homekit_bridge
    homekit = (
        bridge.owner_summary()
        if bridge is not None
        else {
            "enabled": False,
            "adapter_boot_id": None,
            "state": "disabled",
            "registry_revision": None,
            "selected_count": 0,
            "published_count": None,
            "recording_revision": None,
        }
    )
    selected = (
        {row.camera_id for row in bridge._registry.selected(inventory.profile_id)}
        if bridge is not None and inventory.profile_id is not None
        else set()
    )
    all_published = homekit["published_count"] == len(selected) and homekit["state"] == "published"
    devices = []
    for camera in inventory.cameras:
        source = service._owner_admin.control_backend._present.get(camera.scope.camera_id)
        light, siren = service._observed_camera_controls(
            camera.scope.camera_id, source[0] if source else None
        )
        settings = (
            bridge._recording_settings.camera(inventory.profile_id, camera.scope.camera_id)
            if bridge
            else None
        )
        devices.append(
            {
                "id": camera.scope.camera_id,
                "camera_id": camera.scope.camera_id,
                "name": camera.name,
                "source": "ring",
                "kind": "doorbell" if camera.capabilities.doorbell else "camera",
                "model": camera.model,
                "scope": scope | {"device_generation": camera.scope.device_generation},
                "capabilities": sorted(
                    key for key in CAPABILITIES if getattr(camera.capabilities, key)
                ),
                "observed": {
                    "source_available": camera.connection_state,
                    "battery_percent": camera.battery_percent,
                    "light": light,
                    "siren": siren,
                },
                "homekit": {
                    "selected": camera.scope.camera_id in selected,
                    # Aggregate publication proves individual publication only when all match.
                    "published": (True if all_published else None)
                    if camera.scope.camera_id in selected
                    else False,
                    "paired": None,
                    "owner_warm": settings.owner_warm if settings else False,
                    "recording_active": settings.preferences.active if settings else False,
                    "recording_audio_active": settings.preferences.audio_active
                    if settings
                    else False,
                },
            }
        )
    activity = service._owner_activity.snapshot()
    result = {
        "schema": 1,
        "epoch": f"{service._owner_boot_id}:{activity['epoch']}",
        "observed_at": time.time(),
        "daemon": {
            "boot_id": service._owner_boot_id,
            "version": __version__,
            "uptime_seconds": max(0, time.monotonic() - service._started_at)
            if service._started_at is not None
            else None,
            "stopping": service._stop_requested.is_set(),
            "release": release_status(),
        },
        "ring": {
            "authenticated": inventory.profile_id is not None
            and service._account_authority_current(),
            "restoring": service._restoring,
            "events": str(service._event_state)
            if service._account_authority_current()
            else "offline",
            "inventory_complete": inventory.complete,
        },
        "scope": scope,
        "homekit": homekit,
        "devices": devices,
        "noncamera": service._device_monitor.snapshot(),
        "activity": activity,
        "truncated": False,
    }

    # Names can occupy up to six JSON bytes per character. Bound the wire size,
    # not merely the camera count, and never present a cropped list as complete.
    def wire_size(value):
        return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())

    size = wire_size(result)
    while size > MAX_OWNER_STATE_BYTES:
        result["truncated"] = True
        if activity["entries"]:
            removed = activity["entries"].pop(0)
            activity["truncated"] = True
        elif result["noncamera"]["devices"]:
            removed = result["noncamera"]["devices"].pop()
            result["noncamera"]["truncated"] = True
            result["noncamera"]["complete"] = False
        elif devices:
            removed = devices.pop()
        else:
            raise ValueError("Owner state metadata exceeds its byte budget")
        # Ignoring removed separators slightly overestimates, staying conservative.
        size -= wire_size(removed)
    if context != service._current_account_scope():
        raise AdminProtocolError("account_changed", "Ring account changed during observation")
    return result
