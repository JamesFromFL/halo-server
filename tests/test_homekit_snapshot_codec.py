"""Actual JPEG processing, strict finite admission and native process ownership."""

import asyncio
import base64
import signal
import struct
import tracemalloc
from fractions import Fraction
from io import BytesIO
from pathlib import Path

import av
import numpy as np
import pytest

from halo_server.media import snapshot_codec as codec
from halo_server.media.jpeg import JpegError, SnapshotConfiguration, inspect_jpeg
from halo_server.media.snapshot_codec import SnapshotCodecError, SnapshotWorker

PROGRESSIVE = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIs"
    "IxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wgARCAAMABADASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAA"
    "AAX/xAAVAQEBAAAAAAAAAAAAAAAAAAAFBv/aAAwDAQACEAMQAAABlg2g/8QAFBABAAAAAAAAAAAAAAAAAAAAIP/a"
    "AAgBAQABBQIf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/a"
    "AAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAIP/aAAgBAQAGPwIf/8QAFBABAAAAAAAAAAAAAAAAAAAAIP/a"
    "AAgBAQABPyEf/9oADAMBAAIAAwAAABAL/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAA"
    "AAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAIP/aAAgBAQABPxAf/9k="
)

GRAYSCALE = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIs"
    "IxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAAMABABAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcI"
    "CQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcY"
    "GRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKj"
    "pKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/AOPo"
    "oor/2Q=="
)


def jpeg_source(width=640, height=360, *, noise=False):
    if noise:
        pixels = np.random.default_rng(93).integers(0, 256, (height, width, 3), dtype=np.uint8)
    else:
        pixels = np.empty((height, width, 3), dtype=np.uint8)
        pixels[:] = [190, 50, 20]
    frame = av.VideoFrame.from_ndarray(pixels, format="rgb24").reformat(
        format="yuvj420p", threads=1
    )
    encoder = av.CodecContext.create("mjpeg", "w")
    encoder.thread_count = 1
    encoder.width = width
    encoder.height = height
    encoder.pix_fmt = "yuvj420p"
    encoder.time_base = Fraction(1, 1)
    encoder.global_quality = 5 * 118
    encoder.flags |= 2
    frame.pts = 0
    return b"".join(bytes(packet) for packet in encoder.encode(frame) + encoder.encode(None))


def rgb(image):
    decoder = av.CodecContext.create("mjpeg", "r")
    decoder.thread_count = 1
    frames = decoder.decode(av.Packet(image)) + decoder.decode(None)
    assert len(frames) == 1 and not frames[0].is_corrupt
    return frames[0].reformat(format="rgb24", threads=1).to_ndarray()


def exif(orientation):
    tiff = (
        b"II" + struct.pack("<HIH", 42, 8, 1) + struct.pack("<HHIHHI", 274, 3, 1, orientation, 0, 0)
    )
    body = b"Exif\0\0" + tiff
    return b"\xff\xe1" + struct.pack("!H", len(body) + 2) + body


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, PROGRESSIVE, GRAYSCALE])
@pytest.mark.parametrize("dimensions", [(1, 1), (333, 187), (640, 480), (4096, 1), (1920, 1080)])
async def test_real_baseline_progressive_and_grayscale_exact_geometry_and_independent_decode(
    source, dimensions
):
    source = source if source is not None else jpeg_source()
    metadata = inspect_jpeg(source)
    if source == PROGRESSIVE:
        assert metadata.progressive and metadata.scans > 1
    worker = SnapshotWorker()
    baseline = codec._WORKERS
    try:
        result = await worker.convert(source, SnapshotConfiguration(*dimensions))
        actual = inspect_jpeg(result)
        assert (actual.width, actual.height) == dimensions
        assert not actual.progressive and actual.orientation == 1
        pixels = rgb(result)
        assert pixels.shape == (dimensions[1], dimensions[0], 3)
        assert len(result) <= 8 * 1024 * 1024
        try:
            from PIL import Image
        except ImportError:
            pass
        else:
            assert Image.open(BytesIO(result)).size == dimensions
        assert worker.pid is None and baseline == codec._WORKERS
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_letterbox_preserves_picture_aspect_and_black_border_pixels():
    worker = SnapshotWorker()
    try:
        result = await worker.convert(jpeg_source(), SnapshotConfiguration(640, 480))
        pixels = rgb(result)
        assert np.max(pixels[0, 0]) < 8 and np.max(pixels[479, 639]) < 8
        assert np.max(np.abs(pixels[240, 320].astype(int) - [190, 50, 20])) < 8
        assert np.max(pixels[30, 320]) < 8
        assert pixels[90, 320, 0] > 170
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_source_uhd_admitted_but_requested_wire_geometry_uses_existing_decimal_pixel_cap():
    worker = SnapshotWorker()
    try:
        result = await worker.convert(jpeg_source(3840, 2160), SnapshotConfiguration(1920, 1080))
        assert (inspect_jpeg(result).width, inspect_jpeg(result).height) == (1920, 1080)
        with pytest.raises(JpegError):
            SnapshotConfiguration(3840, 2160)
    finally:
        await worker.close()


