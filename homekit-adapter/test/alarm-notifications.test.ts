/** Exercise the installed HAP notification path without sockets, publication or devices. */
import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { Bridge, Characteristic, HAPStatus, Service } from "@homebridge/hap-nodejs";
import { HAPConnection } from "@homebridge/hap-nodejs/dist/lib/util/eventedhttp.js";
import { DeviceAlarm } from "../src/device-alarm.js";
import { deviceChild } from "./device-fixtures.js";
import type { DeviceChild } from "../src/device-protocol.js";

type Event = { aid: number; iid: number; value: unknown };
type Packet = { characteristics: Event[] };
type Transport = {
  registeredEvents: Set<string>;
  queuedEvents: Event[];
  handlingRequest: boolean;
  state: number;
  eventsTimer?: ReturnType<typeof setTimeout>;
  sendEvent: HAPConnection["sendEvent"];
  writeQueuedEventNotifications(): void;
};

function fixture(context: TestContext) {
  const packets: Packet[] = [], calls: boolean[] = [];
  // Bypass only construction/socket IO; real Accessory dispatch, subscription,
  // queue, flush and EVENT/1.0 serialization execute from the installed archive.
  const transport = Object.assign(Object.create(HAPConnection.prototype), {
    registeredEvents: new Set(["2.101", "2.102", "2.103", "1.201"]),
    queuedEvents: [], handlingRequest: false, state: 2,
    encrypt: (buffer: Buffer) => buffer,
    tcpSocket: { write(buffer: Buffer, callback: () => void) {
      const [header, body] = buffer.toString().split("\r\n\r\n");
      assert.match(header!, /^EVENT\/1\.0 200 OK/);
      packets.push(JSON.parse(body!)); callback(); return true;
    } },
  }) as Transport;
  context.after(() => { if (transport.eventsTimer) clearTimeout(transport.eventsTimer); });
  const child: DeviceChild = {
    ...deviceChild(), mappings: ["security_system"],
    values: { contact: null, motion: null, leak: null, smoke: null, carbon_monoxide: null },
    alarm: { mode: "away", triggered: false, control_enabled: false },
  };
  const alarm = new DeviceAlarm(child, () => true, async () => {
    throw new Error("No alarm command is allowed in notification fixtures");
  });
  const bridge = new Bridge("Synthetic alarm bridge", "00000000-0000-4000-8000-000000000001");
  bridge.addBridgedAccessory(alarm.accessory);
  alarm.accessory.aid = 2;
  const current = alarm.service.getCharacteristic(Characteristic.SecuritySystemCurrentState);
  const target = alarm.service.getCharacteristic(Characteristic.SecuritySystemTargetState);
  const fault = alarm.service.getCharacteristic(Characteristic.StatusFault);
  current.iid = 101; target.iid = 102; fault.iid = 103;
  const ordinary = bridge.addService(Service.Switch, "Synthetic ordinary status")
    .getCharacteristic(Characteristic.On);
  bridge.aid = 1; ordinary.iid = 201;
  Object.assign(bridge, { _server: { sendEventNotifications(
    aid: number, iid: number, value: boolean | number, _originator: unknown, immediate: boolean,
  ) {
    calls.push(immediate);
    transport.sendEvent(aid, iid, value, immediate);
  } } });
  const states = () => packets.flatMap(packet => [...packet.characteristics].reverse())
    .filter(event => event.aid === 2 && event.iid === 101).map(event => event.value);
  return { child, alarm, current, target, fault, ordinary, transport, packets, calls, states };
}

test("installed HAP sends alarm transitions immediately and unchanged observations stay quiet", context => {
  const f = fixture(context);
  f.child.alarm!.triggered = true; f.alarm.update(f.child);
  assert.deepEqual(f.states(), [4]);
  assert.equal(f.transport.eventsTimer, undefined);
  for (let i = 0; i < 5; i++) f.alarm.update(f.child);
  assert.deepEqual(f.states(), [4]);
  f.child.alarm!.triggered = false; f.alarm.update(f.child);
  f.child.alarm!.triggered = true; f.alarm.update(f.child);
  assert.deepEqual(f.states(), [4, 1, 4]);
  assert.deepEqual(f.calls, [true, true, true]);
});

test("ordinary events retain batching and alarm arrival flushes them without a timer", context => {
  const f = fixture(context);
  f.ordinary.updateValue(true);
  assert.deepEqual(f.calls, [false]);
  assert.equal(f.packets.length, 0);
  assert.ok(f.transport.eventsTimer);
  f.child.alarm!.triggered = true; f.alarm.update(f.child);
  assert.deepEqual(f.calls, [false, true]);
  assert.equal(f.transport.eventsTimer, undefined);
  assert.deepEqual([...f.packets[0]!.characteristics].reverse(), [
    { aid: 1, iid: 201, value: 1 }, { aid: 2, iid: 101, value: 4 },
  ]);
});

test("busy HTTP response retains each distinct alarm transition in Apple processing order", context => {
  const f = fixture(context);
  f.transport.handlingRequest = true;
  for (const triggered of [true, true, false, true, false]) {
    f.child.alarm!.triggered = triggered; f.alarm.update(f.child);
  }
  assert.equal(f.packets.length, 0, "events cannot corrupt a pending HTTP response body");
  assert.equal(f.transport.eventsTimer, undefined);
  assert.deepEqual(f.transport.queuedEvents.map(event => event.value), [4, 1, 4, 1]);
  f.transport.handlingRequest = false;
  f.transport.writeQueuedEventNotifications();
  assert.deepEqual(f.states(), [4, 1, 4, 1]);
  assert.deepEqual(f.packets[0]!.characteristics.map(event => event.value), [1, 4, 1, 4]);
});

test("subscription removal suppresses pending alarm events and does not fabricate delivery", context => {
  const f = fixture(context);
  f.transport.handlingRequest = true;
  f.child.alarm!.triggered = true; f.alarm.update(f.child);
  f.transport.registeredEvents.delete("2.101");
  f.transport.handlingRequest = false; f.transport.writeQueuedEventNotifications();
  assert.deepEqual(f.states(), []);
  f.child.alarm!.triggered = false; f.alarm.update(f.child);
  assert.equal(f.packets.length, 0);
});

test("unknown source remains a fault without inventing an alarm or disarmed event", async context => {
  const f = fixture(context);
  f.child.alarm!.triggered = true; f.alarm.update(f.child);
  f.alarm.invalidate();
  await assert.rejects(f.current.handleGetRequest(), value => value === HAPStatus.SERVICE_COMMUNICATION_FAILURE);
  await assert.rejects(f.target.handleGetRequest(), value => value === HAPStatus.SERVICE_COMMUNICATION_FAILURE);
  assert.equal(await f.fault.handleGetRequest(), 1);
  assert.deepEqual(f.states(), [4]);
  f.alarm.update(f.child);
  assert.equal(await f.current.handleGetRequest(), 4);
  assert.equal(await f.target.handleGetRequest(), 1);
  assert.equal(await f.fault.handleGetRequest(), 0);
  assert.deepEqual(f.states(), [4], "freshness recovery is not a new alarm incident");
});
