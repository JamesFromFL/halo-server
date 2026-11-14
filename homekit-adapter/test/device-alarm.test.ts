import assert from "node:assert/strict";
import { test } from "node:test";
import {
  Characteristic,
  HAPStatus,
  Perms,
  Service,
} from "@homebridge/hap-nodejs";
import { DeviceAlarm, DeviceAlarmRequests } from "../src/device-alarm.js";
import {
  validateDeviceEnvelope,
  validateDevicePublication,
  type DeviceAlarmRequest,
  type DeviceAlarmResult,
  type DeviceChild,
  type DeviceEnvelope,
} from "../src/device-protocol.js";
import {
  deviceChild,
  deviceHarness,
  deviceProfile,
  devicePublication,
} from "./device-fixtures.js";
const alarmChild = (enabled = false): DeviceChild => ({
  ...deviceChild(),
  mappings: ["security_system"],
  values: {
    contact: null,
    motion: null,
    leak: null,
    smoke: null,
    carbon_monoxide: null,
  },
  alarm: { mode: "disarmed", triggered: false, control_enabled: enabled },
});
const request = (): DeviceAlarmRequest => ({
  scope: { profile_id: deviceProfile, account_generation: 1 },
  registry_revision: 1,
  sequence: 1,
  accessory_uuid: deviceChild().accessory_uuid,
  device_id: deviceChild().device_id,
  target: "away",
});
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

