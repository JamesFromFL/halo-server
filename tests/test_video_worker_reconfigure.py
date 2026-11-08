"""Native decoder continuity and bounded worker protocol during output changes."""

import asyncio
import json
import signal
from dataclasses import asdict, replace
from fractions import Fraction

import av
import pytest
from test_media_h264 import encode_source

from halo_server.media import transcode, transcode_worker
from halo_server.media.h264 import H264Error, VideoConfiguration, parse_sps, split_annexb
from halo_server.media.transcode import VideoTransformError, VideoWorker
from halo_server.media.transcode_worker import Encoder, decode_configuration

CONFIG = VideoConfiguration(640, 360, 30, "high", "3.1", 600)
SMALL = replace(CONFIG, width=320, height=180, max_bit_rate=300)


@pytest.fixture(scope="module")
def source():
    return encode_source(width=640, height=360, fps=24, profile="high", frames=96)


def types(payload):
    return {nal[0] & 31 for nal in split_annexb(payload)}


def assert_idr(payload, configuration):
    assert {5, 7, 8} <= types(payload)
    info = parse_sps(next(nal for nal in split_annexb(payload) if nal[0] & 31 == 7))
    assert (info.width, info.height, info.profile) == (
        configuration.width,
        configuration.height,
        configuration.profile,
    )
    independent = av.CodecContext.create("h264", "r")
    independent.thread_count = 1
    frames = independent.decode(av.Packet(payload))
    assert len(frames) == 1 and not frames[0].is_corrupt
    return info


@pytest.mark.asyncio
async def test_real_worker_reconfigures_on_source_pictures_without_gaps_or_process_restart(source):
    worker = VideoWorker(CONFIG)
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    selected, outputs, pid = CONFIG, [], None
    initial = transcode._ENCODERS
    changes = {5: SMALL, 37: CONFIG, 75: SMALL}
    try:
        for index, (data, pts) in enumerate(source):
            if index in changes:
                assert 5 not in types(data)
                selected = changes[index]
                await worker.reconfigure(selected)
                assert worker.pid == pid and initial + 1 == transcode._ENCODERS
                assert worker.configuration == selected
            result = await worker.feed(data, pts)
            pid = pid or worker.pid
            assert result and len(result) == 1 and result[0][0] == pts
            if index in changes:
                assert_idr(result[0][1], selected)
            packet = av.Packet(result[0][1])
            packet.pts, packet.time_base = pts, Fraction(1, 90000)
            frames = decoder.decode(packet)
            assert len(frames) == 1 and not frames[0].is_corrupt
            assert (frames[0].pts, frames[0].width, frames[0].height) == (
                pts,
                selected.width,
                selected.height,
            )
            assert worker.last_pending_frames == 0
            outputs.append(pts)
        assert outputs == [pts for _data, pts in source]
        assert worker.configuration_revision == len(changes)
    finally:
        await worker.close()
    assert worker.pid is None and initial == transcode._ENCODERS


@pytest.mark.asyncio
async def test_fps_changes_select_real_frames_and_restart_nominal_at_the_new_ceiling(source):
    worker = VideoWorker(CONFIG)
    output_indexes, output_pts, raised_rates = [], [], []
    try:
        for index, (data, pts) in enumerate(source):
            if index in {17, 60}:
                await worker.reconfigure(replace(CONFIG, fps=12) if index == 17 else CONFIG)
            result = await worker.feed(data, pts)
            for stamp, payload in result:
                assert stamp == pts
                output_indexes.append(index)
                output_pts.append(stamp)
                if index in {18, 60}:
                    info = assert_idr(payload, worker.configuration)
                    assert info.frame_rate == (12 if index == 18 else 30)
                if index >= 60 and 7 in types(payload):
                    raised_rates.append(assert_idr(payload, CONFIG).frame_rate)
        assert set(range(len(source))) - set(output_indexes) == set(range(17, 60, 2))
        assert max(b - a for a, b in zip(output_pts, output_pts[1:], strict=False)) == 7500
        assert raised_rates[0] == 30 and raised_rates[-1] == 24
    finally:
        await worker.close()


