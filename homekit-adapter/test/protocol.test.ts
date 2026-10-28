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

test("semantic and raw corpus cases fail actual Node decoder", () => {
  const data = fixtures as unknown as {semantic_invalid: unknown[]; raw_invalid_hex: string[]};
  for (const message of data.semantic_invalid) {
    assert.throws(() => decodeEnvelope(Buffer.from(JSON.stringify(message))), ProtocolError);
  }
  for (const hex of data.raw_invalid_hex) assert.throws(() => decodeEnvelope(Buffer.from(hex, "hex")), ProtocolError);
});

test("fixed bulk headers agree byte-for-byte and reject malformed headers before body allocation", async () => {
  const {encodeBulkHeader, decodeBulkHeader, BULK_HEADER_BYTES} = await import("../src/protocol.js");
  const data = fixtures as unknown as {bulk_valid: {header: import("../src/protocol.js").BulkHeader; hex: string}[]; bulk_invalid_hex: string[]};
  for (const item of data.bulk_valid) {
    const header = Buffer.from(item.hex, "hex");
    assert.equal(header.length, BULK_HEADER_BYTES);
    assert.deepEqual(encodeBulkHeader(item.header), header);
    assert.deepEqual(decodeBulkHeader(header), item.header);
  }
  for (const hex of data.bulk_invalid_hex) assert.throws(() => decodeBulkHeader(Buffer.from(hex, "hex")), ProtocolError);
  const header = data.bulk_valid[0]!.header;
  for (const mutation of [{sequence: true}, {sequence: -1}, {length: 0}, {length: 65537}]) {
    assert.throws(() => encodeBulkHeader({...header, ...mutation} as import("../src/protocol.js").BulkHeader), ProtocolError);
  }
});

test("direction checks reject authority sent from the wrong peer including bulk producer reversal", async () => {
  const {validateDirection} = await import("../src/protocol.js");
  const samples = new Map(fixtures.valid.map(value => [value.kind, value]));
  for (const kind of ["inventory_page", "camera_status", "policy_ack", "lease_result", "snapshot_result", "control_result"] as const) {
    validateDirection(samples.get(kind), "python");
    assert.throws(() => validateDirection(samples.get(kind), "hap"), ProtocolError);
  }
  for (const kind of ["inventory_request", "policy_update", "lease_request", "lease_stop", "snapshot_request", "talkback_request", "control_request"] as const) {
    validateDirection(samples.get(kind), "hap");
    assert.throws(() => validateDirection(samples.get(kind), "python"), ProtocolError);
  }
  for (const message of fixtures.valid.filter(item => item.kind === "transfer_open")) {
    const producer = message.payload.kind === "talkback_pcm" ? "hap" : "python";
    validateDirection(message, producer);
    assert.throws(() => validateDirection(message, producer === "hap" ? "python" : "hap"), ProtocolError);
  }
});

test("reply correlation fences boot/request identity, full privacy scope and each owned handle", async () => {
  const {REPLY_KINDS, validateReply} = await import("../src/protocol.js");
  const samples = new Map([...fixtures.valid].reverse().map(value => [value.kind, value]));
  for (const [kind, replyKind] of Object.entries(REPLY_KINDS)) {
    const request = samples.get(kind as Envelope["kind"])!, reply = samples.get(replyKind)!;
    assert.deepEqual(validateReply(request, reply), reply);
    for (const key of ["boot_id", "request_id"] as const) {
      assert.throws(() => validateReply(request, {...reply, [key]: "00000000-0000-0000-0000-999999999999"}), ProtocolError);
    }
    for (const key of ["session_id", "job_id", "operation_id", "transfer_id"]) {
      if (Object.hasOwn(request.payload, key)) {
        assert.throws(() => validateReply(request, {...reply, payload: {...reply.payload, [key]: "00000000-0000-0000-0000-999999999999"}}), ProtocolError);
      }
    }
    const original = request.payload.scope as Record<string, unknown> | undefined;
    if (original) for (const key of Object.keys(original)) {
      const value = original[key];
      const scope = {...original, [key]: typeof value === "number" ? value + 1 : "00000000-0000-0000-0000-999999999999"};
      assert.throws(() => validateReply(request, {...reply, payload: {...reply.payload, scope}}), ProtocolError);
    }
  }
  const request = structuredClone(samples.get("inventory_request")!);
  request.payload.revision = 50;
  assert.throws(() => validateReply(request, samples.get("inventory_page")), ProtocolError);
});

