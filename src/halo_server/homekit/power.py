"""Recording capability and informational Ring power-source evidence."""

from halo_server.homekit.models import NativeCamera
from halo_server.ring.power import MAINS_ONLY_KINDS


def mains_confirmed(camera):
    return (
        isinstance(camera, NativeCamera)
        and camera.kind in MAINS_ONLY_KINDS
        and camera.capabilities.video
        and not camera.capabilities.battery
    )


def recording_supported(camera):
    """Recording needs real video capability, regardless of model or power source."""
    return isinstance(camera, NativeCamera) and camera.capabilities.video