@pytest.mark.parametrize(
    "dimensions", [(0, 1), (True, 1), (4097, 1), (4096, 4096), (-1, 1), (1.0, 3)]
)
def test_invalid_or_unqualified_output_geometry_is_rejected(dimensions):
    with pytest.raises(JpegError):
        SnapshotConfiguration(*dimensions)


@pytest.mark.parametrize("orientation", [2, 3, 4, 5, 6, 7, 8, 0, 65535])
def test_nonidentity_exif_orientation_fails_before_native_decoder(orientation):
    source = jpeg_source(32, 24)
    with pytest.raises(JpegError) as error:
        inspect_jpeg(source[:2] + exif(orientation) + source[2:])
    assert error.value.code == "unsupported_orientation"


@pytest.mark.asyncio
async def test_identity_exif_is_preserved_when_copying_without_enumerating_side_data():
    source = jpeg_source(32, 24)
    source = source[:2] + exif(1) + source[2:]
    assert inspect_jpeg(source).orientation == 1
    worker = SnapshotWorker()
    try:
        image = await worker.convert(source, SnapshotConfiguration(32, 24))
        assert image == source
        assert np.max(np.abs(rgb(image)[12, 16].astype(int) - [190, 50, 20])) < 8
    finally:
        await worker.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "pixel_bomb",
        "precision",
        "cmyk",
        "lossless",
        "duplicate_frame",
        "zero_quantization",
        "quantization_precision",
        "bad_huffman",
        "baseline_huffman_selector",
        "unknown_component",
        "truncated_segment",
        "tail",
        "multiple_images",
        "restart_outside_scan",
    ],
)
def test_structurally_unsafe_or_unsupported_source_fails_before_native_allocation(mutation):
    source = jpeg_source(32, 24)
    data = bytearray(source)
    sof = source.index(b"\xff\xc0")
    dqt = source.index(b"\xff\xdb")
    dht = source.index(b"\xff\xc4")
    sos = source.index(b"\xff\xda")
    if mutation == "pixel_bomb":
        data[sof + 5 : sof + 9] = struct.pack("!HH", 8192, 8192)
    elif mutation == "precision":
        data[sof + 4] = 12
    elif mutation == "cmyk":
        data[sof + 9] = 4
    elif mutation == "lossless":
        data[sof + 1] = 0xC3
    elif mutation == "duplicate_frame":
        data[sof:sof] = source[sof : sof + 2 + int.from_bytes(source[sof + 2 : sof + 4], "big")]
    elif mutation == "zero_quantization":
        data[dqt + 5] = 0
    elif mutation == "quantization_precision":
        length = int.from_bytes(source[dqt + 2 : dqt + 4], "big")
        body = source[dqt + 4 : dqt + 2 + length]
        expanded = bytearray()
        for offset in range(0, len(body), 65):
            expanded.append(body[offset] | 16)
            for value in body[offset + 1 : offset + 65]:
                expanded.extend((0, value))
        data[dqt : dqt + length + 2] = b"\xff\xdb" + struct.pack("!H", len(expanded) + 2) + expanded
    elif mutation == "bad_huffman":
        data[dht + 5] = 3
    elif mutation == "baseline_huffman_selector":
        data[dht + 4] = 2
        data[sos + 6] = (data[sos + 6] & 15) | 32
    elif mutation == "unknown_component":
        data[sos + 5] = 255
    elif mutation == "truncated_segment":
        data[dqt + 2 : dqt + 4] = b"\xff\xff"
    elif mutation == "tail":
        data.extend(b"tail")
    elif mutation == "multiple_images":
        data.extend(source)
    elif mutation == "restart_outside_scan":
        data[2:2] = b"\xff\xd0"
    with pytest.raises(JpegError):
        inspect_jpeg(bytes(data))


