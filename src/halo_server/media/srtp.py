"""Per-output native libsrtp contexts; authenticate before feedback authority."""

from __future__ import annotations

import struct

import pylibsrtp

MAX_SECURE_PACKET_BYTES = 16 * 1024 + 64


class SrtpError(ValueError):
    """A secure packet was invalid; key material and packets are never formatted."""


class SecureVideo:
    """One negotiated AES128-CM/SHA1-80 track with separate direction state.

    The first authenticated remote RTCP sender is pinned. A wildcard policy must
    not allocate unlimited native stream state from varying peer SSRC values.
    """

    def __init__(self, key: bytes, salt: bytes, ssrc: int):
        if not isinstance(key, bytes) or len(key) != 16:
            raise SrtpError("SRTP key length is invalid")
        if not isinstance(salt, bytes) or len(salt) != 14:
            raise SrtpError("SRTP salt length is invalid")
        if type(ssrc) is not int or not 1 <= ssrc <= 0xFFFFFFFF:
            raise SrtpError("SRTP synchronization source is invalid")
        self._material = key + salt
        self._ssrc = ssrc
        self._tx = self._session(ssrc)
        self._rx = None
        self._remote_ssrc: int | None = None
        self._closed = False

    def _session(self, ssrc):
        policy = pylibsrtp.Policy(
            key=self._material,
            ssrc_type=pylibsrtp.Policy.SSRC_SPECIFIC,
            ssrc_value=ssrc,
            srtp_profile=pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
        )
        policy.window_size = 128
        return pylibsrtp.Session(policy)

    def _call(self, method, packet):
        if (
            self._closed
            or not isinstance(packet, bytes)
            or not 8 <= len(packet) <= MAX_SECURE_PACKET_BYTES
        ):
            raise SrtpError("Secure packet is unavailable or outside its bounds")
        try:
            return method(packet)
        except pylibsrtp.Error as exc:
            raise SrtpError("Secure packet authentication or protection failed") from exc

    def protect(self, packet: bytes) -> bytes:
        if self._closed:
            raise SrtpError("Secure track is closed")
        return self._call(self._tx.protect, packet)

    def protect_rtcp(self, packet: bytes) -> bytes:
        if self._closed:
            raise SrtpError("Secure track is closed")
        return self._call(self._tx.protect_rtcp, packet)

    def unprotect_rtcp(self, packet: bytes) -> bytes:
        if (
            self._closed
            or not isinstance(packet, bytes)
            or not 22 <= len(packet) <= MAX_SECURE_PACKET_BYTES
        ):
            raise SrtpError("Secure control packet is outside its bounds")
        ssrc = struct.unpack_from("!I", packet, 4)[0]
        if self._remote_ssrc is not None and self._remote_ssrc != ssrc:
            raise SrtpError("Secure control sender changed")
        candidate = self._rx if self._rx is not None else self._session(ssrc)
        result = self._call(candidate.unprotect_rtcp, packet)
        if self._rx is None:
            self._rx, self._remote_ssrc = candidate, ssrc
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._tx = self._rx = None
        self._material = b""
