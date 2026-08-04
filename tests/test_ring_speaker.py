"""Contract tests for Ring WebRTC speaker signaling."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from halo_server.media import ring_speaker


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


def ready_stream():
    offered = asyncio.Event()
    offered.set()
    answered = asyncio.Event()
    answered.set()
    socket = FakeSocket()
    stream = SimpleNamespace(
        _offered_event=offered,
        _sdp_answer_event=answered,
        session_id="ring-session",
        is_alive=True,
        websocket=socket,
        dialog_id="dialog",
        device_api_id=1,
    )

    def get_session_message(method, body):
        return {
            "method": method,
            "dialog_id": stream.dialog_id,
            "body": {
                **body,
                "doorbot_id": stream.device_api_id,
                "session_id": stream.session_id,
            },
        }

    stream.get_session_message = get_session_message
    return stream, socket


@pytest.fixture(autouse=True)
def validated_version(monkeypatch):
    monkeypatch.setattr(
        ring_speaker.importlib_metadata,
        "version",
        lambda _name: "0.9.14",
    )


@pytest.mark.asyncio
async def test_speaker_control_latches_activation_for_stream_lifetime():
    stream, socket = ready_stream()
    device = SimpleNamespace(_webrtc_streams={"halo-session": stream})

    assert await ring_speaker.activate_ring_camera_speaker(device, "halo-session")
    assert not await ring_speaker.activate_ring_camera_speaker(device, "halo-session")

    assert [message["body"]["stealth_mode"] for message in socket.messages] == [False]


@pytest.mark.asyncio
async def test_speaker_control_waits_for_answer_and_ring_session_id():
    stream, socket = ready_stream()
    stream._sdp_answer_event.clear()
    stream.session_id = None
    device = SimpleNamespace(_webrtc_streams={"halo-session": stream})

    task = asyncio.create_task(
        ring_speaker.activate_ring_camera_speaker(
            device,
            "halo-session",
            timeout=0.5,
        )
    )
    await asyncio.sleep(0)
    assert socket.messages == []

    stream._sdp_answer_event.set()
    await asyncio.sleep(0)
    stream.session_id = "ring-session"

    assert await task
    assert socket.messages[0]["body"]["stealth_mode"] is False


@pytest.mark.asyncio
async def test_speaker_control_rejects_unvalidated_dependency(monkeypatch):
    stream, _socket = ready_stream()
    device = SimpleNamespace(_webrtc_streams={"halo-session": stream})
    monkeypatch.setattr(
        ring_speaker.importlib_metadata,
        "version",
        lambda _name: "0.9.15",
    )

    with pytest.raises(ring_speaker.RingSpeakerControlError, match="not validated"):
        await ring_speaker.activate_ring_camera_speaker(device, "halo-session")


@pytest.mark.asyncio
async def test_speaker_control_rejects_changed_or_missing_stream_contract():
    with pytest.raises(ring_speaker.RingSpeakerControlError, match="registry"):
        await ring_speaker.activate_ring_camera_speaker(
            SimpleNamespace(),
            "halo-session",
        )

    device = SimpleNamespace(
        _webrtc_streams={
            "halo-session": SimpleNamespace(
                _offered_event=asyncio.Event(),
            )
        }
    )
    with pytest.raises(ring_speaker.RingSpeakerControlError, match="contract changed"):
        await ring_speaker.activate_ring_camera_speaker(device, "halo-session")


@pytest.mark.asyncio
async def test_speaker_control_times_out_without_answer():
    stream, _socket = ready_stream()
    stream._sdp_answer_event.clear()
    device = SimpleNamespace(_webrtc_streams={"halo-session": stream})

    with pytest.raises(ring_speaker.RingSpeakerControlError, match="timed out"):
        await ring_speaker.activate_ring_camera_speaker(
            device,
            "halo-session",
            timeout=0.01,
        )
