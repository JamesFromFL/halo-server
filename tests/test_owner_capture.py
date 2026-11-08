"""An explicit diagnostic owner never wakes or borrows authority from a camera."""

import asyncio
import json
from uuid import uuid4

import pytest
from test_native_service import Camera, Capture, service_fixture

from halo_server.api.admin import AdminProtocolError
from halo_server.cli import build_parser
from halo_server.media.leases import LeasePurpose


def request(**changes):
    return {
        "capture_id": str(uuid4()),
        "camera_id": "42",
        "seconds": 30,
        "record_media": True,
    } | changes


@pytest.mark.asyncio
async def test_explicit_capture_is_idle_idempotent_scoped_and_privately_stopped(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    service.paths.prepare()
    args = request()
    try:
        first = await service.handle_admin("stream_capture_start", args)
        assert first["accepting"] and first["capture_id"] == args["capture_id"]
        again = await service.handle_admin("stream_capture_start", args)
        assert again["directory"] == first["directory"]
        assert client.submissions == 0 and not service._media_sessions
        owner = service._owner_admin.stream_capture
        assert owner.trace_for("unknown") is None
        trace = owner.trace_for("42")
        assert trace is not None
        assert trace.event("lifecycle", "session", reason="start")
        assert trace.media("source_frame", "audio", b"\xf8\xff\xfe", rtp_timestamp=0)
        await service.handle_admin("stream_capture_mark", {"capture_id": args["capture_id"]})
        with pytest.raises(AdminProtocolError, match="current owner"):
            await service.handle_admin("stream_capture_stop", {"capture_id": str(uuid4())})
        stopped = await service.handle_admin(
            "stream_capture_stop", {"capture_id": args["capture_id"]}
        )
        assert stopped["finished"] and not stopped["accepting"]
        assert not trace.media("source_frame", "audio", b"later", rtp_timestamp=960)
        assert client.submissions == 0
        from pathlib import Path

        path = Path(stopped["directory"])
        assert path.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in path.iterdir())
        owner_metadata = json.loads((path / "owner.json").read_text())
        assert owner_metadata["record_media"] is True
        assert "camera_id" not in owner_metadata and "account-display-only" not in repr(
            owner_metadata
        )
    finally:
        await service._owner_admin.close()


@pytest.mark.asyncio
async def test_capture_revokes_immediately_on_device_replacement(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    service.paths.prepare()
    try:
        await service.handle_admin("stream_capture_start", request())
        owner = service._owner_admin.stream_capture
        trace = owner.trace_for("42")
        client.all_devices[:] = [Camera()]
        assert not trace.media("source_frame", "audio", b"wrong owner", rtp_timestamp=0)
        assert owner.trace_for("42") is None
        await owner.reconcile()
        assert owner.capture.closed
        await owner.close()
        assert owner.status()["finished"]
    finally:
        await service._owner_admin.close()


@pytest.mark.asyncio
async def test_trace_lookup_failure_cannot_fail_camera_start(tmp_path, monkeypatch):
    service, _, _ = service_fixture(tmp_path)
    service.paths.prepare()
    await service.handle_admin("stream_capture_start", request())
    owner = service._owner_admin.stream_capture
    try:
        with monkeypatch.context() as patch:

            def failed_status():
                raise OSError("diagnostic status unavailable")

            patch.setattr(owner.capture, "status", failed_status)
            assert owner.trace_for("42") is None
            assert owner.trace.revoked.is_set()
            assert owner.capture.closed
    finally:
        await service._owner_admin.close()


@pytest.mark.asyncio
async def test_revocation_housekeeping_fences_without_join(tmp_path, monkeypatch):
    service, client, _ = service_fixture(tmp_path)
    service.paths.prepare()
    await service.handle_admin("stream_capture_start", request())
    owner = service._owner_admin.stream_capture
    try:
        with monkeypatch.context() as patch:

            def forbidden_join():
                pytest.fail("Routine housekeeping must not join a diagnostic writer")

            patch.setattr(owner.capture, "close", forbidden_join)
            client.all_devices[:] = [Camera()]
            await owner.reconcile()
            assert owner.trace.revoked.is_set() and owner.capture.closed
    finally:
        await service._owner_admin.close()


@pytest.mark.asyncio
async def test_capture_rejects_busy_camera_and_attaches_only_after_retirement(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    service.paths.prepare()

    class Session(Capture):
        trace = None

        def set_diagnostic_trace(self, trace):
            self.trace = trace
            return not self.leases

    source = Session(client, client.all_devices[0])
    service._media_sessions["42"] = source
    source.acquire(LeasePurpose.HOMEKIT_LIVE, lease_id="test")
    try:
        with pytest.raises(AdminProtocolError) as error:
            await service.handle_admin("stream_capture_start", request())
        assert error.value.code == "busy"
        source.release("test")
        await service.handle_admin("stream_capture_start", request())
        assert source.trace is service._owner_admin.stream_capture.trace
    finally:
        await service._owner_admin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"record_media": False},
        {"record_media": 1},
        {"seconds": True},
        {"seconds": 301},
        {"seconds": 0},
        {"capture_id": "../other"},
        {"output": "/tmp/unowned"},
    ],
)
async def test_invalid_capture_requests_do_not_create_files(tmp_path, changes):
    service, client, _ = service_fixture(tmp_path)
    with pytest.raises(AdminProtocolError) as error:
        await service.handle_admin("stream_capture_start", request(**changes))
    assert error.value.code == "bad_params"
    assert (
        client.submissions == 0
        and not (service.paths.state_directory / "diagnostic-captures").exists()
    )


def test_capture_cli_is_explicit_and_bounded_by_owner_validation():
    parser = build_parser()
    args = parser.parse_args(["stream-capture", "start", "42", "--seconds", "180"])
    assert args.capture_command == "start" and args.seconds == 180
    assert parser.parse_args(["stream-capture", "status"]).capture_command == "status"
    token = str(uuid4())
    assert parser.parse_args(["stream-capture", "stop", token]).capture_id == token


@pytest.mark.asyncio
async def test_stop_fences_producers_before_join_worker_runs(tmp_path, monkeypatch):
    from halo_server import owner_capture

    service, _, _ = service_fixture(tmp_path)
    service.paths.prepare()
    args = request()
    await service.handle_admin("stream_capture_start", args)
    owner = service._owner_admin.stream_capture
    worker_entered, release_worker = asyncio.Event(), asyncio.Event()
    original = owner_capture.owned_thread

    async def delayed_worker(function, *arguments):
        worker_entered.set()
        await release_worker.wait()
        return await original(function, *arguments)

    monkeypatch.setattr(owner_capture, "owned_thread", delayed_worker)
    stop = asyncio.create_task(
        service.handle_admin("stream_capture_stop", {"capture_id": args["capture_id"]})
    )
    try:
        await asyncio.wait_for(worker_entered.wait(), 1)
        assert owner.trace.closed
        assert owner.capture.closed
        assert not owner.capture.event("lifecycle", "session", reason="start")
    finally:
        release_worker.set()
        await stop
        await service._owner_admin.close()