def test_marker_and_fill_floods_fail_with_small_allocation_and_finite_metadata_work():
    flood = b"\xff\xd8" + b"\xff\xfe\0\2" * 1025 + b"\xff\xd9"
    fills = b"\xff\xd8" + b"\xff" * (8 * 1024 * 1024 - 4) + b"\xff\xd9"
    tracemalloc.start()
    try:
        for payload in [flood, fills]:
            with pytest.raises(JpegError):
                inspect_jpeg(payload)
        assert tracemalloc.get_traced_memory()[1] < 256 * 1024
    finally:
        tracemalloc.stop()


@pytest.mark.asyncio
async def test_entropy_truncation_is_rejected_by_native_decode_not_misrepresented_as_header_proof():
    image = jpeg_source()
    truncated = image[:-15] + b"\xff\xd9"
    assert inspect_jpeg(truncated).width == 640
    worker = SnapshotWorker()
    baseline = codec._WORKERS
    try:
        with pytest.raises(SnapshotCodecError):
            await worker.convert(truncated, SnapshotConfiguration(640, 360))
        assert worker.pid is None and baseline == codec._WORKERS
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, GRAYSCALE])
async def test_valid_exact_size_baseline_is_preserved_byte_for_byte_after_native_proof(source):
    source = source if source is not None else jpeg_source(333, 187)
    metadata = inspect_jpeg(source)
    assert metadata.baseline
    worker = SnapshotWorker()
    try:
        output = await worker.convert(
            source, SnapshotConfiguration(metadata.width, metadata.height)
        )
        assert output == source
        assert rgb(output).shape == (metadata.height, metadata.width, 3)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_exact_size_progressive_still_transforms_to_baseline():
    metadata = inspect_jpeg(PROGRESSIVE)
    assert metadata.progressive and not metadata.baseline
    worker = SnapshotWorker()
    try:
        output = await worker.convert(
            PROGRESSIVE, SnapshotConfiguration(metadata.width, metadata.height)
        )
        assert output != PROGRESSIVE and inspect_jpeg(output).baseline
        assert rgb(output).shape == (metadata.height, metadata.width, 3)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_rejects_nonidentity_exif_with_typed_fixed_error():
    source = jpeg_source()
    source = source[:2] + exif(6) + source[2:]
    worker = SnapshotWorker()
    try:
        with pytest.raises(SnapshotCodecError) as error:
            await worker.convert(source, SnapshotConfiguration(320, 240))
        assert error.value.code == "unsupported_orientation"
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_native_maximum_output_and_process_thread_memory_budget_are_real():
    # Different geometry forces the maximum-output allocation/encoding path.
    image = jpeg_source(4000, 1998, noise=True)
    worker = SnapshotWorker()
    observed = []
    task = asyncio.create_task(worker.convert(image, SnapshotConfiguration(4000, 2000)))
    try:
        while not task.done():
            if worker.pid is not None:
                try:
                    text = Path(f"/proc/{worker.pid}/status").read_text()
                except FileNotFoundError:
                    pass
                else:
                    observed.append(
                        {
                            line.split(":")[0]: line.split(":")[1].strip()
                            for line in text.splitlines()
                            if line.startswith(("Threads:", "VmPeak:"))
                        }
                    )
            await asyncio.sleep(0.002)
        result = await task
        assert inspect_jpeg(result).width == 4000 and len(result) <= 8 * 1024 * 1024
        assert observed and all(int(item["Threads"]) == 1 for item in observed)
        assert all(int(item.get("VmPeak", "0 kB").split()[0]) <= 512 * 1024 for item in observed)
    finally:
        await worker.close()


HUNG = """import signal,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
sys.stdin.buffer.read(12)
while True: time.sleep(1)
"""