test("alarm current state follows real mode and triggered observations, preserving unknown", async () => {
  const child = alarmChild(),
    alarm = new DeviceAlarm(
      child,
      () => true,
      async () => {
        throw new Error("forbidden");
      },
    );
  const current = alarm.service.getCharacteristic(
      Characteristic.SecuritySystemCurrentState,
    ),
    target = alarm.service.getCharacteristic(
      Characteristic.SecuritySystemTargetState,
    );
  for (const [mode, value] of [
    ["disarmed", 3],
    ["home", 0],
    ["away", 1],
  ] as const) {
    child.alarm!.mode = mode;
    alarm.update(child);
    assert.equal(await current.handleGetRequest(), value);
    assert.equal(await target.handleGetRequest(), value);
  }
  child.alarm!.triggered = true;
  alarm.update(child);
  assert.equal(await current.handleGetRequest(), 4);
  assert.equal(await target.handleGetRequest(), 1);
  child.alarm!.triggered = null;
  alarm.update(child);
  await assert.rejects(
    current.handleGetRequest(),
    (value) => value === HAPStatus.SERVICE_COMMUNICATION_FAILURE,
  );
  assert.equal(
    await alarm.service
      .getCharacteristic(Characteristic.StatusFault)
      .handleGetRequest(),
    1,
  );
  child.alarm!.mode = null;
  alarm.update(child);
  await assert.rejects(
    target.handleGetRequest(),
    (value) => value === HAPStatus.SERVICE_COMMUNICATION_FAILURE,
  );
});
test("alarm has stable standard permissions while policy blocks writes by default", async () => {
  let calls = 0;
  const child = alarmChild(),
    alarm = new DeviceAlarm(
      child,
      () => true,
      async () => {
        calls++;
      },
    );
  const target = alarm.service.getCharacteristic(
    Characteristic.SecuritySystemTargetState,
  );
  assert.deepEqual(target.props.validValues, [0, 1, 3]);
  const permissions = [...target.props.perms];
  assert.ok(permissions.includes(Perms.PAIRED_WRITE));
  await assert.rejects(
    target.handleSetRequest(1),
    (value) => value === HAPStatus.READ_ONLY_CHARACTERISTIC,
  );
  assert.equal(calls, 0);
  child.alarm!.control_enabled = true;
  alarm.update(child);
  assert.deepEqual(target.props.perms, permissions);
  await target.handleSetRequest(1);
  assert.equal(calls, 1);
  for (const invalid of [2, 4, "panic", "bypass"])
    await assert.rejects(target.handleSetRequest(invalid));
  assert.equal(calls, 1);
  alarm.invalidate();
  assert.deepEqual(target.props.perms, permissions);
  await assert.rejects(target.handleSetRequest(3));
  assert.equal(calls, 1);
});
test("alarm waits for confirmation without optimistic current or target updates", async (context) => {
  const completion = deferred<DeviceAlarmResult>();
  let received: DeviceAlarmRequest | null = null;
  const fixture = deviceHarness(async (payload) => {
    received = payload;
    return completion.promise;
  });
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(
    devicePublication({ children: [alarmChild(true)] }),
  );
  const service = fixture.manager.accessory!.bridgedAccessories[0]!.getService(
      Service.SecuritySystem,
    )!,
    target = service.getCharacteristic(
      Characteristic.SecuritySystemTargetState,
    ),
    current = service.getCharacteristic(
      Characteristic.SecuritySystemCurrentState,
    );
  const pending = target.handleSetRequest(1);
  await Promise.resolve();
  assert.equal(target.value, 3);
  assert.equal(current.value, 3);
  assert.ok(received);
  const observed = alarmChild(true);
  observed.alarm!.mode = "away";
  await fixture.manager.publish(
    devicePublication({ sequence: 2, children: [observed] }),
  );
  completion.resolve({
    ...(received as unknown as DeviceAlarmRequest),
    accepted: true,
    code: "ok",
  });
  await pending;
  assert.equal(await current.handleGetRequest(), 1);
  assert.equal(await target.handleGetRequest(), 1);
});
test("one alarm command per device and a revocation cannot be undone while confirmation waits", async (context) => {
  const completion = deferred<DeviceAlarmResult>();
  const fixture = deviceHarness(async (payload) => {
    await completion.promise;
    return { ...payload, accepted: true, code: "ok" };
  });
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(
    devicePublication({ children: [alarmChild(true)] }),
  );
  const first = fixture.manager.setAlarm(deviceChild().device_id, "home"),
    rejected = assert.rejects(first);
  await assert.rejects(
    fixture.manager.setAlarm(deviceChild().device_id, "away"),
  );
  await fixture.manager.publish(
    devicePublication({ sequence: 2, children: [alarmChild(false)] }),
  );
  await fixture.manager.publish(
    devicePublication({ sequence: 3, children: [alarmChild(true)] }),
  );
  completion.resolve({ ...request(), accepted: true, code: "ok" });
  await rejected;
});
test("unconfirmed commands never change observed current state and expiry prevents sends", async (context) => {
  let calls = 0;
  const fixture = deviceHarness(async (payload) => {
    calls++;
    return { ...payload, accepted: false, code: "uncertain" };
  });
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(
    devicePublication({ children: [alarmChild(true)] }),
  );
  await assert.rejects(
    fixture.manager.setAlarm(deviceChild().device_id, "away"),
  );
  assert.equal(calls, 1);
  const current = fixture.manager
    .accessory!.bridgedAccessories[0]!.getService(Service.SecuritySystem)!
    .getCharacteristic(Characteristic.SecuritySystemCurrentState);
  assert.equal(await current.handleGetRequest(), 3);
  fixture.now = 15001;
  await assert.rejects(
    fixture.manager.setAlarm(deviceChild().device_id, "away"),
  );
  assert.equal(calls, 1);
});
test("a fresh snapshot cannot revive a command whose prior observation expired", async (context) => {
  const completion = deferred<DeviceAlarmResult>();
  const fixture = deviceHarness(async (payload) => {
    await completion.promise;
    return { ...payload, accepted: true, code: "ok" };
  });
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(
    devicePublication({ children: [alarmChild(true)] }),
  );
  const first = fixture.manager.setAlarm(deviceChild().device_id, "home"),
    rejected = assert.rejects(first);
  fixture.now = 15001;
  await fixture.manager.publish(
    devicePublication({
      sequence: 2,
      expires_at_ms: 30000,
      children: [alarmChild(true)],
    }),
  );
  completion.resolve({ ...request(), accepted: true, code: "ok" });
  await rejected;
});
test("alarm request channel matches the full authority tuple and bounds concurrent requests", async () => {
  const sent: DeviceEnvelope[] = [],
    failures: string[] = [];
  const channel = new DeviceAlarmRequests({
    boot: () => deviceProfile,
    send: async (message) => {
      sent.push(message);
    },
    fail: (code) => failures.push(code),
  });
  const pending = channel.request(request());
  assert.equal(sent.length, 1);
  assert.equal(sent[0]!.kind, "alarm_set");
  validateDeviceEnvelope(sent[0], "device-hap");
  const response: DeviceEnvelope = {
    ...sent[0]!,
    kind: "alarm_set_result",
    payload: { ...request(), accepted: true, code: "ok" },
  };
  for (const changed of [
    { target: "home" },
    { sequence: 2 },
    { registry_revision: 2 },
    { device_id: "f".repeat(64) },
    { scope: { profile_id: deviceProfile, account_generation: 2 } },
  ])
    assert.throws(() =>
      channel.receive({
        ...response,
        payload: { ...response.payload, ...changed },
      }),
    );
  channel.receive(response);
  assert.equal((await pending).accepted, true);
  assert.throws(() => channel.receive(response));
  assert.deepEqual(failures, []);
  const queue = Array.from({ length: 4 }, () => channel.request(request()));
  const rejects = queue.map((value) => assert.rejects(value));
  await assert.rejects(channel.request(request()), /busy/);
  channel.close();
  await Promise.all(rejects);
});
test("alarm timeout closes admission rather than retrying or accepting a late response", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let sent: DeviceEnvelope | undefined;
  const failures: string[] = [];
  const channel = new DeviceAlarmRequests({
    boot: () => deviceProfile,
    send: async (message) => {
      sent = message;
    },
    fail: (code) => failures.push(code),
    timeoutMs: 10,
  });
  const pending = assert.rejects(channel.request(request()));
  context.mock.timers.tick(11);
  await pending;
  assert.deepEqual(failures, ["alarm-command-timeout"]);
  assert.throws(() =>
    channel.receive({
      ...sent!,
      kind: "alarm_set_result",
      payload: { ...request(), accepted: true, code: "ok" },
    }),
  );
  await assert.rejects(channel.request(request()));
});
test("strict alarm schema exposes no arbitrary action or ambiguous unavailable state", () => {
  const snapshot = devicePublication({ children: [alarmChild(true)] });
  assert.doesNotThrow(() => validateDevicePublication(snapshot));
  for (const changed of [
    { mode: "night" },
    { mode: "panic" },
    { mode: [] },
    { control_enabled: 1 },
    { bypass: true },
  ])
    assert.throws(() =>
      validateDevicePublication({
        ...snapshot,
        children: [
          {
            ...alarmChild(true),
            alarm: { ...alarmChild(true).alarm!, ...changed },
          },
        ],
      }),
    );
  assert.throws(() =>
    validateDevicePublication({
      ...snapshot,
      children: [
        { ...alarmChild(true), mappings: ["contact", "security_system"] },
      ],
    }),
  );
  assert.throws(() =>
    validateDevicePublication({
      ...snapshot,
      children: [{ ...alarmChild(true), available: false }],
    }),
  );
  for (const target of ["night", "panic", "bypass", "arm_away", null])
    assert.throws(() =>
      validateDeviceEnvelope(
        {
          v: 1,
          kind: "alarm_set",
          boot_id: deviceProfile,
          request_id: deviceProfile,
          payload: { ...request(), target },
        },
        "device-hap",
      ),
    );
  assert.throws(() =>
    validateDeviceEnvelope(
      {
        v: 1,
        kind: "alarm_set",
        boot_id: deviceProfile,
        request_id: deviceProfile,
        payload: request(),
      },
      "python",
    ),
  );
});
