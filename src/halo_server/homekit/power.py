"""Positive mains-only Ring hardware evidence for explicit recording warm-up."""

from halo_server.homekit.models import NativeCamera

# Reviewed against ring-doorbell 0.9.14 and the hardware sources recorded in
# S12 power-config evidence. Shared battery/plug-in product kinds stay denied.
MAINS_ONLY_KINDS = frozenset(
    {
        "hp_cam_v1",
        "floodlight_v2",
        "floodlight_pro",
        "cocoa_floodlight",
        "stickup_cam_mini",
        "stickup_cam_mini_v2",
        "stickup_cam_mini_ptz_v1",
        "hp_cam_v2",
        "spotlightw_v2",
        "stickup_cam_elite",
        "stickup_cam_wired",
        "lpd_v1",
        "lpd_v2",
        "lpd_v3",
        "lpd_v4",
        "jbox_v1",
        "doorbell_graham_cracker",
    }
)


def mains_confirmed(camera):
    return (
        isinstance(camera, NativeCamera)
        and camera.kind in MAINS_ONLY_KINDS
        and camera.capabilities.video
        and not camera.capabilities.battery
    )