def replace_process(monkeypatch, tmp_path, script):
    path = tmp_path / "child.py"
    path.write_text(script)
    real = asyncio.create_subprocess_exec
    children = []

    async def spawn(*args, **kwargs):
        child = await real(args[0], args[1], str(path), **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(codec.asyncio, "create_subprocess_exec", spawn)
    return children


@pytest.mark.asyncio
async def test_hung_conversion_escalates_kill_reaps_and_releases_capacity(monkeypatch, tmp_path):
    children = replace_process(monkeypatch, tmp_path, HUNG)
    monkeypatch.setattr(codec, "CONVERT_SECONDS", 0.1)
    monkeypatch.setattr(codec, "STOP_SECONDS", 0.03)
    worker = SnapshotWorker()
    baseline = codec._WORKERS
    try:
        with pytest.raises(SnapshotCodecError) as error:
            await worker.convert(GRAYSCALE, SnapshotConfiguration(32, 24))
        assert error.value.code == "timeout"
        assert children[0].returncode == -signal.SIGKILL and baseline == codec._WORKERS
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_capacity_is_rejected_before_third_process_or_unbounded_wait(monkeypatch, tmp_path):
    children = replace_process(monkeypatch, tmp_path, HUNG)
    monkeypatch.setattr(codec, "STOP_SECONDS", 0.03)
    workers = [SnapshotWorker() for _ in range(3)]
    baseline = codec._WORKERS
    tasks = [
        asyncio.create_task(worker.convert(GRAYSCALE, SnapshotConfiguration(32, 24)))
        for worker in workers[:2]
    ]
    try:
        while len(children) < 2:
            await asyncio.sleep(0.001)
        with pytest.raises(SnapshotCodecError) as error:
            await workers[2].convert(GRAYSCALE, SnapshotConfiguration(32, 24))
        assert error.value.code == "busy" and len(children) == 2
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for worker in workers:
            await worker.close()
    assert baseline == codec._WORKERS


@pytest.mark.asyncio
async def test_close_owns_late_spawn_and_prevents_late_image_delivery(monkeypatch):
    real = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    release = asyncio.Event()
    children = []

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        child = await real(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(codec.asyncio, "create_subprocess_exec", spawn)
    worker = SnapshotWorker()
    baseline = codec._WORKERS
    task = asyncio.create_task(worker.convert(GRAYSCALE, SnapshotConfiguration(32, 24)))
    try:
        await entered.wait()
        closing = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        assert not closing.done() and baseline + 1 == codec._WORKERS
        release.set()
        await asyncio.wait_for(closing, 2)
        with pytest.raises(SnapshotCodecError):
            await task
        assert (
            children[0].returncode is not None and worker.pid is None and baseline == codec._WORKERS
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await worker.close()


@pytest.mark.asyncio
async def test_cancel_while_pipe_writer_is_stalled_joins_process_and_never_returns_image(
    monkeypatch, tmp_path
):
    children = replace_process(monkeypatch, tmp_path, HUNG)
    monkeypatch.setattr(codec, "STOP_SECONDS", 0.03)
    worker = SnapshotWorker()
    baseline = codec._WORKERS
    task = asyncio.create_task(
        worker.convert(b"x" * (8 * 1024 * 1024), SnapshotConfiguration(32, 24))
    )
    try:
        while not children:
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None and baseline == codec._WORKERS
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_worker_crash_and_oversized_output_length_fail_closed_before_output_allocation(
    monkeypatch, tmp_path
):
    for script in [
        "import os;os._exit(7)",
        "import sys,struct\n"
        "sys.stdout.buffer.write(struct.pack('!I',8*1024*1024+2))\n"
        "sys.stdout.buffer.flush()",
    ]:
        children = replace_process(monkeypatch, tmp_path, script)
        worker = SnapshotWorker()
        baseline = codec._WORKERS
        try:
            with pytest.raises(SnapshotCodecError):
                await worker.convert(GRAYSCALE, SnapshotConfiguration(32, 24))
            assert children[0].returncode is not None and baseline == codec._WORKERS
        finally:
            await worker.close()
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_failed_native_retirement_keeps_global_slot_until_explicit_retry(
    monkeypatch, tmp_path
):
    children = replace_process(monkeypatch, tmp_path, HUNG)
    monkeypatch.setattr(codec, "MAX_WORKERS", 1)
    monkeypatch.setattr(codec, "STOP_SECONDS", 0.01)
    worker = SnapshotWorker()
    blocked = SnapshotWorker()
    baseline = codec._WORKERS
    task = asyncio.create_task(worker.convert(GRAYSCALE, SnapshotConfiguration(32, 24)))
    try:
        while not children:
            await asyncio.sleep(0.001)
        process = children[0]
        real_wait = process.wait
        terminate = process.terminate
        kill = process.kill

        async def hang():
            await asyncio.Event().wait()

        monkeypatch.setattr(process, "wait", hang)
        monkeypatch.setattr(process, "terminate", lambda: None)
        monkeypatch.setattr(process, "kill", lambda: None)
        with pytest.raises(SnapshotCodecError) as error:
            await worker.close()
        assert error.value.code == "cleanup_pending" and worker in codec._RETIRING_WORKERS
        assert baseline + 1 == codec._WORKERS
        with pytest.raises(SnapshotCodecError) as error:
            await blocked.convert(GRAYSCALE, SnapshotConfiguration(32, 24))
        assert error.value.code == "busy"
        monkeypatch.setattr(process, "wait", real_wait)
        monkeypatch.setattr(process, "terminate", terminate)
        monkeypatch.setattr(process, "kill", kill)
        await codec.retire_snapshot_workers()
        await asyncio.gather(task, return_exceptions=True)
        assert baseline == codec._WORKERS and worker not in codec._RETIRING_WORKERS
    finally:
        if children:
            monkeypatch.setattr(children[0], "wait", real_wait)
            monkeypatch.setattr(children[0], "terminate", terminate)
            monkeypatch.setattr(children[0], "kill", kill)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await worker.close()
        await blocked.close()


@pytest.mark.asyncio
async def test_snapshot_preflight_and_malformed_input_leave_no_native_reservations():
    baseline = codec._WORKERS
    result = await codec.snapshot_capabilities()
    assert result.ready and result.reason is None and baseline == codec._WORKERS
    worker = SnapshotWorker()
    try:
        with pytest.raises(SnapshotCodecError):
            await worker.convert(b"x" * (8 * 1024 * 1024 + 1), SnapshotConfiguration(32, 24))
        assert worker.pid is None and baseline == codec._WORKERS
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_cancelled_unresolved_spawn_remains_owned_until_late_process_is_reaped(monkeypatch):
    real = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    release = asyncio.Event()
    children = []

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        child = await real(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(codec.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codec, "STOP_SECONDS", 0.01)
    monkeypatch.setattr(codec, "MAX_WORKERS", 1)
    worker = SnapshotWorker()
    blocked = SnapshotWorker()
    baseline = codec._WORKERS
    task = asyncio.create_task(worker.convert(GRAYSCALE, SnapshotConfiguration(32, 24)))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(SnapshotCodecError) as error:
            await task
        assert error.value.code == "cleanup_pending"
        assert worker in codec._RETIRING_WORKERS and baseline + 1 == codec._WORKERS
        with pytest.raises(SnapshotCodecError) as error:
            await blocked.convert(GRAYSCALE, SnapshotConfiguration(32, 24))
        assert error.value.code == "busy" and not children
        release.set()
        # The explicit retry owns the late child; there is no original caller left.
        monkeypatch.setattr(codec, "STOP_SECONDS", 1)
        await codec.retire_snapshot_workers()
        assert children[0].returncode is not None and baseline == codec._WORKERS
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await worker.close()
        await blocked.close()


@pytest.mark.asyncio
async def test_one_image_worker_cannot_be_reused_after_successful_conversion():
    worker = SnapshotWorker()
    baseline = codec._WORKERS
    try:
        await worker.convert(GRAYSCALE, SnapshotConfiguration(32, 24))
        with pytest.raises(SnapshotCodecError) as error:
            await worker.convert(GRAYSCALE, SnapshotConfiguration(32, 24))
        assert error.value.code == "busy" and baseline == codec._WORKERS
    finally:
        await worker.close()