def test_decoder_held_origins_and_pending_force_fifo_survive_native_reconfiguration():
    source = encode_source(fps=24, profile="high", frames=48, bframes=2)
    worker, control = Encoder(asdict(CONFIG)), Encoder(asdict(CONFIG))
    decoder, old_forces = worker.decoder, worker.forces
    outputs, controls, acknowledged = [], [], []
    changes = {1: SMALL, 17: CONFIG, 37: SMALL}
    requests = {0: 1, 1: 2, 17: 3, 18: 4, 37: 5}
    revision = 0
    for index, (data, pts) in enumerate(source):
        if index in changes:
            inputs, forces, serial = dict(worker.inputs), tuple(worker.forces), worker.last_force
            assert inputs  # A live decoder may own reordered source pictures.
            if index == 1:
                assert forces == (1,)
            revision += 1
            worker.reconfigure(asdict(changes[index]), revision)
            assert dict(worker.inputs) == inputs
            assert tuple(worker.forces) == forces and worker.forces is old_forces
            assert worker.decoder is decoder and worker.last_force == serial
        result = worker.feed(data, pts, requests.get(index, 0))
        controls.extend(control.feed(data, pts, 0))
        outputs.extend(result)
        for _stamp, token, payload in result:
            if token:
                acknowledged.append(token)
                assert_idr(payload, VideoConfiguration(**worker.config))
    assert [row[0] for row in outputs] == [row[0] for row in controls]
    assert acknowledged == list(requests.values())
    assert not worker.forces and not worker.pending and len(worker.inputs) == 2


def test_encoder_prepare_failure_preserves_every_previous_owner(monkeypatch, source):
    worker = Encoder(asdict(CONFIG))
    for data, pts in source[:3]:
        worker.feed(data, pts, 0)
    before = dict(vars(worker))

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic encoder initialization failure")

    with monkeypatch.context() as context:
        context.setattr(transcode_worker, "open_encoder", fail)
        with pytest.raises(RuntimeError, match="synthetic"):
            worker.reconfigure(asdict(SMALL), 1)
    assert vars(worker) == before
    data, pts = source[3]
    assert worker.feed(data, pts, 0)[0][0] == pts
    worker.reconfigure(asdict(SMALL), 1)
    assert worker.decoder is before["decoder"] and worker.encoder is not before["encoder"]


def test_unresolved_encoder_packets_are_not_confused_with_decoder_origins(source):
    worker = Encoder(asdict(CONFIG))
    data, pts = source[0]
    worker.feed(data, pts, 0)
    frame = av.VideoFrame(CONFIG.width, CONFIG.height, "yuv420p")
    for plane in frame.planes:
        plane.update(bytes([80]) * plane.buffer_size)
    pending = worker.encode(frame, 3750)
    assert worker.pending
    encoder = worker.encoder
    with pytest.raises(ValueError):
        worker.reconfigure(asdict(SMALL), 1)
    assert worker.encoder is encoder and worker.configuration_revision == 0
    assert worker.resolve(pending)[0][0] == 3750
    worker.reconfigure(asdict(SMALL), 1)
    assert worker.configuration_revision == 1


@pytest.mark.parametrize("revision", [0, -1, 2, True, 1.0, 2**53])
def test_child_requires_exact_next_integer_revision(revision):
    worker = Encoder(asdict(CONFIG))
    before = dict(vars(worker))
    with pytest.raises(ValueError):
        worker.reconfigure(asdict(SMALL), revision)
    assert vars(worker) == before


def test_transport_only_commands_do_not_replace_encoder_or_source_cadence(source):
    worker = Encoder(asdict(CONFIG))
    for data, pts in source[:15]:
        worker.feed(data, pts, 0)
    before = dict(vars(worker))
    changed = replace(CONFIG, mtu=512, rtcp_interval=1)
    worker.reconfigure(asdict(changed), 1)
    assert worker.config == asdict(changed) and worker.configuration_revision == 1
    for key, value in before.items():
        if key not in {"config", "configuration_revision"}:
            assert vars(worker)[key] is value


