"""Fresh, observed Ring controls; acknowledgements never invent device state."""

from __future__ import annotations

import math

from halo_server.ring.devices import has_ring_camera_capability

CONTROL_STATE_TTL = 90.0


def observed_controls(device):
    light = siren = None
    if has_ring_camera_capability(device, "light"):
        try:
            value = device.lights
        except Exception:
            value = None
        if type(value) is str and value in {"on", "off"}:
            light = value == "on"
    if has_ring_camera_capability(device, "siren"):
        # SDK 0.9.14's public siren property returns zero for absent source
        # fields. Only an explicitly observed countdown is evidence of off.
        try:
            attributes = device._attrs
            status = attributes.get("siren_status") if isinstance(attributes, dict) else None
            value = status.get("seconds_remaining") if isinstance(status, dict) else None
        except Exception:
            value = None
        if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 86400:
            siren = value > 0
    return light, siren
