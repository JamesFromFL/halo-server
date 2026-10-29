"""HomeKit parameter aggregation, authenticated wire data and independent decode."""

import struct

import av
import pylibsrtp
import pytest
from aiortc.codecs.h264 import h264_depayload
from test_media_h264 import encode_source

from halo_server.media.h264 import annexb, split_annexb
from halo_server.media.rtp import (
    MAX_ACCESS_UNIT_BYTES,
    MAX_UDP_PAYLOAD_BYTES,
    H264Packetizer,
    RtpError,
)
from halo_server.media.srtp import SecureVideo


def members(payload):
    """Independent strict STAP-A syntax check, without production depacketizing."""
    assert payload[0] & 31 == 24 and not payload[0] & 0x80
    result, offset = [], 1
    while offset < len(payload):
        assert offset + 2 <= len(payload)
        count = int.from_bytes(payload[offset : offset + 2], "big")
        offset += 2
        assert count > 0 and offset + count <= len(payload)
        nal = payload[offset : offset + count]
        assert 1 <= nal[0] & 31 <= 23 and not nal[0] & 0x80
        result.append(nal)
        offset += count
    assert offset == len(payload) and result
    assert payload[0] & 0x60 == max(nal[0] & 0x60 for nal in result)
    return tuple(result)


def independent_reconstruction(packets):
    # aiortc's separately maintained RFC6184 parser handles STAP-A and FU-A.
    # Parsing reconstructed Annex-B also proves no aggregate header is passed
    # through as a codec NAL to the independent native decoder.
    return b"".join(h264_depayload(packet[12:]) for packet in packets)


@pytest.mark.parametrize("sps_nri", [0, 0x20, 0x40, 0x60])
@pytest.mark.parametrize("pps_nri", [0, 0x20, 0x40, 0x60])
def test_stapa_lengths_nri_headers_timestamp_and_sequence_wrap(sps_nri, pps_nri):
    sps, pps, idr = bytes((sps_nri | 7, 0xAB)), bytes((pps_nri | 8, 0xCD, 0xEF)), b"\x65xy"
    sender = H264Packetizer(0x12345678, sequence=65535)
    packets = sender.packetize(annexb((sps, pps, idr)), 0xABCDEF01, payload_type=99, mtu=128)
    assert len(packets) == 2
    expected = bytes((max(sps_nri, pps_nri) | 24,))
    expected += b"\x00\x02" + sps + b"\x00\x03" + pps
    assert packets[0].packet == bytes.fromhex("8063ffffabcdef0112345678") + expected
    assert packets[1].packet == bytes.fromhex("80e30000abcdef0112345678") + idr
    assert packets[0].payload_bytes == len(expected)
    assert packets[1].payload_bytes == len(idr)
    assert members(expected) == (sps, pps)
    assert sender.sequence == 1
    assert split_annexb(independent_reconstruction([item.packet for item in packets])) == (
        sps,
        pps,
        idr,
    )


@pytest.mark.parametrize("mtu", [128, 512, 1200, 1228, 1378])
def test_prefix_sei_aud_duplicates_and_fragmented_idr_keep_every_nal_in_order(mtu):
    sps, pps = b"\x67SPS", b"\x68PPS"
    source = (
        b"\x09\xf0",
        sps,
        b"\x06\x05\x01\x80",
        pps,
        sps,
        pps,
        b"\x06\x80",
        b"\x65" + b"\xab" * 12_000,
    )
    packets = H264Packetizer(123, sequence=1).packetize(
        annexb(source), 90000, payload_type=99, mtu=mtu
    )
    assert packets[0].packet[12:] == source[0]
    assert members(packets[1].packet[12:]) == source[1:6]
    assert packets[2].packet[12:] == source[6]
    assert packets[3].packet[12] & 31 == 28
    assert all(not item.packet[1] & 0x80 for item in packets[:-1])
    assert packets[-1].packet[1] & 0x80
    assert all(len(item.packet) + 10 <= min(mtu, MAX_UDP_PAYLOAD_BYTES) for item in packets)
    assert split_annexb(independent_reconstruction([item.packet for item in packets])) == source


def test_exact_aggregate_packet_limit_is_inclusive_without_parameter_fragmentation():
    sps, pps, idr = b"\x67" + b"a" * 49, b"\x68" + b"b" * 50, b"\x65xy"
    sender = H264Packetizer(123, sequence=10)
    packets = sender.packetize(annexb((sps, pps, idr)), 1, payload_type=99, mtu=128)
    assert len(packets[0].packet) + 10 == 128
    assert members(packets[0].packet[12:]) == (sps, pps)
    assert sender.sequence == 12
    with pytest.raises(RtpError, match="aggregation exceeds"):
        sender.packetize(annexb((sps + b"a", pps, idr)), 2, payload_type=99, mtu=128)
    assert sender.sequence == 12


