"""Regular compound RTCP identity, wire shape and independent SRTP boundaries."""

import struct

import pytest
from aiortc.rtp import RtcpPacket, RtcpSdesPacket, RtcpSrPacket

from halo_server.media.live_timeline import LiveTimeline
from halo_server.media.rtp import RtpError, sender_report
from halo_server.media.srtp import SecureVideo


def timeline():
    return LiveTimeline(audio_rate=24000, video_origin=1234, audio_origin=5678)


def test_regular_reports_keep_one_anonymous_identity_across_distinct_av_sources():
    owner = timeline()
    assert len(owner.cname) == 32 and owner.cname.isalnum()
    assert owner.cname != timeline().cname
    assert owner.cname.decode() not in repr(owner.snapshot())
    for ssrc in (11, 22):
        report = sender_report(ssrc, 90000, 10, 1000, now=100.5, cname=owner.cname)
        assert len(report) % 4 == 0 and len(report) < 128
        # Inspect compound boundaries separately from the aiortc parser.
        assert struct.unpack_from("!BBH", report) == (0x80, 200, 6)
        assert struct.unpack_from("!BB", report, 28) == (0x81, 202)
        assert struct.unpack_from("!I", report, 32)[0] == ssrc
        assert report[36:38] == bytes((1, 32))
        assert report[38:70] == owner.cname
        sr, sdes = RtcpPacket.parse(report)
        assert isinstance(sr, RtcpSrPacket) and isinstance(sdes, RtcpSdesPacket)
        assert sr.ssrc == sdes.chunks[0].ssrc == ssrc
        assert sdes.chunks[0].items == [(1, owner.cname)]
        assert sr.sender_info.packet_count == 10
        assert sr.sender_info.octet_count == 1000


def test_compound_report_survives_srtcp_authentication_as_one_datagram():
    sender = SecureVideo(bytes(range(16)), bytes(range(14)), 55)
    receiver = SecureVideo(bytes(range(16)), bytes(range(14)), 55)
    try:
        report = sender_report(55, 90000, 10, 1000, now=100.5, cname=timeline().cname)
        protected = sender.protect_rtcp(report)
        assert receiver.unprotect_rtcp(protected) == report
        assert len(RtcpPacket.parse(report)) == 2
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("cname", [b"", b"x" * 256, "not-bytes", bytearray(b"a"), 123])
def test_cname_is_bounded_before_serialization(cname):
    with pytest.raises(RtpError, match="canonical name"):
        sender_report(1, 0, 0, 0, now=1, cname=cname)


def test_sdes_padding_and_maximum_cname_round_trip():
    for size in (1, 2, 3, 4, 254, 255):
        name = b"x" * size
        report = sender_report(1, 0, 0, 0, now=1, cname=name)
        assert len(report) % 4 == 0
        assert RtcpPacket.parse(report)[1].chunks[0].items == [(1, name)]
