"""Current pinned transport coverage, never saved network identities or media."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aioice.ice import Candidate, CandidatePair, StunProtocol
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import SessionDescription

from halo_server.media.receiver_metrics import ReceiverPipeline
from halo_server.media.session import _safe_pipeline, _safe_selected_coverage


class Transport(asyncio.DatagramTransport):
    def sendto(self, *_args):
        pass


def local_protocol(connection):
    protocol = StunProtocol(connection)
    protocol.local_candidate = Candidate("fixed", 1, "udp", 1, "127.0.0.1", 9, "host")
    protocol.transport = Transport()
    return protocol


async def fake_owner():
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    audio = pc.addTransceiver("audio").receiver
    video = pc.addTransceiver("video").receiver
    video.setTransport(audio.transport)
    connection = audio.transport.transport._connection
    protocol = local_protocol(connection)
    connection._protocols.append(protocol)
    connection._nominated[1] = CandidatePair(protocol, protocol.local_candidate)
    pipeline = ReceiverPipeline(audio, video)
    pipeline.install_lower_boundaries()
    return pc, audio, video, connection, protocol, pipeline


async def close_fake(pc, pipeline):
    pipeline.fence()
    for transceiver in pc.getTransceivers():
        transceiver.receiver.transport.transport._connection._protocols.clear()
    await pc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle", [True, False])
async def test_actual_peer_answer_current_coverage_after_bundle_or_two_transports(bundle):
    offerer, answerer = (RTCPeerConnection(RTCConfiguration(iceServers=[])) for _ in range(2))
    video = offerer.addTransceiver("video", direction="recvonly").receiver
    audio = offerer.addTransceiver("audio", direction="sendrecv").receiver
    pipeline = ReceiverPipeline(audio, video)
    try:
        offer = await offerer.createOffer()
        if not bundle:
            description = SessionDescription.parse(offer.sdp)
            description.group = [item for item in description.group if item.semantic != "BUNDLE"]
            offer = RTCSessionDescription(str(description), "offer")
        await offerer.setLocalDescription(offer)
        pipeline.install_lower_boundaries()
        before = pipeline.snapshot()["lower_boundary"]
        assert before["connections"] == 2
        assert before["selected_transport_coverage"]["state"] == "unobserved"
        await answerer.setRemoteDescription(offerer.localDescription)
        answer = await answerer.createAnswer()
        if not bundle:
            description = SessionDescription.parse(answer.sdp)
            description.group = [item for item in description.group if item.semantic != "BUNDLE"]
            answer = RTCSessionDescription(str(description), "answer")
        await answerer.setLocalDescription(answer)
        await offerer.setRemoteDescription(answerer.localDescription)
        async with asyncio.timeout(5):
            while offerer.connectionState != "connected" or answerer.connectionState != "connected":
                await asyncio.sleep(0.01)
        sampled = pipeline.snapshot()
        coverage = sampled["lower_boundary"]["selected_transport_coverage"]
        assert coverage == {
            "state": "covered",
            "current_transports": 1 if bundle else 2,
            "current_protocols": before["protocols"] // 2 if bundle else before["protocols"],
            "nominated_pairs": 1 if bundle else 2,
            "transports_covered": True,
            "protocols_covered": True,
            "nominated_covered": True,
            "wrappers_current": True,
        }
        assert _safe_pipeline(sampled)["lower_boundary"]["selected_transport_coverage"] == coverage
        assert "127.0.0.1" not in json.dumps(coverage)
    finally:
        pipeline.fence()
        await asyncio.gather(offerer.close(), answerer.close())


@pytest.mark.asyncio
async def test_overwritten_live_hook_is_uncovered_and_is_not_restored_over_new_owner():
    pc, audio, _video, _connection, _protocol, pipeline = await fake_owner()
    try:
        assert (
            pipeline.snapshot()["lower_boundary"]["selected_transport_coverage"]["state"]
            == "covered"
        )
        original = audio.transport._handle_rtp_data

        async def replacement(*args, **kwargs):
            return await original(*args, **kwargs)

        audio.transport._handle_rtp_data = replacement
        sampled = pipeline.snapshot()["lower_boundary"]["selected_transport_coverage"]
        assert sampled["state"] == "uncovered" and sampled["wrappers_current"] is False
        pipeline.fence()
        assert audio.transport._handle_rtp_data is replacement
    finally:
        await close_fake(pc, pipeline)


@pytest.mark.asyncio
async def test_new_protocol_or_replaced_transport_cannot_inherit_installed_coverage():
    pc, audio, video, connection, _protocol, pipeline = await fake_owner()
    replacement = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        extra = local_protocol(connection)
        connection._protocols.append(extra)
        coverage = pipeline.snapshot()["lower_boundary"]["selected_transport_coverage"]
        assert coverage["state"] == "uncovered" and coverage["protocols_covered"] is False
        connection._protocols.remove(extra)
        other = replacement.addTransceiver("video").receiver.transport
        audio.setTransport(other)
        video.setTransport(other)
        assert (
            pipeline.snapshot()["lower_boundary"]["selected_transport_coverage"]["state"]
            == "unavailable"
        )
    finally:
        connection._protocols.clear()
        await close_fake(pc, pipeline)
        await replacement.close()


@pytest.mark.asyncio
async def test_nominated_unknown_or_missing_protocol_remains_unknown_or_false():
    pc, _audio, _video, connection, protocol, pipeline = await fake_owner()
    try:
        connection._nominated.clear()
        value = pipeline.snapshot()["lower_boundary"]["selected_transport_coverage"]
        assert value["state"] == "unobserved" and value["nominated_covered"] is None
        foreign = local_protocol(connection)
        connection._nominated[1] = CandidatePair(foreign, protocol.local_candidate)
        value = pipeline.snapshot()["lower_boundary"]["selected_transport_coverage"]
        assert value["state"] == "uncovered" and value["nominated_covered"] is False
        connection._nominated[1] = SimpleNamespace(protocol=protocol)
        assert (
            pipeline.snapshot()["lower_boundary"]["selected_transport_coverage"]["state"]
            == "unavailable"
        )
    finally:
        await close_fake(pc, pipeline)


@pytest.mark.parametrize(
    "bad", [{"state": []}, {"state": "covered"}, {"state": "covered", "current_transports": True}]
)
def test_status_never_promotes_missing_or_malformed_coverage(bad):
    assert _safe_selected_coverage(bad) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, "new_udp", "unsupported", "foreign_receiver"])
async def test_protocol_endpoint_replacement_is_not_once_installed_proof(replacement):
    pc, _audio, _video, connection, protocol, pipeline = await fake_owner()
    original_transport, original_receiver = protocol.transport, protocol.receiver
    try:
        assert (
            pipeline.snapshot()["lower_boundary"]["selected_transport_coverage"]["state"]
            == "covered"
        )
        if replacement == "foreign_receiver":
            protocol.receiver = object()
        else:
            protocol.transport = (
                Transport() if replacement == "new_udp" else object() if replacement else None
            )
        value = pipeline.snapshot()["lower_boundary"]["selected_transport_coverage"]
        assert value["state"] == "uncovered" and value["protocols_covered"] is False
    finally:
        protocol.transport, protocol.receiver = original_transport, original_receiver
        await close_fake(pc, pipeline)
