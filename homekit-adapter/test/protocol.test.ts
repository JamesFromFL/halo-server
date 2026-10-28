import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  decodeEnvelope, encodeEnvelope, FrameDecoder, MAX_FRAME_BYTES, ProtocolError,
  type Envelope,
} from "../src/protocol.js";

const fixtures = JSON.parse(readFileSync(
  process.env.HALO_PROTOCOL_FIXTURES ??
    new URL("../../../src/halo_server/homekit/protocol/v1/fixtures.json", import.meta.url), "utf8",
)) as { valid: Envelope[]; invalid: unknown[] };
const ping = fixtures.valid.find(message => message.kind === "ping")!;

test("canonical Python IPC fixtures pass and invalid fixtures fail", () => {
  for (const fixture of fixtures.valid) {
    const framed = encodeEnvelope(fixture);
    assert.deepEqual(decodeEnvelope(framed.subarray(4)), fixture);
  }
  for (const fixture of fixtures.invalid) {
    assert.throws(() => encodeEnvelope(fixture), ProtocolError);
    assert.throws(() => decodeEnvelope(Buffer.from(JSON.stringify(fixture))), ProtocolError);
  }
});

test("split header/body and coalesced frames preserve exact message sequence", () => {
  const decoder = new FrameDecoder();
  const frame = encodeEnvelope(ping);
  const result: Envelope[] = [];
  for (const byte of frame) result.push(...decoder.feed(Buffer.from([byte])));
  assert.deepEqual(result, [ping]);
  assert.equal(decoder.partial, false);
  assert.deepEqual(decoder.feed(Buffer.concat([frame, frame])), [ping, ping]);
  decoder.end();
});

test("exact frame body cap accepts padding and rejects larger/zero advertised lengths", () => {
  const body = Buffer.from(JSON.stringify(ping));
  const padded = Buffer.concat([body, Buffer.alloc(MAX_FRAME_BYTES - body.length, 0x20)]);
  assert.deepEqual(decodeEnvelope(padded), ping);
  assert.throws(() => decodeEnvelope(Buffer.concat([padded, Buffer.from(" ")])), ProtocolError);
  for (const length of [0, MAX_FRAME_BYTES + 1, 0xffffffff]) {
    const header = Buffer.alloc(4);
    header.writeUInt32BE(length);
    const decoder = new FrameDecoder();
    assert.throws(() => decoder.feed(header), ProtocolError);
    assert.throws(() => decoder.feed(encodeEnvelope(ping)), /decoder-closed/);
  }
});

test("truncated body/header cannot revive a decoder", () => {
  for (const cut of [1, 3, 4, 10]) {
    const decoder = new FrameDecoder();
    decoder.feed(encodeEnvelope(ping).subarray(0, cut));
    assert.throws(() => decoder.end(), /truncated-frame/);
    assert.throws(() => decoder.feed(encodeEnvelope(ping)), /decoder-closed/);
  }
});

test("ambiguous JSON, invalid UTF8/BOM and nonfinite numbers fail", () => {
  const samples = [
    Buffer.from('{"v":1,"v":1}'),
    Buffer.from('{"v":1,"\\u0076":1}'),
    Buffer.from('{"x":1e999}'),
    Buffer.from('{"x":NaN}'),
    Buffer.from("[".repeat(40) + "0" + "]".repeat(40)),
    Buffer.from([0xff]),
    Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(JSON.stringify(ping))]),
  ];
  for (const sample of samples) assert.throws(() => decodeEnvelope(sample), ProtocolError);
});

test("canonical UUID, bounded strings and protocol fields match Python semantics", () => {
  const hello = structuredClone(fixtures.valid[0]!);
  hello.payload.build_id = "😀".repeat(128);
  assert.deepEqual(decodeEnvelope(encodeEnvelope(hello).subarray(4)), hello);
  hello.payload.build_id += "😀";
  assert.throws(() => encodeEnvelope(hello), ProtocolError);
  hello.payload.build_id = "untrusted\ncontrol";
  assert.throws(() => encodeEnvelope(hello), ProtocolError);
  hello.payload.build_id = "valid";
  hello.boot_id = "00000000-0000-0000-0000-00000000000A";
  assert.throws(() => encodeEnvelope(hello), ProtocolError);
});

test("burst output from one feed is bounded", () => {
  const decoder = new FrameDecoder();
  assert.throws(() => decoder.feed(Buffer.concat(Array(257).fill(encodeEnvelope(ping)))), /frame-burst/);
});
