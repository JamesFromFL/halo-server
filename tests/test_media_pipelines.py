import pytest

from halo_server.media.pipelines import (
    build_hybrid_launch,
    build_passthrough_launch,
    build_transcode_launch,
    validate_launch,
)

CAPS = {
    "width": 2548,
    "height": 1440,
    "achannels": 2,
    "arate": 48000,
}


def test_passthrough_uses_encoded_h264_and_opus_with_bounded_queues():
    launch = build_passthrough_launch()

    assert "video/x-h264,stream-format=byte-stream,alignment=au" in launch
    assert "audio/x-opus" in launch
    assert "h264parse" in launch
    assert "rtph264pay" in launch
    assert "rtpopuspay" in launch
    assert "do-timestamp=false" in launch
    assert "leaky=downstream" in launch
    assert "videoconvert" not in launch
    assert "x264enc" not in launch


def test_hybrid_passes_video_and_normalizes_unfiltered_mono_opus():
    launch = build_hybrid_launch(CAPS)

    assert "video/x-h264,stream-format=byte-stream,alignment=au" in launch
    assert "videoconvert" not in launch
    assert "x264enc" not in launch
    assert "audio/x-raw,format=S16LE,layout=interleaved,channels=1,rate=48000" in launch
    assert "appsrc name=asrc is-live=true format=time do-timestamp=false" in launch
    assert "audiorate" not in launch
    assert "opusenc audio-type=generic frame-size=20 bitrate=24000" in launch
    assert "dtx=false" in launch
    assert "inband-fec=false" in launch
    for audio_filter in ("afftdn", "anlmdn", "arnndn", "highpass", "lowpass", "agate"):
        assert audio_filter not in launch


def test_transcode_normalizes_raw_audio_before_opus_encoding():
    launch = build_transcode_launch(CAPS, "x264enc")

    assert "audioresample ! audiorate skip-to-first=true ! opusenc" in launch
    assert launch.index("audiorate") < launch.index("opusenc")


@pytest.mark.parametrize(
    "launch",
    [
        build_transcode_launch(CAPS, "x264enc"),
        build_passthrough_launch(),
        build_hybrid_launch(CAPS),
    ],
)
def test_pipeline_parses_with_installed_gstreamer(launch):
    validate_launch(launch)
