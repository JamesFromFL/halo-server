"""GStreamer pipeline descriptions for Halo media output."""

from __future__ import annotations

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

ENCODER_DESCRIPTIONS = {
    "x264enc": "x264enc tune=zerolatency speed-preset=veryfast bitrate=4000 key-int-max=30",
    "vah264enc": "vah264enc",
    "nvh264enc": "nvh264enc",
}


def build_transcode_launch(caps: dict[str, int], encoder: str) -> str:
    """Return the established decoded video and normalized audio pipeline."""
    enc = ENCODER_DESCRIPTIONS[encoder]
    return (
        "( appsrc name=vsrc is-live=true format=time do-timestamp=true "
        f"caps=video/x-raw,format=RGB,width={caps['width']},height={caps['height']},"
        "framerate=30/1 "
        f"! queue ! videoconvert ! video/x-raw,format=I420 ! {enc} "
        "! h264parse config-interval=1 ! rtph264pay name=pay0 pt=96 "
        "  appsrc name=asrc is-live=true format=time do-timestamp=true "
        f"caps=audio/x-raw,format=S16LE,layout=interleaved,channels={caps['achannels']},"
        f"rate={caps['arate']} "
        "! queue ! audioconvert ! audioresample ! audiorate skip-to-first=true ! opusenc "
        "! rtpopuspay name=pay1 pt=97 )"
    )


def build_passthrough_launch() -> str:
    """Return the encoded H.264 and native Opus pipeline."""
    return (
        "( appsrc name=vsrc is-live=true format=time do-timestamp=false block=false "
        "caps=video/x-h264,stream-format=byte-stream,alignment=au "
        "! queue max-size-buffers=120 max-size-bytes=0 max-size-time=0 leaky=downstream "
        "! h264parse config-interval=-1 "
        "! rtph264pay name=pay0 pt=96 config-interval=1 "
        "  appsrc name=asrc is-live=true format=time do-timestamp=false block=false "
        "caps=audio/x-opus,channel-mapping-family=0,channels=2,rate=48000 "
        "! queue max-size-buffers=240 max-size-bytes=0 max-size-time=0 leaky=downstream "
        "! rtpopuspay name=pay1 pt=97 )"
    )


def build_hybrid_launch(caps: dict[str, int]) -> str:
    """Return passthrough video with unfiltered, normalized 20 ms Opus audio."""
    return (
        "( appsrc name=vsrc is-live=true format=time do-timestamp=false block=false "
        "caps=video/x-h264,stream-format=byte-stream,alignment=au "
        "! queue max-size-buffers=120 max-size-bytes=0 max-size-time=0 leaky=downstream "
        "! h264parse config-interval=-1 "
        "! rtph264pay name=pay0 pt=96 config-interval=1 "
        "  appsrc name=asrc is-live=true format=time do-timestamp=false block=false "
        f"caps=audio/x-raw,format=S16LE,layout=interleaved,channels={caps['achannels']},"
        f"rate={caps['arate']} "
        "! queue ! audioconvert ! audioresample "
        "! audio/x-raw,format=S16LE,layout=interleaved,channels=1,rate=48000 "
        "! opusenc audio-type=generic frame-size=20 bitrate=24000 dtx=false inband-fec=false "
        "! rtpopuspay name=pay1 pt=97 )"
    )


def validate_launch(launch: str) -> None:
    """Parse a pipeline and immediately return it to the null state."""
    Gst.init(None)
    pipeline = Gst.parse_launch(launch)
    pipeline.set_state(Gst.State.NULL)


def encoder_is_available(name: str) -> bool:
    Gst.init(None)
    return Gst.ElementFactory.find(name) is not None
