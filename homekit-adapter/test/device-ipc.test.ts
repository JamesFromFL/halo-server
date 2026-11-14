import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import type { Socket } from "node:net";
import { test } from "node:test";
import { DeviceControlChannel } from "../src/device-ipc.js";
import {
  encodeDeviceEnvelope,
  type DeviceEnvelope,
} from "../src/device-protocol.js";

const message: DeviceEnvelope = {
  v: 1,
  kind: "ping",
  boot_id: "00000000-0000-0000-0000-000000000001",
  request_id: "00000000-0000-0000-0000-000000000002",
  payload: {},
};

class FakeSocket extends EventEmitter {
  writes: Buffer[] = [];
  callbacks: ((error?: Error | null) => void)[] = [];
  destroyed = false;
  write(frame: Buffer, callback: (error?: Error | null) => void): boolean {
    this.writes.push(frame);
    this.callbacks.push(callback);
    return false;
  }
  destroy(): this {
    this.destroyed = true;
    this.emit("close");
    return this;
  }
  asSocket(): Socket {
    return this as unknown as Socket;
  }
}

test("writer serializes frozen frames and bounds queued writes", async () => {
  const socket = new FakeSocket();
  const channel = new DeviceControlChannel(socket.asSocket(), {
    maximumPendingWrites: 2,
  });
  const mutable = structuredClone(message);
  const first = channel.send(mutable);
  mutable.request_id = "00000000-0000-0000-0000-000000000003";
  const second = channel.send(mutable);
  await assert.rejects(channel.send(message), /write-capacity/);
  assert.equal(socket.writes.length, 1);
  assert.deepEqual(socket.writes[0], encodeDeviceEnvelope(message));
  socket.callbacks.shift()!();
  await first;
  assert.equal(socket.writes.length, 2);
  socket.callbacks.shift()!();
  await second;
  assert.equal(channel.pendingBytes, 0);
  assert.equal(channel.pendingWrites, 0);
  channel.close();
});

test("writer byte capacity rejects before allocating socket output", async () => {
  const socket = new FakeSocket();
  const channel = new DeviceControlChannel(socket.asSocket(), {
    maximumPendingBytes: 10,
  });
  await assert.rejects(channel.send(message), /write-capacity/);
  assert.equal(socket.writes.length, 0);
  channel.close();
});

test("stalled write closes channel and retires all pending promises", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const socket = new FakeSocket();
  const channel = new DeviceControlChannel(socket.asSocket(), {
    ioTimeoutMs: 20,
  });
  const first = assert.rejects(channel.send(message), /write-timeout/);
  const second = assert.rejects(channel.send(message), /write-timeout/);
  context.mock.timers.tick(21);
  await Promise.all([first, second]);
  assert.equal(channel.isClosed, true);
  assert.equal(channel.pendingBytes, 0);
  assert.equal(channel.pendingWrites, 0);
  assert.equal(socket.destroyed, true);
});

test("partial frame deadline is not extended by individual bytes", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const socket = new FakeSocket();
  const channel = new DeviceControlChannel(socket.asSocket(), {
    ioTimeoutMs: 20,
  });
  const errors: Error[] = [];
  channel.on("failure", (error) => errors.push(error));
  socket.emit("data", Buffer.from([0]));
  context.mock.timers.tick(10);
  socket.emit("data", Buffer.from([0]));
  context.mock.timers.tick(11);
  assert.equal(channel.isClosed, true);
  assert.match(errors[0]!.message, /partial-frame-timeout/);
});

test("completed frame starts a new partial deadline without retaining stale input", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const socket = new FakeSocket();
  const channel = new DeviceControlChannel(socket.asSocket(), {
    ioTimeoutMs: 20,
  });
  const frames: DeviceEnvelope[] = [];
  channel.on("message", (message) => frames.push(message));
  const encoded = encodeDeviceEnvelope(message);
  socket.emit("data", encoded.subarray(0, 2));
  context.mock.timers.tick(15);
  socket.emit(
    "data",
    Buffer.concat([encoded.subarray(2), encoded.subarray(0, 2)]),
  );
  context.mock.timers.tick(10);
  assert.equal(channel.isClosed, false);
  socket.emit("data", encoded.subarray(2));
  context.mock.timers.tick(30);
  assert.equal(channel.isClosed, false);
  assert.deepEqual(frames, [message, message]);
  channel.close();
});

test("EOF during frame and I/O failure retire queued writes", async () => {
  const socket = new FakeSocket();
  const channel = new DeviceControlChannel(socket.asSocket());
  const pending = assert.rejects(channel.send(message), /truncated-frame/);
  socket.emit("data", Buffer.from([0]));
  socket.emit("end");
  await pending;
  assert.equal(channel.isClosed, true);
});
