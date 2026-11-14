import asyncio
import base64
import binascii
import logging
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest
import ring_doorbell.listen.eventlistener as sdk_listener
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from firebase_messaging import FcmPushClient, FcmPushClientConfig, FcmRegisterConfig
from firebase_messaging.fcmpushclient import FcmPushClientRunState
from firebase_messaging.proto.mcs_pb2 import DataMessageStanza
from http_ece import ECEException, encrypt
from ring_doorbell import RingEventListener, RingEventListenerConfig
from ring_doorbell.const import FCM_API_KEY, FCM_APP_ID, FCM_PROJECT_ID, FCM_RING_SENDER_ID

from halo_server.ring import push


def _base64(value):
    return base64.urlsafe_b64encode(value).decode("ascii")


def _public(key):
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


@pytest.fixture
def encrypted():
    receiver = ec.generate_private_key(ec.SECP256R1())
    sender = ec.generate_private_key(ec.SECP256R1())
    salt, auth = bytes(range(16)), bytes(range(16, 32))
    credentials = {
        "keys": {
            "private": _base64(
                receiver.private_bytes(
                    serialization.Encoding.DER,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            ).rstrip("="),
            "secret": _base64(auth).rstrip("="),
        },
        "gcm": {"app_id": "offline-fixture"},
    }
    plaintext = b'{"fixture":"offline-only"}'
    ciphertext = encrypt(
        plaintext,
        salt=salt,
        private_key=sender,
        dh=_public(receiver),
        auth_secret=auth,
        version="aesgcm",
    )
    return SimpleNamespace(
        credentials=credentials,
        plaintext=plaintext,
        ciphertext=ciphertext,
        key=_base64(_public(sender)),
        salt=_base64(salt),
    )


@pytest.mark.parametrize("key_padding", [False, True])
@pytest.mark.parametrize("salt_padding", [False, True])
def test_decrypts_authenticated_payload_with_padded_or_unpadded_headers(
    encrypted, key_padding, salt_padding
):
    key = encrypted.key if key_padding else encrypted.key.rstrip("=")
    salt = encrypted.salt if salt_padding else encrypted.salt.rstrip("=")
    if not (key_padding and salt_padding):
        with pytest.raises(binascii.Error, match="Incorrect padding"):
            FcmPushClient._decrypt_raw_data(encrypted.credentials, key, salt, encrypted.ciphertext)
    assert (
        push._HaloFcmPushClient._decrypt_raw_data(
            encrypted.credentials, key, salt, encrypted.ciphertext
        )
        == encrypted.plaintext
    )


def _malformed_header(encrypted, name):
    key, salt = encrypted.key, encrypted.salt
    if name == "empty":
        return "", salt
    if name == "wrong_type":
        return key.encode("ascii"), salt
    if name == "non_ascii":
        return "é" + key[1:], salt
    if name == "whitespace":
        return " " + key[1:], salt
    if name == "alphabet":
        return "!" + key[1:], salt
    if name == "standard_alphabet":
        return "+" + key[1:], salt
    if name == "oversized":
        return key + "A", salt
    if name == "excess_padding":
        return key + "=", salt
    if name == "interior_padding":
        return key[:3] + "=" + key[4:], salt
    if name == "partial_padding":
        return key, salt[:-1]
    if name == "noncanonical_bits":
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        unpadded = key.rstrip("=")
        last = alphabet[(alphabet.index(unpadded[-1]) & ~3) | 1]
        return unpadded[:-1] + last, salt
    if name == "key_length":
        return _base64(bytes(64)), salt
    if name == "salt_length":
        return key, _base64(bytes(15))
    if name == "point_prefix":
        return _base64(b"\x03" + bytes(64)), salt
    if name == "invalid_point":
        return _base64(b"\x04" + bytes(64)), salt
    raise AssertionError(name)


@pytest.mark.parametrize(
    "name",
    [
        "empty",
        "wrong_type",
        "non_ascii",
        "whitespace",
        "alphabet",
        "standard_alphabet",
        "oversized",
        "excess_padding",
        "interior_padding",
        "partial_padding",
        "noncanonical_bits",
        "key_length",
        "salt_length",
        "point_prefix",
        "invalid_point",
    ],
)
def test_rejects_malformed_headers_without_retaining_the_value(encrypted, name):
    key, salt = _malformed_header(encrypted, name)
    with pytest.raises(push._InvalidPushHeader, match="^invalid-push-header$"):
        push._HaloFcmPushClient._decrypt_raw_data(
            encrypted.credentials, key, salt, encrypted.ciphertext
        )


@pytest.mark.parametrize("failure", ["ciphertext", "key", "salt", "empty"])
def test_cryptographic_verification_still_rejects_invalid_payloads(encrypted, failure):
    key, salt, ciphertext = encrypted.key, encrypted.salt, encrypted.ciphertext
    if failure == "ciphertext":
        ciphertext = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
    elif failure == "key":
        key = _base64(_public(ec.generate_private_key(ec.SECP256R1())))
    elif failure == "salt":
        salt = _base64(bytes(reversed(range(16))))
    else:
        ciphertext = b""
    with pytest.raises(ECEException):
        push._HaloFcmPushClient._decrypt_raw_data(encrypted.credentials, key, salt, ciphertext)


def _stanza(encrypted, *, identifier="fixture", key=None):
    message = DataMessageStanza(persistent_id=identifier, raw_data=encrypted.ciphertext)
    message.app_data.add(key="crypto-key", value="dh=" + (encrypted.key if key is None else key))
    message.app_data.add(key="encryption", value="salt=" + encrypted.salt.rstrip("="))
    message.app_data.add(key="subtype", value="offline-fixture")
    return message


@pytest.mark.asyncio
async def test_malformed_headers_drop_with_bounded_warning_and_next_message_delivers(
    encrypted, caplog
):
    delivered, acknowledged = [], []
    config = FcmPushClientConfig(log_warn_limit=2)
    receiver = push._HaloFcmPushClient(
        lambda data, *args: delivered.append(data),
        FcmRegisterConfig("fixture-project", "fixture-app", "fixture-key", "fixture-sender"),
        encrypted.credentials,
        config=config,
    )
    receiver.run_state = FcmPushClientRunState.STARTED

    async def acknowledge(identifier):
        acknowledged.append(identifier)

    receiver._send_selective_ack = acknowledge
    with caplog.at_level(logging.WARNING):
        for index in range(4):
            await receiver._handle_message(
                _stanza(encrypted, identifier=f"invalid-{index}", key="!private-header")
            )
        assert delivered == []
        await receiver._handle_message(
            _stanza(encrypted, identifier="valid", key=encrypted.key.rstrip("="))
        )
    assert delivered == [{"fixture": "offline-only"}]
    assert receiver.run_state is FcmPushClientRunState.STARTED
    assert receiver.persistent_ids == ["invalid-0", "invalid-1", "invalid-2", "invalid-3", "valid"]
    assert acknowledged == receiver.persistent_ids
    assert [record.getMessage() for record in caplog.records] == [
        "Ignoring malformed Ring push encryption header"
    ] * 2


def _ring():
    return SimpleNamespace(
        auth=SimpleNamespace(get_device_model=lambda: "offline-fixture", _session=object())
    )


def test_factory_installs_only_its_receiver_with_exact_sdk_inputs():
    ring = _ring()
    credentials = {"fixture": "credentials"}
    updates = []
    config = RingEventListenerConfig.default_config()
    listener = push.create_ring_event_listener(ring, credentials, updates.append, config=config)
    second = push.create_ring_event_listener(ring, None, None, config=config)
    original = RingEventListener(ring, credentials, config=config)
    receiver = listener._receiver
    assert type(listener) is RingEventListener
    assert isinstance(receiver, push._HaloFcmPushClient)
    assert receiver is not second._receiver
    assert original._receiver is None
    assert sdk_listener.FcmPushClient is FcmPushClient
    assert receiver.callback == listener._on_notification
    assert receiver.credentials is credentials
    assert receiver.credentials_updated_callback == listener._credentials_updated_cb
    assert receiver.config is config
    assert receiver._http_client_session is ring.auth._session
    assert receiver.fcm_config == FcmRegisterConfig(
        FCM_PROJECT_ID, FCM_APP_ID, FCM_API_KEY, FCM_RING_SENDER_ID
    )
    updated = {"fixture": "updated"}
    receiver.credentials_updated_callback(updated)
    assert listener._credentials is updated
    assert updates == [updated]
    assert second._credentials is None


@pytest.mark.asyncio
async def test_sdk_start_and_stop_reuse_the_preinstalled_receiver(monkeypatch):
    ring = _ring()
    ring._add_event_to_dings_data = lambda event: None
    updates, calls = [], []
    listener = push.create_ring_event_listener(
        ring, None, updates.append, config=RingEventListenerConfig.default_config()
    )
    receiver = listener._receiver
    updated = {"fixture": "updated"}

    async def checkin():
        calls.append("checkin")
        receiver.credentials_updated_callback(updated)
        return "offline-fixture-token"

    async def subscribe(token):
        assert token == "offline-fixture-token"
        calls.append("subscribe")
        listener.subscribed = True

    async def start():
        calls.append("start")

    async def stop():
        calls.append("stop")

    async def refresh():
        await asyncio.Event().wait()

    def unexpected_receiver(*args, **kwargs):
        raise AssertionError("SDK replaced the preinstalled receiver")

    monkeypatch.setattr(sdk_listener, "FcmPushClient", unexpected_receiver)
    monkeypatch.setattr(receiver, "checkin_or_register", checkin)
    monkeypatch.setattr(receiver, "start", start)
    monkeypatch.setattr(receiver, "stop", stop)
    monkeypatch.setattr(listener, "add_subscription_to_ring", subscribe)
    monkeypatch.setattr(listener, "_periodic_session_refresh", refresh)
    assert await listener.start()
    refresh_task = listener.session_refresh_task
    assert listener._receiver is receiver
    assert receiver._http_client_session is ring.auth._session
    assert listener._credentials is updated
    assert updates == [updated]
    await listener.stop()
    await asyncio.sleep(0)
    assert calls == ["checkin", "subscribe", "start", "stop"]
    assert not listener.started
    assert listener._callbacks == {}
    assert listener.session_refresh_task is None
    assert refresh_task.cancelled()


@pytest.mark.parametrize("unsupported", ["ring-doorbell", "firebase-messaging", "missing"])
def test_factory_version_gate_precedes_private_contract_access(monkeypatch, unsupported):
    def installed(name):
        if unsupported == "missing":
            raise PackageNotFoundError(name)
        if name == unsupported:
            return "999.0"
        return {"ring-doorbell": "0.9.14", "firebase-messaging": "0.4.5"}[name]

    monkeypatch.setattr(push, "version", installed)
    with pytest.raises(push.RingPushCompatibilityError, match="^unsupported-ring-push-contract$"):
        push.create_ring_event_listener(object(), None, None, config=object())
