import assert from "node:assert/strict";
import { test } from "node:test";
import { setImmediate as tick } from "node:timers/promises";
import {
  Characteristic,
  HAPStatus,
  Perms,
  Service,
} from "@homebridge/hap-nodejs";
import { IdentifierCache } from "@homebridge/hap-nodejs/dist/lib/model/IdentifierCache.js";
import { DeviceLight } from "../src/device-light.js";
import { DeviceCommandRequests } from "../src/device-commands.js";
import {
  validateDeviceEnvelope,
  validateDevicePublication,
  type DeviceChild,
  type DeviceLightRequest,
  type DeviceLightResult,
  type DeviceEnvelope,
} from "../src/device-protocol.js";
import {
  deviceChild,
  deviceHarness,
  deviceProfile,
  devicePublication,
} from "./device-fixtures.js";

function lightChild(
  index = 3,
  enabled = false,
  on: boolean | null = false,
): DeviceChild {
  const child = deviceChild(index);
  return {
    ...child,
    mappings: ["lightbulb"],
    values: Object.fromEntries(
      Object.keys(child.values).map((key) => [key, null]),
    ) as DeviceChild["values"],
    light: { on, control_enabled: enabled },
  };
}
function request(): DeviceLightRequest {
  const p = devicePublication(),
    c = lightChild();
  return {
    scope: p.scope,
    registry_revision: p.registry_revision,
    sequence: p.sequence,
    accessory_uuid: c.accessory_uuid,
    device_id: c.device_id,
    target: false,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

test("light reports real booleans and faults unknown or expired state without actuating", async () => {
  let fresh = true,
    calls = 0;
  const child = lightChild(),
    light = new DeviceLight(
      child,
      () => fresh,
      async () => {
        calls++;
        return true;
      },
    );
  const on = light.service.getCharacteristic(Characteristic.On);
  assert.equal(await on.handleGetRequest(), false);
  const permissions = [...on.props.perms];
  assert.ok(permissions.includes(Perms.WRITE_RESPONSE));
  await assert.rejects(
    on.handleSetRequest(true),
    (value) => value === HAPStatus.READ_ONLY_CHARACTERISTIC,
  );
  child.light!.on = true;
  light.update(child);
  assert.equal(await on.handleGetRequest(), true);
  child.light!.on = null;
  light.update(child);
  await assert.rejects(
    on.handleGetRequest(),
    (value) => value === HAPStatus.SERVICE_COMMUNICATION_FAILURE,
  );
  child.light!.on = false;
  light.update(child);
  fresh = false;
  await assert.rejects(on.handleGetRequest());
  assert.deepEqual(on.props.perms, permissions);
  assert.equal(calls, 0);
});

test("Off returning to automatic preserves a lamp still observed On through the HAP write path", async (context) => {
  const fixture = deviceHarness(undefined, async (payload) => ({
    ...payload,
    accepted: true,
    code: "ok",
    observed_on: true,
  }));
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(
    devicePublication({ children: [lightChild(3, true, true)] }),
  );
  const bridge = fixture.manager.accessory!;
  bridge._assignIDs(new IdentifierCache("02:11:22:33:44:55"));
  const child = bridge.bridgedAccessories[0]!,
    on = child
      .getService(Service.Lightbulb)!
      .getCharacteristic(Characteristic.On);
  const changes: unknown[] = [];
  on.on("change", (change) => changes.push(change.newValue));
  const writer = bridge as unknown as {
    handleCharacteristicWrite(
      connection: object,
      data: object,
      state: number,
    ): Promise<{ status: number; value?: unknown }>;
  };
  for (const r of [true, false]) {
    const result = await writer.handleCharacteristicWrite(
      {},
      { aid: child.aid, iid: on.iid, value: false, r },
      0,
    );
    assert.equal(result.status, HAPStatus.SUCCESS);
    assert.equal(result.value, r ? 1 : undefined);
    assert.equal(on.value, true);
    assert.equal(await on.handleGetRequest(), true);
    await tick();
  }
  assert.ok(changes.length > 0);
  assert.ok(changes.every((value) => value === true));
});

test("false readback remains false despite the pinned HAP response omitting falsy values", async (context) => {
  const fixture = deviceHarness(undefined, async (payload) => ({
    ...payload,
    accepted: true,
    code: "ok",
    observed_on: false,
  }));
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(
    devicePublication({ children: [lightChild(3, true, true)] }),
  );
  const bridge = fixture.manager.accessory!;
  bridge._assignIDs(new IdentifierCache("02:11:22:33:44:55"));
  const child = bridge.bridgedAccessories[0]!,
    on = child
      .getService(Service.Lightbulb)!
      .getCharacteristic(Characteristic.On);
  const writer = bridge as unknown as {
    handleCharacteristicWrite(
      connection: object,
      data: object,
      state: number,
    ): Promise<{ status: number; value?: unknown }>;
  };
  const result = await writer.handleCharacteristicWrite(
    {},
    { aid: child.aid, iid: on.iid, value: false, r: true },
    0,
  );
  assert.equal(result.status, HAPStatus.SUCCESS);
  assert.equal(result.value, undefined); // Existing HAP serialization: data.r && value.
  assert.equal(on.value, false);
  assert.equal(await on.handleGetRequest(), false);
  await tick();
  assert.equal(on.value, false);
});

test("light waits without optimistic state and accepts concurrent readback publication", async (context) => {
  const completion = deferred<DeviceLightResult>();
  let received: DeviceLightRequest | undefined;
  const fixture = deviceHarness(undefined, async (payload) => {
    received = payload;
    return completion.promise;
  });
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(
    devicePublication({ children: [lightChild(3, true, false)] }),
  );
  const on = fixture.manager
    .accessory!.bridgedAccessories[0]!.getService(Service.Lightbulb)!
    .getCharacteristic(Characteristic.On);
  const pending = on.handleSetRequest(true);
  await Promise.resolve();
  assert.equal(on.value, false);
  assert.equal(await on.handleGetRequest(), false);
  await fixture.manager.publish(
    devicePublication({ sequence: 2, children: [lightChild(3, true, true)] }),
  );
  completion.resolve({
    ...received!,
    accepted: true,
    code: "ok",
    observed_on: true,
  });
  await pending;
  assert.equal(on.value, true);
});

test("a repeated pending snapshot does not override fresh successful readback", async (context) => {
  const completion = deferred<DeviceLightResult>();
  const fixture = deviceHarness(undefined, async (payload) => {
    await completion.promise;
    return { ...payload, accepted: true, code: "ok", observed_on: true };
  });
  context.after(() => fixture.manager.close());
  const first = devicePublication({ children: [lightChild(3, true, false)] });
  await fixture.manager.publish(first);
  const on = fixture.manager
    .accessory!.bridgedAccessories[0]!.getService(Service.Lightbulb)!
    .getCharacteristic(Characteristic.On);
  const pending = on.handleSetRequest(true);
  await fixture.manager.publish({ ...first, sequence: 2 });
  completion.resolve({
    ...request(),
    accepted: true,
    code: "ok",
    observed_on: true,
  });
  assert.equal(await pending, true);
  assert.equal(on.value, true);
  assert.equal(await on.handleGetRequest(), true);
});

test("a newer observed Off snapshot wins over an earlier successful On readback", async (context) => {
  const completion = deferred<DeviceLightResult>();
  const fixture = deviceHarness(undefined, async (payload) => {
    await completion.promise;
    return { ...payload, accepted: true, code: "ok", observed_on: true };
  });
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(
    devicePublication({ children: [lightChild(3, true, false)] }),
  );
  const on = fixture.manager
    .accessory!.bridgedAccessories[0]!.getService(Service.Lightbulb)!
    .getCharacteristic(Characteristic.On);
  const changes: unknown[] = [];
  on.on("change", (change) => changes.push(change.newValue));
  const pending = on.handleSetRequest(true);
  await fixture.manager.publish(
    devicePublication({ sequence: 2, children: [lightChild(3, true, true)] }),
  );
  await fixture.manager.publish(
    devicePublication({ sequence: 3, children: [lightChild(3, true, false)] }),
  );
  const beforeReply = changes.length;
  completion.resolve({
    ...request(),
    accepted: true,
    code: "ok",
    observed_on: true,
  });
  assert.equal(await pending, false);
  await tick();
  assert.equal(on.value, false);
  assert.equal(await on.handleGetRequest(), false);
  assert.ok(changes.slice(beforeReply).every((value) => value === false));
});

for (const revoke of ["policy", "expiry", "registry"] as const)
  test(`light confirmation remains revoked after ${revoke} changes`, async (context) => {
    const completion = deferred<DeviceLightResult>();
    const fixture = deviceHarness(undefined, async (payload) => {
      await completion.promise;
      return { ...payload, accepted: true, code: "ok", observed_on: true };
    });
    context.after(() => fixture.manager.close());
    await fixture.manager.publish(
      devicePublication({ children: [lightChild(3, true, false)] }),
    );
    const pending = fixture.manager.setLight(lightChild().device_id, true),
      rejected = assert.rejects(pending);
    await assert.rejects(
      fixture.manager.setLight(lightChild().device_id, false),
    );
    if (revoke === "policy") {
      await fixture.manager.publish(
        devicePublication({
          sequence: 2,
          children: [lightChild(3, false, false)],
        }),
      );
      await fixture.manager.publish(
        devicePublication({
          sequence: 3,
          children: [lightChild(3, true, true)],
        }),
      );
    } else {
      if (revoke === "expiry") fixture.now = 15002;
      await fixture.manager.publish(
        devicePublication({
          sequence: 2,
          registry_revision: revoke === "registry" ? 2 : 1,
          expires_at_ms: 30000,
          children: [lightChild(3, true, true)],
        }),
      );
    }
    completion.resolve({
      ...request(),
      accepted: true,
      code: "ok",
      observed_on: true,
    });
    await rejected;
  });

test("lights preserve graph identifiers and binary-only service shape across refresh/restart", async (context) => {
  const fixture = deviceHarness(),
    again = deviceHarness();
  context.after(() => fixture.manager.close());
  context.after(() => again.manager.close());
  const cache = new IdentifierCache("02:11:22:33:44:55");
  const ids = (manager: typeof fixture.manager) => {
    manager.accessory!._assignIDs(cache);
    return manager.accessory!.bridgedAccessories.map((child) => ({
      uuid: child.UUID,
      aid: child.aid,
      services: child.services.map((s) => ({
        uuid: s.UUID,
        subtype: s.subtype,
        iid: s.iid,
        chars: s.characteristics.map((c) => [c.UUID, c.iid]),
      })),
    }));
  };
  const p = devicePublication({
    children: [lightChild(3, false, false), lightChild(4, true, true)],
  });
  await fixture.manager.publish(p);
  const expected = ids(fixture.manager);
  await fixture.manager.publish({
    ...p,
    sequence: 2,
    children: [
      { ...lightChild(4, false, null), name: "Renamed" },
      lightChild(3, true, true),
    ],
  });
  assert.deepEqual(ids(fixture.manager), expected);
  await again.manager.publish(p);
  assert.deepEqual(ids(again.manager), expected);
  const service = fixture.manager.accessory!.bridgedAccessories[0]!.getService(
    Service.Lightbulb,
  )!;
  assert.equal(service.testCharacteristic(Characteristic.Brightness), false);
  assert.equal(
    fixture.manager.accessory!.bridgedAccessories[0]!.getService(
      Service.MotionSensor,
    ),
    undefined,
  );
});

test("mixed alarm and light RPCs share a four-request limit and require exact typed replies", async () => {
  const sent: DeviceEnvelope[] = [];
  const channel = new DeviceCommandRequests({
    boot: () => deviceProfile,
    send: async (message) => {
      sent.push(message);
    },
    fail: () => {},
  });
  const pending = Array.from({ length: 4 }, (_, i) =>
    i % 2
      ? channel.request("alarm_set", { ...request(), target: "away" })
      : channel.request("light_set", request()),
  );
  const outcomes = Promise.allSettled(pending);
  await assert.rejects(channel.request("light_set", request()), /busy/);
  assert.throws(() =>
    channel.receive({
      ...sent[0]!,
      kind: "alarm_set_result",
      payload: { ...sent[0]!.payload, accepted: true, code: "ok" },
    }),
  );
  const response: DeviceEnvelope = {
    ...sent[0]!,
    kind: "light_set_result",
    payload: {
      ...sent[0]!.payload,
      accepted: true,
      code: "ok",
      observed_on: false,
    },
  };
  validateDeviceEnvelope(response, "python");
  channel.receive(response);
  assert.equal(((await pending[0]) as DeviceLightResult).observed_on, false);
  assert.throws(() => channel.receive(response));
  channel.close();
  await outcomes;
});

test("light protocol rejects guessed states, arbitrary controls, durations and invalid readback", () => {
  const publication = devicePublication({
    children: [lightChild(3, true, true)],
  });
  validateDevicePublication(publication);
  for (const change of [
    { on: 1 },
    { on: "false" },
    { control_enabled: 1 },
    { brightness: 50 },
  ]) {
    const copy = structuredClone(publication);
    Object.assign(copy.children[0]!.light!, change);
    assert.throws(() => validateDevicePublication(copy));
  }
  const message: DeviceEnvelope = {
    v: 1,
    kind: "light_set",
    boot_id: deviceProfile,
    request_id: deviceProfile,
    payload: { ...request() },
  };
  validateDeviceEnvelope(message, "device-hap");
  for (const change of [
    { target: 1 },
    { target: "on" },
    { duration: 60 },
    { target: null },
    { group_id: "fixture" },
  ])
    assert.throws(() =>
      validateDeviceEnvelope({
        ...message,
        payload: { ...message.payload, ...change },
      }),
    );
  for (const change of [
    { accepted: true, code: "ok", observed_on: null },
    { accepted: true, code: "ok", observed_on: 1 },
    { accepted: false, code: "uncertain", observed_on: false },
  ])
    assert.throws(() =>
      validateDeviceEnvelope({
        ...message,
        kind: "light_set_result",
        payload: { ...message.payload, ...change },
      }),
    );
});
