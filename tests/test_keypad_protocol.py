"""Wire schemas and transport admission using in-memory fixtures only."""

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from halo_server.ring.hub.models import AlarmCapability
from halo_server.ring.hub.protocol import (
    AlarmSchemaError,
    DeviceDocument,
    DeviceInfoDocUpdate,
    encode_keypad_brightness_command,
    parse_clap_frame,
)
from halo_server.ring.hub.provider import AlarmCommandSupersededError, AlarmLocationSeed
from halo_server.ring.hub.store import keypad_brightness
from halo_server.ring.hub.transport import AlarmTicketAsset, AlarmTransportError, RingAlarmTransport


def frame(*, inventory=False, body=None):
    return json.dumps(
        {
            "channel": "message" if inventory else "DataUpdate",
            "msg": {
                "msg": "DeviceInfoDocGetList" if inventory else "DeviceInfoDocUpdate",
                "datatype": "DeviceInfoDocType",
                "src": "asset",
                "body": body
                if body is not None
                else [
                    {
                        "general": {"v2": {"zid": "keypad"}},
                        "impulse": {"v1": [{"impulseType": "keypad.motion"}]},
                    }
                ],
            },
        }
    )


def test_event_parser_preserves_only_known_realtime_event_without_raw_payload():
    message = parse_clap_frame(
        frame(
            body=[
                {
                    "general": {"v2": {"zid": "other"}},
                    "impulse": {"v1": [{"impulseType": "access-code", "code": "not-retained"}]},
                },
                {
                    "general": {"v2": {"zid": "keypad"}},
                    "impulse": {
                        "v1": [
                            {"impulseType": "unknown", "data": "discarded"},
                            {"impulseType": "keypad.motion", "data": {"secret": "discarded"}},
                        ]
                    },
                },
            ]
        )
    )
    assert isinstance(message, DeviceInfoDocUpdate)
    assert not message.devices[0].keypad_motion
    assert message.devices[1].keypad_motion
    assert dict(message.devices[1].data) == {"zid": "keypad"}
    assert "discarded" not in repr(message) and "not-retained" not in repr(message)
    assert not parse_clap_frame(frame(inventory=True)).devices[0].keypad_motion


@pytest.mark.parametrize(
    "impulse",
    [
        None,
        {},
        [],
        {"v1": {}},
        {"v1": [None, "keypad.motion"]},
        {"v1": [{"impulseType": "keypad.motion.other"}]},
    ],
)
def test_unqualified_impulses_do_not_produce_motion(impulse):
    message = parse_clap_frame(
        frame(body=[{"general": {"v2": {"zid": "keypad"}}, "impulse": impulse}])
    )
    assert not message.devices[0].keypad_motion


def test_mismatched_general_identity_cannot_redirect_impulse():
    with pytest.raises(AlarmSchemaError):
        parse_clap_frame(
            frame(
                body=[
                    {
                        "general": {"v2": {"zid": "keypad"}},
                        "device": {"v1": {"zid": "other"}},
                        "impulse": {"v1": [{"impulseType": "keypad.motion"}]},
                    }
                ]
            )
        )
    with pytest.raises(AlarmSchemaError):
        DeviceDocument("keypad", {"zid": "keypad"}, keypad_motion="true")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),
        (1, 100),
        (0.005, 1),
        (0.125, 13),
        (0.145, 15),
        (0.285, 29),
        (0.994, 99),
        (0.995, 100),
        (False, None),
        (None, None),
        ("0.5", None),
        (-0.1, None),
        (1.01, None),
        (float("nan"), None),
        (float("inf"), None),
        (10**400, None),
    ],
)
def test_normalization_is_finite_exact_and_half_up(raw, expected):
    assert keypad_brightness(raw) == expected
    assert str(AlarmCapability.KEYPAD_BRIGHTNESS) == "keypad-brightness"


@pytest.mark.parametrize("value", [0, 1, 35, 100])
def test_only_brightness_field_is_encoded(value):
    encoded = encode_keypad_brightness_command("asset", "keypad", value, 1)
    assert encoded == {
        "channel": "message",
        "msg": {
            "msg": "DeviceInfoSet",
            "datatype": "DeviceInfoSetType",
            "dst": "asset",
            "body": [{"zid": "keypad", "device": {"v1": {"brightness": value / 100}}}],
            "seq": 1,
        },
    }


@pytest.mark.parametrize("value", [True, False, None, "50", 0.5, -1, 101])
def test_invalid_brightness_encoder_targets_fail_before_socket(value):
    with pytest.raises(ValueError):
        encode_keypad_brightness_command("asset", "keypad", value, 1)


class Socket:
    closed = False

    def __init__(self):
        self.sent = []

    async def send_str(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


def transport(kind="base_station_v1"):
    instance = RingAlarmTransport(SimpleNamespace(), AlarmLocationSeed("home"))
    instance._websocket = Socket()
    instance._connection_epoch = 1
    instance._assets = (AlarmTicketAsset("asset", kind, "online"),)
    return instance


@contextmanager
def guard():
    yield


@pytest.mark.asyncio
async def test_transport_requires_exact_opt_in_and_asset_kind():
    instance = transport()
    socket = instance._websocket
    try:
        with pytest.raises(AlarmTransportError):
            await instance._send_encoded(
                lambda seq: encode_keypad_brightness_command("asset", "keypad", 10, seq),
                epoch=1,
                admission=guard,
            )
        assert not socket.sent
        await instance.send_keypad_brightness("asset", "keypad", 10, epoch=1, admission=guard)
        assert len(socket.sent) == 1
        assert socket.sent[0]["msg"]["body"] == [
            {"zid": "keypad", "device": {"v1": {"brightness": 0.1}}}
        ]
    finally:
        await instance.close()
    wrong = transport("beams_bridge_v1")
    try:
        with pytest.raises(AlarmTransportError):
            await wrong.send_keypad_brightness("asset", "keypad", 10, epoch=1, admission=guard)
        assert not wrong._websocket.sent
    finally:
        await wrong.close()


@pytest.mark.asyncio
async def test_final_admission_runs_after_transport_queue_without_retiring_socket():
    instance = transport()
    socket = instance._websocket
    allowed = [True]

    @contextmanager
    def admission():
        if not allowed[0]:
            raise AlarmCommandSupersededError("policy-denied")
        yield

    await instance._send_lock.acquire()
    task = asyncio.create_task(
        instance.send_keypad_brightness("asset", "keypad", 10, epoch=1, admission=admission)
    )
    await asyncio.sleep(0)
    allowed[0] = False
    instance._send_lock.release()
    try:
        with pytest.raises(AlarmCommandSupersededError):
            await task
        assert not socket.sent and not socket.closed
    finally:
        await instance.close()