@pytest.mark.parametrize(
    "change",
    [
        {"width": True},
        {"width": 641},
        {"width": 1922},
        {"height": 1082},
        {"fps": 0},
        {"fps": 31},
        {"fps": "30"},
        {"profile": ["high"]},
        {"profile": "extended"},
        {"level": "5.0"},
        {"max_bit_rate": 0},
        {"max_bit_rate": 17501},
        {"payload_type": 95},
        {"mtu": 1379},
        {"rtcp_interval": True},
        {"rtcp_interval": float("nan")},
        {"rtcp_interval": float("inf")},
        {"rtcp_interval": 11},
        {"width": 1920, "height": 1080},
    ],
)
def test_child_and_parent_reject_the_same_outside_contract_values(change):
    config = asdict(CONFIG) | change
    with pytest.raises((H264Error, TypeError)):
        VideoConfiguration(**config)
    with pytest.raises(ValueError):
        decode_configuration(json.dumps(config).encode())


@pytest.mark.parametrize("payload", [b"[]", b"{}", b"null", b"{" * 8193, b'{"width":1,"width":2}'])
def test_configuration_json_rejects_ambiguous_or_unbounded_input(payload):
    with pytest.raises(ValueError):
        decode_configuration(payload)


def test_configuration_rejects_unknown_fields_without_mutating_valid_caller_dictionary():
    config = asdict(CONFIG)
    worker = Encoder(config)
    config["width"] = 320
    assert worker.config["width"] == CONFIG.width
    with pytest.raises(ValueError):
        worker.reconfigure(asdict(CONFIG) | {"x264-params": "unsafe"}, 1)


@pytest.mark.asyncio
async def test_native_protocol_rejects_replayed_revision_and_releases_capacity():
    worker = VideoWorker(CONFIG)
    initial = transcode._ENCODERS
    try:
        await worker.reconfigure(SMALL)
        assert worker.configuration_revision == 1
        body = json.dumps(asdict(CONFIG)).encode()
        process = worker._process
        process.stdin.write(transcode_worker.HEADER.pack(b"C", 1, 0, len(body)) + body)
        await process.stdin.drain()
        assert await worker._read() == b"Eworker-failed"
        assert await asyncio.wait_for(process.wait(), 2) == 2
    finally:
        await worker.close()
    assert worker.pid is None and initial == transcode._ENCODERS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,revision,force,body,length",
    [
        (b"C", 1, 1, b"{}", None),
        (b"C", 0, 0, b"{}", None),
        (b"C", 1, 0, b"", 8193),
        (b"C", 1, 0, b"", 0),
        (b"C", 1, 0, b'{"width":1,"width":2}', None),
        (b"X", 1, 0, b"{}", None),
    ],
)
async def test_native_protocol_rejects_invalid_command_before_any_frame(
    kind, revision, force, body, length
):
    worker = VideoWorker(CONFIG)
    try:
        await worker.start()
        process = worker._process
        header = transcode_worker.HEADER.pack(
            kind, revision, force, len(body) if length is None else length
        )
        process.stdin.write(header + body)
        await process.stdin.drain()
        assert await asyncio.wait_for(worker._read(), 2) == b"Eworker-failed"
        assert await asyncio.wait_for(process.wait(), 2) == 2
    finally:
        await worker.close()


FAKE_WORKER = """
import signal,struct,sys,time
def read(n):
 result=b''
 while len(result)<n:
  part=sys.stdin.buffer.read(n-len(result))
  if not part: raise EOFError
  result+=part
 return result
def write(value):
 sys.stdout.buffer.write(struct.pack('!I',len(value))+value)
 sys.stdout.buffer.flush()
read(struct.unpack('!I',read(4))[0]);write(b'S')
kind,revision,force,count=struct.unpack('!cQQI',read(21));read(count)
ACTION
time.sleep(100)
"""


