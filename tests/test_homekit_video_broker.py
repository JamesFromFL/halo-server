"""Private video control routes only to the selected Python output owner."""

import base64
import copy
import json
from dataclasses import asdict
from importlib.resources import files
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homekit_broker import permit, request, setup_broker

from halo_server.homekit.ipc import encode_frame, validate_reply
from halo_server.homekit.models import HomeKitOperationError


def sample(kind):
    corpus = json.loads(
        files("halo_server.homekit").joinpath("protocol/v1/fixtures.json").read_text()
    )
    return copy.deepcopy(next(item["payload"] for item in corpus["valid"] if item["kind"] == kind))


class VideoOwner:
    ready = True

    def __init__(self):
        self.calls = []
        self.failure = None

    async def prepare(self, boot, scope, session_id, **parameters):
        self.calls.append(("prepare", boot, scope, session_id, parameters))
        if self.failure:
            raise self.failure
        return SimpleNamespace(
            address=parameters["source_address"],
            port=5001,
            ssrc=42,
            key=parameters["key"],
            salt=parameters["salt"],
            audio=None,
        )

    async def start(self, *args, **kwargs):
        self.calls.append(("start", *args))

    async def reconfigure(self, *args):
        self.calls.append(("reconfigure", *args))

    async def stop(self, *args):
        self.calls.append(("stop", *args))


@pytest.mark.asyncio
async def test_video_private_contract_preserves_scope_keys_and_configuration(tmp_path):
    broker, boot, _, _, client, _ = await setup_broker(tmp_path)
    owner = broker.live_manager = VideoOwner()
    try:
        scope = await permit(broker, boot, streaming=True)
        identity = str(uuid4())
        for kind in ("video_prepare", "video_start", "video_reconfigure", "video_stop"):
            data = sample(kind) | {"scope": scope, "session_id": identity}
            envelope = request(boot, kind, data)
            result = await broker.request(envelope)
            response = {**envelope, "kind": kind + "_result", "payload": result}
            encode_frame(response)
            validate_reply(envelope, response)
            assert result["status"] == "accepted" and result["code"] == "ok"
            if kind == "video_prepare":
                assert result["endpoint"]["key"] == data["key"]
                assert owner.calls[-1][-1]["key"] == base64.b64decode(data["key"])
        assert [call[0] for call in owner.calls] == ["prepare", "start", "reconfigure", "stop"]
        assert asdict(owner.calls[1][-1]) == sample("video_start")["config"]
        assert client.submissions == 0  # The fake output owner never touches Ring.
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_video_rejection_has_no_endpoint_or_secret_error_text(tmp_path):
    broker, boot, _, _, _, _ = await setup_broker(tmp_path)
    owner = broker.live_manager = VideoOwner()
    try:
        scope = await permit(broker, boot, streaming=True)
        data = sample("video_prepare") | {"scope": scope}
        for failure, expected in (
            (HomeKitOperationError("policy_denied", "SECRET"), "policy-denied"),
            (ValueError("SECRET"), "invalid-request"),
            (OSError("SECRET"), "source-failed"),
        ):
            owner.failure = failure
            envelope = request(boot, "video_prepare", data)
            result = await broker.request(envelope)
            assert result["endpoint"] is None and result["code"] == expected
            assert "SECRET" not in json.dumps(result)
            encode_frame({**envelope, "kind": "video_prepare_result", "payload": result})
        owner.ready = False
        stopped = await broker.request(
            request(boot, "video_stop", {"scope": scope, "session_id": data["session_id"]})
        )
        assert stopped["status"] == "accepted"  # Cleanup remains available when output is degraded.
    finally:
        await broker.retired(boot)
