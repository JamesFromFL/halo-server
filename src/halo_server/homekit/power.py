"""Positive mains-only Ring hardware evidence for explicit recording warm-up."""

from halo_server.homekit.models import NativeCamera
from halo_server.ring.power import MAINS_ONLY_KINDS


def mains_confirmed(camera):
    return (
        isinstance(camera, NativeCamera)
        and camera.kind in MAINS_ONLY_KINDS
        and camera.capabilities.video
        and not camera.capabilities.battery
    )
