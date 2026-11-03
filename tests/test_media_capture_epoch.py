"""Capture clear is observable even when packet sequences remain contiguous."""

from uuid import uuid4

import pytest
from test_homekit_controller import POLICY, Backend

from halo_server.homekit.controller import HomeKitController
from halo_server.media.buffer import EncodedMediaBuffer

KEYFRAME = bytes.fromhex(
    "000000016742c00bda0507e840000003004000000300a3c50aa80000000168ce0fc800000001658884"
)


def test_packet_read_snapshot_and_live_epoch_increment_without_resetting_sequence():
    buffer = EncodedMediaBuffer()
    first = buffer.append_video(KEYFRAME, 10)
    old = buffer.read(after_sequence=None, timeout=0)
    assert first.capture_epoch == old.capture_epoch == buffer.snapshot().capture_epoch == 1
    buffer.clear()
    assert buffer.capture_epoch == 2
    assert buffer.read(after_sequence=first.sequence, timeout=0).capture_epoch == 2
    second = buffer.append_video(KEYFRAME, 11)
    current = buffer.read(after_sequence=first.sequence, timeout=0)
    assert second.sequence == first.sequence + 1
    assert second.capture_epoch == current.capture_epoch == buffer.snapshot().capture_epoch == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_read", [False, True])
async def test_native_handle_exposes_current_epoch_and_marks_read_resynchronization(
    empty_read,
):
    backend = Backend()
    controller = HomeKitController(backend)
    boot = str(uuid4())
    await controller.attach(boot, (backend.cameras[0].scope,))
    scope = await controller.update_policy(boot, controller.inventory().cameras[0].scope, POLICY)
    handle = await controller.open_live(boot, scope)
    try:
        old = await handle.read(timeout=0)
        first_sequence = old.packets[-1].sequence
        buffer = backend.resources[0].buffer
        buffer.clear()
        # This getter fences a paced, already-read AU before the next read call.
        assert handle.capture_epoch == old.capture_epoch + 1
        if empty_read:
            empty = await handle.read(timeout=0)
            assert empty.resynchronized and not empty.packets
        packet = buffer.append_video(KEYFRAME, 2)
        batch = await handle.read(timeout=0)
        assert packet.sequence == first_sequence + 1
        assert batch.capture_epoch == handle.capture_epoch == old.capture_epoch + 1
        assert batch.resynchronized is (not empty_read)
    finally:
        await controller.close()