def substitute_child(tmp_path, monkeypatch, action):
    script = tmp_path / "configuration_child.py"
    script.write_text(FAKE_WORKER.replace("ACTION", action))
    spawn = asyncio.create_subprocess_exec
    made = []

    async def substitute(*args, **kwargs):
        process = await spawn(args[0], args[1], str(script), **kwargs)
        made.append(process)
        return process

    monkeypatch.setattr(transcode.asyncio, "create_subprocess_exec", substitute)
    return made


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "write(b'C'+struct.pack('!Q',revision+1))",
        "write(b'V'+struct.pack('!Q',revision))",
        "write(b'C')",
        "sys.stdout.buffer.write(struct.pack('!I',2**31));sys.stdout.buffer.flush()",
    ],
)
async def test_invalid_acknowledgement_retires_unknown_native_state(tmp_path, monkeypatch, action):
    made = substitute_child(tmp_path, monkeypatch, action)
    worker = VideoWorker(CONFIG)
    initial = transcode._ENCODERS
    try:
        with pytest.raises(VideoTransformError):
            await worker.reconfigure(SMALL)
        assert worker.configuration == CONFIG and worker.configuration_revision == 0
        assert made[0].returncode is not None
        assert worker.pid is None and initial == transcode._ENCODERS
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_hung_reconfiguration_is_killed_reaped_and_never_committed(tmp_path, monkeypatch):
    made = substitute_child(tmp_path, monkeypatch, "signal.signal(signal.SIGTERM,lambda *_:None)")
    monkeypatch.setattr(transcode, "FRAME_SECONDS", 0.05)
    monkeypatch.setattr(transcode, "STOP_SECONDS", 0.03)
    worker, initial = VideoWorker(CONFIG), transcode._ENCODERS
    try:
        with pytest.raises(TimeoutError):
            await worker.reconfigure(SMALL)
        assert made[0].returncode == -signal.SIGKILL
        assert worker.configuration == CONFIG and worker.configuration_revision == 0
        assert worker.pid is None and initial == transcode._ENCODERS
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["cancel", "close", "none"])
async def test_command_ownership_blocks_feed_probe_and_reconfigure_during_acknowledgement(
    monkeypatch, retirement
):
    worker, initial = VideoWorker(CONFIG), transcode._ENCODERS
    await worker.start()
    entered, release = asyncio.Event(), asyncio.Event()
    read = worker._read

    async def delayed_ack(*args, **kwargs):
        body = await read(*args, **kwargs)
        entered.set()
        await release.wait()
        return body

    monkeypatch.setattr(worker, "_read", delayed_ack)
    pending = asyncio.create_task(worker.reconfigure(SMALL))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(VideoTransformError):
            await worker.feed(b"source", 0)
        with pytest.raises(VideoTransformError):
            await worker.probe()
        with pytest.raises(VideoTransformError):
            await worker.reconfigure(CONFIG)
        if retirement == "cancel":
            pending.cancel()
        elif retirement == "close":
            await worker.close()
        release.set()
        if retirement == "none":
            await pending
            assert worker.configuration == SMALL and worker.configuration_revision == 1
            assert initial + 1 == transcode._ENCODERS
        else:
            expected = asyncio.CancelledError if retirement == "cancel" else VideoTransformError
            with pytest.raises(expected):
                await pending
            assert worker.configuration == CONFIG and worker.configuration_revision == 0
            assert worker.pid is None and initial == transcode._ENCODERS
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await worker.close()


@pytest.mark.asyncio
async def test_terminal_probe_and_retired_worker_reject_configuration_commands():
    worker = VideoWorker(CONFIG)
    try:
        await worker.probe()
        with pytest.raises(VideoTransformError):
            await worker.reconfigure(SMALL)
        assert worker.configuration_revision == 0
    finally:
        await worker.close()
    with pytest.raises(VideoTransformError):
        await worker.reconfigure(SMALL)


@pytest.mark.asyncio
async def test_bad_parent_configuration_does_not_allocate_native_capacity():
    worker, initial = VideoWorker(CONFIG), transcode._ENCODERS
    broken = replace(CONFIG)
    object.__setattr__(broken, "fps", True)
    try:
        for value in (None, asdict(CONFIG), broken):
            with pytest.raises(H264Error):
                await worker.reconfigure(value)
        assert worker.pid is None and initial == transcode._ENCODERS
    finally:
        await worker.close()