test("every nested DTO boundary rejects unknown fields", () => {
  const mutate = (message: Envelope, node: unknown, path: (string | number)[] = []): void => {
    if (Array.isArray(node)) { node.forEach((child, index) => mutate(message, child, [...path, index])); return; }
    if (node === null || typeof node !== "object") return;
    const value = node as Record<string, unknown>;
    const mutation = structuredClone(message);
    let target: unknown = mutation;
    for (const key of path) target = (target as Record<string | number, unknown>)[key];
    (target as Record<string, unknown>).unrecognized = null;
    assert.throws(() => decodeEnvelope(Buffer.from(JSON.stringify(mutation))), ProtocolError);
    for (const [key, child] of Object.entries(value)) mutate(message, child, [...path, key]);
  };
  for (const message of fixtures.valid) mutate(message, message);
});

test("control cap counts UTF8 bytes even when a maximum-count inventory page needs splitting", () => {
  const maximal = fixtures.valid.find(item => item.kind === "inventory_page" &&
    (item.payload.devices as {observed_format: unknown}[]).some(device => device.observed_format !== null))!;
  const page = structuredClone(maximal);
  const devices = page.payload.devices as Record<string, unknown>[];
  const device = devices[0]!;
  device.family = device.kind = device.model = "😀".repeat(128);
  page.payload.total = 32;
  page.payload.devices = Array.from({length: 32}, (_, index) => ({...structuredClone(device),
    scope: {...device.scope as Record<string, unknown>, camera_id: String(index)}}));
  assert.ok(Buffer.byteLength(JSON.stringify(page)) > MAX_FRAME_BYTES);
  assert.throws(() => encodeEnvelope(page), ProtocolError);
});

test("literal text acceptance is pinned to the same Unicode16 ranges as Python and schema", async () => {
  const {FORBIDDEN_TEXT_RANGES} = await import("../src/protocol.js");
  const ranges = (fixtures as unknown as {unicode16_forbidden_ranges: [number, number][]}).unicode16_forbidden_ranges;
  assert.deepEqual(FORBIDDEN_TEXT_RANGES, ranges);
  const sample = fixtures.valid.find(message => message.kind === "policy_ack")!;
  for (const [start, end] of ranges) for (const point of new Set([start, end])) {
    const message = structuredClone(sample);
    message.payload.scope = {...message.payload.scope as Record<string, unknown>, camera_id: `a${String.fromCodePoint(point)}b`};
    assert.throws(() => encodeEnvelope(message), ProtocolError);
  }
});

test("S07 configuration and event replies correlate null scope and each registry/page identity", async () => {
  const {validateReply} = await import("../src/protocol.js");
  const samples = new Map([...fixtures.valid].reverse().map(value => [value.kind, value]));
  for (const kind of ["accessory_config", "event_sync"] as const) for (const nullScope of [false, true]) {
    const request = structuredClone(samples.get(kind)!);
    const reply = structuredClone(samples.get(kind === "accessory_config" ? "accessory_config_ack" : "event_sync_ack")!);
    if (nullScope) {
      request.payload.scope = reply.payload.scope = null;
      if (kind === "accessory_config") request.payload.entries = [];
    }
    assert.deepEqual(validateReply(request, reply), reply);
    const stale = structuredClone(reply);
    stale.payload.scope = nullScope ? samples.get(kind)!.payload.scope : null;
    assert.throws(() => validateReply(request, stale), /invalid-reply-ownership/);
    if (kind === "accessory_config") for (const field of ["revision", "generation", "page_index"]) {
      const unowned = structuredClone(reply);
      unowned.payload[field] = (unowned.payload[field] as number) + 1;
      assert.deepEqual(decodeEnvelope(encodeEnvelope(unowned).subarray(4)), unowned);
      assert.throws(() => validateReply(request, unowned), /invalid-config-correlation/);
    }
  }
  const request = structuredClone(samples.get("event_sync")!), reply = structuredClone(samples.get("event_sync_ack")!);
  request.payload.epoch = "old-epoch"; request.payload.sequence = 10;
  reply.payload.epoch = "current-epoch"; reply.payload.sequence = 100;
  assert.deepEqual(validateReply(request, reply), reply);
});

test("an expired monotonic deadline is structurally valid and never proves event freshness", () => {
  for (const kind of ["event_state", "camera_event"] as const) {
    const message = structuredClone(fixtures.valid.find(value => value.kind === kind)!);
    message.payload.expires_at_ms = 0;
    assert.ok((message.payload.ttl_ms as number) > 0);
    assert.deepEqual(decodeEnvelope(encodeEnvelope(message).subarray(4)), message);
  }
});
