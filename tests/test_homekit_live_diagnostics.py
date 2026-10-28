"""Finite live-media metadata must neither reveal secrets nor affect ownership."""

import asyncio
import errno
import logging

import pytest
from test_homekit_live import CONFIG, KEY, SALT, encoded_picture, prepared

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media import homekit_live as module


def messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == module.__name__]


@pytest.mark.asyncio
async def test_start_first_complete_frame_and_stop_are_logged_once_without_identity_or_keys(caplog):
    caplog.set_level(logging.INFO, logger=module.__name__)
    manager, controller, identity, _, receiver = await prepared()
    controller.handle.buffer.append_video(encoded_picture(), 90000)
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        async with asyncio.timeout(3):
            while not manager._sessions[identity].trace_first_frame:
                await asyncio.sleep(0.005)
        await manager.stop(controller.boot, controller.scope, identity)
        await manager.stop(controller.boot, controller.scope, identity)
        trace = messages(caplog)
        assert len(trace) == 3
        assert trace[0].startswith("HomeKit live start width=320 height=240 fps=15")
        assert "bitrate_kbps=300 mtu=1200" in trace[0]
        assert "HomeKit live first_frame mode=copy keyframe=True" in trace[1]
        assert "stage=stop reason=stop_request" in trace[2] and "first_frame=True" in trace[2]
        for private in (
            KEY.hex(),
            SALT.hex(),
            identity,
            controller.boot,
            controller.scope.profile_id,
            "127.0.0.1",
        ):
            assert private not in "\n".join(trace)
        assert controller.handle.closed and manager.pending_resources == 0
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_automatic_failure_keeps_original_outcome_but_only_logs_exception_metadata(caplog):
    caplog.set_level(logging.INFO, logger=module.__name__)
    manager, controller, identity, _, receiver = await prepared()
    ended = []

    async def notice(*arguments):
        ended.append(arguments[-1])

    manager._on_ended = notice
    secret = f"private SDP key={KEY.hex()} peer=192.0.2.44\ninjected log"
    session = manager._sessions[identity]
    try:
        manager._end(
            session, "media_failed", stage="video_send", error=OSError(errno.ECONNREFUSED, secret)
        )
        manager._end(session, "feedback_failed", stage="video_feedback", error=RuntimeError(secret))
        async with asyncio.timeout(2):
            while manager.pending_resources or not ended:
                await asyncio.sleep(0.005)
        trace = messages(caplog)
        assert len(trace) == 1
        assert "stage=video_send reason=media_failed" in trace[0]
        assert "error_class=ConnectionRefusedError" in trace[0] and "errno=111" in trace[0]
        assert "video_packets=0 video_octets=0" in trace[0]
        assert all(
            value not in trace[0] for value in (secret, KEY.hex(), "192.0.2.44", "injected log")
        )
        assert ended == ["media_failed"]
        assert manager.cleanup_failures == 0
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_unrecognized_trace_labels_and_error_codes_never_enter_logs(caplog):
    caplog.set_level(logging.INFO, logger=module.__name__)
    manager, _, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    secret = "untrusted_private_label"
    try:
        manager._trace_terminal(session, secret, secret, HomeKitOperationError(secret))
        trace = messages(caplog)
        assert len(trace) == 1
        assert "stage=other reason=other" in trace[0]
        assert "error_code=other" in trace[0]
        assert secret not in trace[0]
        session.trace_terminal = False
        unknown = type(secret, (RuntimeError,), {})(secret)
        manager._trace_terminal(session, "video_send", "media_failed", unknown)
        assert "error_class=other" in messages(caplog)[1]
        assert secret not in "\n".join(messages(caplog))
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_throwing_diagnostic_sink_cannot_change_start_or_retirement(monkeypatch):
    def broken(*_arguments, **_keywords):
        raise RuntimeError("Synthetic diagnostic sink failure")

    monkeypatch.setattr(module._log, "info", broken)
    manager, controller, identity, _, receiver = await prepared()
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        session = manager._sessions[identity]
        manager._trace_terminal(
            session, "audio_send", "audio_output_failed", RuntimeError("private")
        )
        await manager.stop(controller.boot, controller.scope, identity)
        assert controller.opens == 1 and controller.handle.closed
        assert manager.pending_resources == manager.cleanup_failures == 0
    finally:
        receiver.close()
        await manager.close()
