# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-listener compatibility for the pinned Ring legacy Web Push receiver."""

from __future__ import annotations

import base64
import binascii
import re
from importlib.metadata import PackageNotFoundError, version

from cryptography.hazmat.primitives.asymmetric import ec
from firebase_messaging import FcmPushClient, FcmRegisterConfig
from ring_doorbell import RingEventListener
from ring_doorbell.const import FCM_API_KEY, FCM_APP_ID, FCM_PROJECT_ID, FCM_RING_SENDER_ID


class RingPushCompatibilityError(RuntimeError):
    """The installed SDK does not provide the reviewed listener contract."""


class _InvalidPushHeader(ValueError):
    """An incoming encryption header is invalid; never retain its value."""


def _canonical_header(value: str, expected_bytes: int) -> str:
    max_chars = 4 * ((expected_bytes + 2) // 3)
    if not isinstance(value, str) or not 1 <= len(value) <= max_chars:
        raise _InvalidPushHeader("invalid-push-header")
    if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value, flags=re.ASCII) is None:
        raise _InvalidPushHeader("invalid-push-header")
    unpadded = value.rstrip("=")
    padded = unpadded + "=" * (-len(unpadded) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise _InvalidPushHeader("invalid-push-header") from None
    if len(decoded) != expected_bytes:
        raise _InvalidPushHeader("invalid-push-header")
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
    if value not in (canonical, canonical.rstrip("=")):
        raise _InvalidPushHeader("invalid-push-header")
    if expected_bytes == 65:
        if decoded[0] != 4:
            raise _InvalidPushHeader("invalid-push-header")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded)
        except ValueError:
            raise _InvalidPushHeader("invalid-push-header") from None
    return canonical


class _HaloFcmPushClient(FcmPushClient):
    @staticmethod
    def _decrypt_raw_data(credentials, crypto_key_str, salt_str, raw_data):
        # Legacy aesgcm headers use RFC 7515 base64url without padding. Keep
        # compatibility with padded senders, then retain the SDK's authenticated
        # ECDH/AES-GCM decryption and its existing credential handling.
        crypto_key = _canonical_header(crypto_key_str, 65)
        salt = _canonical_header(salt_str, 16)
        return FcmPushClient._decrypt_raw_data(credentials, crypto_key, salt, raw_data)

    def _handle_data_message(self, msg):
        try:
            return super()._handle_data_message(msg)
        except _InvalidPushHeader:
            # Returning lets the SDK acknowledge this malformed message rather
            # than redeliver it indefinitely. No payload or identifier is logged.
            self._log_warn_with_limit("Ignoring malformed Ring push encryption header")


def create_ring_event_listener(ring, credentials, updated, *, config):
    """Install an isolated receiver while preserving the SDK's listener lifecycle."""
    try:
        compatible = (
            version("ring-doorbell") == "0.9.14" and version("firebase-messaging") == "0.4.5"
        )
    except PackageNotFoundError:
        compatible = False
    if not compatible:
        raise RingPushCompatibilityError("unsupported-ring-push-contract")

    listener = RingEventListener(ring, credentials, updated, config=config)
    # Mirror ring-doorbell 0.9.14 start(): a preinstalled receiver is reused.
    # This changes one listener instance, never either SDK's global classes.
    listener._receiver = _HaloFcmPushClient(
        listener._on_notification,
        FcmRegisterConfig(FCM_PROJECT_ID, FCM_APP_ID, FCM_API_KEY, FCM_RING_SENDER_ID),
        listener._credentials,
        listener._credentials_updated_cb,
        config=listener._config,
        http_client_session=listener._ring.auth._session,
    )
    return listener
