import assert from "node:assert/strict";
import { test } from "node:test";
import {
  decodeDeviceEnvelope,
  DeviceFrameDecoder,
  encodeDeviceEnvelope,
  validateDeviceEnvelope,
  validateDevicePublication,
  type DeviceEnvelope,
} from "../src/device-protocol.js";
import { deviceChild, devicePublication } from "./device-fixtures.js";
const message = (
  kind: DeviceEnvelope["kind"] = "ping",
  payload: Record<string, unknown> = {},
): DeviceEnvelope => ({
  v: 1,
  kind,
  boot_id: "00000000-0000-4000-8000-000000000001",
  request_id: "00000000-0000-4000-8000-000000000002",
  payload,
});
test("device framing survives every single split and joined frames", () => {
  const first = message("publish", { ...devicePublication() }),
    frame = encodeDeviceEnvelope(first);
  for (let i = 0; i <= frame.length; i++) {
    const decoder = new DeviceFrameDecoder();
    assert.deepEqual(
      [
        ...decoder.feed(frame.subarray(0, i)),
        ...decoder.feed(frame.subarray(i)),
      ],
      [first],
    );
    decoder.end();
  }
  assert.deepEqual(
    new DeviceFrameDecoder().feed(
      Buffer.concat([frame, encodeDeviceEnvelope(message())]),
    ),
    [first, message()],
  );
});
test("device protocol rejects unknown authority, secrets, duplicate JSON keys, invalid UTF8 and bounded frame attacks", () => {
  for (const kind of [
    "execute",
    "panic",
    "alarm_arm",
    "camera_event",
    "hello_ack",
  ])
    assert.throws(() =>
      validateDeviceEnvelope({ ...message(), kind }, "python"),
    );
  assert.throws(() =>
    validateDeviceEnvelope({ ...message(), payload: { token: "never" } }),
  );
  for (const bytes of [
    Buffer.from(JSON.stringify(message()).replace('"v":1', '"v":1,"v":1')),
    Buffer.from([0xff]),
    Buffer.from('{"__proto__":{}}'),
    Buffer.from("[".repeat(17) + "0" + "]".repeat(17)),
  ])
    assert.throws(() => decodeDeviceEnvelope(bytes));
  for (const size of [0, 262145, 0xffffffff]) {
    const header = Buffer.alloc(4);
    header.writeUInt32BE(size);
    const decoder = new DeviceFrameDecoder();
    assert.throws(() => decoder.feed(header), /frame-length/);
    assert.throws(() => decoder.feed(Buffer.alloc(0)), /decoder-closed/);
  }
  const decoder = new DeviceFrameDecoder();
  decoder.feed(Buffer.from([0]));
  assert.throws(() => decoder.end(), /truncated-frame/);
  assert.throws(
    () =>
      new DeviceFrameDecoder().feed(
        Buffer.concat(
          Array.from({ length: 33 }, () => encodeDeviceEnvelope(message())),
        ),
      ),
    /frame-burst/,
  );
});
test("publication validates UUID, names, capacity, exact typed observations and unmapped-state fences", () => {
  const valid = devicePublication();
  assert.doesNotThrow(() => validateDevicePublication(valid));
  for (const patch of [
    { available: false },
    { mappings: ["contact", "contact"] },
    { mappings: ["smoke", "contact"] },
    { mappings: ["switch"] },
    { name: "secret\nline" },
    { name: "\u200b" },
    { accessory_uuid: valid.bridge.accessory_uuid },
    { values: { ...valid.children[0]!.values, contact: 0 } },
    { values: { ...valid.children[0]!.values, motion: false } },
    { values: { ...valid.children[0]!.values, panic: true } },
  ])
    assert.throws(() =>
      validateDevicePublication({
        ...valid,
        children: [{ ...deviceChild(), ...patch }],
      }),
    );
  assert.throws(() =>
    validateDevicePublication({
      ...valid,
      children: [deviceChild(), deviceChild()],
    }),
  );
  assert.throws(() =>
    validateDevicePublication({ ...valid, expires_at_ms: NaN }),
  );
  assert.throws(() => validateDevicePublication({ ...valid, sequence: 1.1 }));
});

test("device wire identifiers reject final line terminators in JavaScript regex end anchors", () => {
  for (const suffix of ["\n", "\r", "\u2028", "\u2029"]) {
    const valid = devicePublication();
    assert.throws(() =>
      validateDeviceEnvelope({
        ...message(),
        boot_id: message().boot_id + suffix,
      }),
    );
    assert.throws(() =>
      validateDevicePublication({
        ...valid,
        bridge: { ...valid.bridge, username: valid.bridge.username + suffix },
      }),
    );
    assert.throws(() =>
      validateDevicePublication({
        ...valid,
        children: [
          { ...deviceChild(), device_id: deviceChild().device_id + suffix },
        ],
      }),
    );
  }
});