@pytest.mark.parametrize(
    "source",
    [
        (b"\x65xy",),
        (b"\x67SPS", b"\x65xy"),
        (b"\x68PPS", b"\x65xy"),
        (b"\x67SPS", b"\x61xy"),
        (b"\x61xy", b"\x67SPS", b"\x68PPS"),
        (b"\x67SPS", b"\x65xy", b"\x68PPS"),
        (b"\x67SPS", b"\x68PPS"),
        (b"\x67", b"\x68PPS", b"\x65xy"),
        (b"\x67SPS", b"\x68", b"\x65xy"),
        (b"\x67" + b"a" * 65535, b"\x68PPS", b"\x65xy"),
        (b"\x67SPS", b"\x06" + b"a" * 2000, b"\x68PPS", b"\x65xy"),
    ],
    ids=(
        "bare-idr",
        "missing-pps",
        "missing-sps",
        "partial-p-prefix",
        "parameters-after-picture",
        "parameters-straddle-picture",
        "no-picture",
        "empty-sps",
        "empty-pps",
        "parameter-length-overflow",
        "oversized-intervening-prefix",
    ),
)
def test_missing_late_empty_or_oversized_parameters_reject_without_consuming_sequence(source):
    sender = H264Packetizer(123, sequence=100)
    with pytest.raises(RtpError):
        sender.packetize(annexb(source), 1, payload_type=99, mtu=1200)
    assert sender.sequence == 100


@pytest.mark.parametrize("kind", [0, 24, 25, 26, 27, 28, 29, 30, 31, 0x87])
def test_unsupported_nested_packetization_or_forbidden_nal_is_not_forwarded(kind):
    sender = H264Packetizer(123, sequence=100)
    with pytest.raises(RtpError):
        sender.packetize(annexb((bytes((kind,)) + b"bad",)), 1, payload_type=99, mtu=1200)
    assert sender.sequence == 100


def test_no_parameter_cache_can_reuse_old_configuration_or_cross_source():
    sender = H264Packetizer(123, sequence=100)
    first = sender.packetize(
        annexb((b"\x67old", b"\x68old", b"\x65xy")), 1, payload_type=99, mtu=1200
    )
    assert members(first[0].packet[12:]) == (b"\x67old", b"\x68old")
    with pytest.raises(RtpError, match="lacks current"):
        sender.packetize(annexb((b"\x65xy",)), 2, payload_type=99, mtu=1200)
    assert sender.sequence == 102
    second = sender.packetize(
        annexb((b"\x67new", b"\x68new", b"\x65xy")), 3, payload_type=99, mtu=1200
    )
    assert members(second[0].packet[12:]) == (b"\x67new", b"\x68new")
    p_frame = sender.packetize(annexb((b"\x61unchanged",)), 4, payload_type=99, mtu=1200)
    assert len(p_frame) == 1 and p_frame[0].packet[12:] == b"\x61unchanged"


def test_count_and_access_unit_byte_bounds_precede_sequence_consumption():
    sender = H264Packetizer(123, sequence=100)
    for payload in (
        annexb((b"\x61xx",) * 257),
        b"\0\0\0\1\x61" + b"x" * MAX_ACCESS_UNIT_BYTES,
    ):
        with pytest.raises(RtpError):
            sender.packetize(payload, 1, payload_type=99, mtu=1200)
        assert sender.sequence == 100


@pytest.mark.parametrize("profile", ["baseline", "main", "high"])
@pytest.mark.parametrize("mtu", [128, 1200, 1378])
def test_real_native_h264_stapa_srtp_and_independent_decoder_preserve_source(profile, mtu):
    source = encode_source(profile=profile, frames=5, force=(3,))
    sender = H264Packetizer(123, sequence=65530)
    secure = SecureVideo(b"k" * 16, b"s" * 14, 123)
    peer = pylibsrtp.Session(
        pylibsrtp.Policy(
            key=b"k" * 16 + b"s" * 14,
            ssrc_type=pylibsrtp.Policy.SSRC_SPECIFIC,
            ssrc_value=123,
        )
    )
    reference, decoder = av.CodecContext.create("h264", "r"), av.CodecContext.create("h264", "r")
    reference.thread_count = decoder.thread_count = 1
    decoded_count, idrs, last_sequence = 0, 0, 65529
    try:
        for payload, timestamp in source:
            nals = split_annexb(payload)
            packets = sender.packetize(payload, timestamp, payload_type=99, mtu=mtu)
            clear = []
            for item in packets:
                assert item.sequence == (last_sequence + 1) & 0xFFFF
                last_sequence = item.sequence
                protected = secure.protect(item.packet)
                assert len(protected) <= min(mtu, MAX_UDP_PAYLOAD_BYTES)
                assert len(protected) == len(item.packet) + 10
                recovered = peer.unprotect(protected)
                assert recovered == item.packet
                assert struct.unpack_from("!I", recovered, 4)[0] == timestamp
                clear.append(recovered)
            assert all(not packet[1] & 0x80 for packet in clear[:-1])
            assert clear[-1][1] & 0x80
            reconstructed = independent_reconstruction(clear)
            assert split_annexb(reconstructed) == nals
            if any(nal[0] & 31 == 5 for nal in nals):
                idrs += 1
                stap_index = next(
                    index for index, packet in enumerate(clear) if packet[12] & 31 == 24
                )
                assert {7, 8} <= {nal[0] & 31 for nal in members(clear[stap_index][12:])}
                assert not clear[stap_index][1] & 0x80
                independent = av.CodecContext.create("h264", "r")
                independent.thread_count = 1
                assert len(independent.decode(av.Packet(reconstructed))) == 1
            actual, expected = (
                decoder.decode(av.Packet(reconstructed)),
                reference.decode(av.Packet(payload)),
            )
            assert len(actual) == len(expected) == 1
            assert actual[0].to_ndarray().tobytes() == expected[0].to_ndarray().tobytes()
            assert (actual[0].width, actual[0].height) == (320, 240)
            decoded_count += 1
        assert decoded_count == 5 and idrs == 2
    finally:
        secure.close()
