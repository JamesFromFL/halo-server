"""Terminal audio observations never change delivery or retirement decisions."""

import asyncio
import errno
import socket

import pytest

from halo_server.media import homekit_audio
from halo_server.media.homekit_audio import AudioEndpointRequest, AudioTrack
from halo_server.media.opus import AudioConfiguration


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,stage,reason",
    [
        ("_send_audio", "audio_send", "audio_output_failed"),
        ("_receive", "audio_feedback", "audio_feedback_failed"),
        ("_reports", "audio_reports", "audio_report_failed"),
    ],
)
@pytest.mark.parametrize("broken_reporter", [False, True])
async def test_audio_terminal_observation_keeps_original_failure_and_no_raw_log(
    method, stage, reason, broken_reporter, monkeypatch, caplog
):
    error = ConnectionRefusedError(errno.ECONNREFUSED, "untrusted-private-error-text")

    class Socket:
        def setblocking(self, _value):
            pass

        def setsockopt(self, *_values):
            pass

        def bind(self, _value):
            pass

        def connect(self, _value):
            pass

        def getsockname(self):
            return "127.0.0.1", 50000

        def gettimeout(self):
            return 0

        def recv(self, _size):
            raise error

        def fileno(self):
            return -1

        def close(self):
            pass

    monkeypatch.setattr(homekit_audio.socket, "socket", lambda *_args: Socket())
    observed, failures = [], []

    def denied(_permission):
        raise error

    def report(actual_stage, actual_reason, actual_error):
        observed.append((actual_stage, actual_reason, actual_error))
        if broken_reporter:
            raise asyncio.CancelledError("untrusted-diagnostic-error-text")

    track = AudioTrack(
        AudioEndpointRequest(50001, bytes(16), bytes(14)),
        bind=("127.0.0.1", 0),
        endpoint=("127.0.0.1", 50001),
        family=socket.AF_INET,
        excluded_ssrc=1,
        check=denied,
        epoch=lambda: 1,
        controller=None,
        boot=None,
        scope=None,
        failure=failures.append,
        presentation=lambda *_args: 0,
        touch_feedback=lambda _value: None,
        last_feedback=lambda: 0,
        diagnostic_failure=report,
    )
    track.config = AudioConfiguration(16000, 20, "cbr", 24, rtcp_interval=0.1)
    try:
        await getattr(track, method)()
        assert observed == [(stage, reason, error)]
        assert failures == [reason] and track.failure == reason
        assert track.sent_packets == track.sent_octets == 0
        assert "untrusted-private-error-text" not in caplog.text
        assert "untrusted-diagnostic-error-text" not in caplog.text
    finally:
        await track.close()
