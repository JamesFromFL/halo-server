import assert from "node:assert/strict";
import { test } from "node:test";
import { setImmediate as tick } from "node:timers/promises";
import { Characteristic, HAPStatus, Service } from "@homebridge/hap-nodejs";
import { IdentifierCache } from "@homebridge/hap-nodejs/dist/lib/model/IdentifierCache.js";
import { DeviceKeypad } from "../src/device-keypad.js";
import {
  validateDevicePublication,
  validateDeviceKeypad,
  type DeviceChild,
  type DeviceKeypadRequest,
  type DeviceKeypadResult,
} from "../src/device-protocol.js";
import {
  deviceChild,
  deviceHarness,
  devicePublication,
} from "./device-fixtures.js";
function keypad(brightness: number | null = 20, locked = false): DeviceChild {
  return {
    ...deviceChild(4, []),
    mappings: ["keypad_light"],
    keypad: {
      brightness,
      locked,
      control_enabled: true,
      on_brightness: brightness || 60,
    },
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

test("keypad protocol separates observed percentage, local lock and qualified motion", () => {
  const child = keypad();
  validateDevicePublication(devicePublication({ children: [child] }));
  for (const brightness of [true, -1, 101, 12.5, "50"])
    assert.throws(() =>
      validateDevicePublication(
        devicePublication({
          children: [
            {
              ...child,
              keypad: { ...child.keypad!, brightness: brightness as number },
            },
          ],
        }),
      ),
    );
  for (const mappings of [
    ["keypad_light", "lightbulb"],
    ["contact", "keypad_light"],
  ])
    assert.throws(() =>
      validateDevicePublication(
        devicePublication({
          children: [
            { ...child, mappings: mappings as DeviceChild["mappings"] },
          ],
        }),
      ),
    );
  const p = devicePublication(),
    base = {
      scope: p.scope,
      registry_revision: 1,
      sequence: 1,
      accessory_uuid: child.accessory_uuid,
      device_id: child.device_id,
    };
  for (const target of [0, 1, 100]) validateDeviceKeypad({ ...base, target });
  for (const target of [true, -1, 101, 1.5, "50"])
    assert.throws(() => validateDeviceKeypad({ ...base, target }));
  validateDeviceKeypad({ ...base, target: true }, false, true);
  assert.throws(() =>
    validateDeviceKeypad({ ...base, target: 1 }, false, true),
  );
  assert.throws(() =>
    validateDeviceKeypad(
      {
        ...base,
        target: 10,
        accepted: true,
        code: "ok",
        observed_brightness: null,
      },
      true,
    ),
  );
});
for (const initial of [0, 20])
  for (const response of [true, false])
    test(`real HAP combined On/Brightness; initial=${initial}, response=${response}`, async (context) => {
      const targets: number[] = [];
      const fixture = deviceHarness(undefined, undefined, async (request) => {
        targets.push(request.target);
        return {
          ...request,
          accepted: true,
          code: "ok",
          observed_brightness: 37,
        };
      });
      context.after(() => fixture.manager.close());
      await fixture.manager.publish(
        devicePublication({ children: [keypad(initial)] }),
      );
      const bridge = fixture.manager.accessory!;
      bridge._assignIDs(new IdentifierCache("02:11:22:33:44:55"));
      const child = bridge.bridgedAccessories[0]!,
        service = child.getService(Service.Lightbulb)!;
      const on = service.getCharacteristic(Characteristic.On),
        brightness = service.getCharacteristic(Characteristic.Brightness);
      const writer = bridge as unknown as {
        handleCharacteristicWrite(
          connection: object,
          data: object,
          state: number,
        ): Promise<{ status: number; value?: unknown }>;
      };
      const results = await Promise.all(
        [on, brightness].map((c, i) =>
          writer.handleCharacteristicWrite(
            {},
            { aid: child.aid, iid: c.iid, value: i ? 40 : true, r: response },
            0,
          ),
        ),
      );
      assert.deepEqual(targets, [40]);
      assert.ok(results.every((r) => r.status === HAPStatus.SUCCESS));
      assert.equal(on.value, true);
      assert.equal(brightness.value, 37);
      assert.equal(await brightness.handleGetRequest(), 37);
    });
test("On alone uses prior nonzero brightness; zero readback stays zero", async () => {
  const targets: number[] = [];
  const instance = new DeviceKeypad(
    keypad(0),
    () => true,
    async (target) => {
      targets.push(target);
      return 0;
    },
    async (v) => v,
  );
  const on = instance.light.getCharacteristic(Characteristic.On);
  assert.equal(await on.handleSetRequest(true), false);
  assert.deepEqual(targets, [60]);
  assert.equal(on.value, false);
  assert.equal(
    instance.light.getCharacteristic(Characteristic.Brightness).value,
    0,
  );
});
test("locked writes reject even idempotent On and retain observed brightness", async () => {
  let calls = 0;
  const instance = new DeviceKeypad(
    keypad(20, true),
    () => true,
    async (value) => {
      calls++;
      return value;
    },
    async (v) => v,
  );
  await assert.rejects(
    instance.light.getCharacteristic(Characteristic.On).handleSetRequest(true),
    (e) => e === HAPStatus.READ_ONLY_CHARACTERISTIC,
  );
  await assert.rejects(
    instance.light
      .getCharacteristic(Characteristic.Brightness)
      .handleSetRequest(20),
    (e) => e === HAPStatus.READ_ONLY_CHARACTERISTIC,
  );
  assert.equal(calls, 0);
  assert.equal(
    await instance.light
      .getCharacteristic(Characteristic.Brightness)
      .handleGetRequest(),
    20,
  );
});
test("separate lock lane persists through own publication and revokes pending dimmer", async (context) => {
  const pending = deferred<DeviceKeypadResult>();
  let request: DeviceKeypadRequest | undefined;
  const child = keypad();
  let sequence = 1;
  const fixture = deviceHarness(
    undefined,
    undefined,
    async (value) => {
      request = value;
      return pending.promise;
    },
    async (value) => {
      child.keypad!.locked = value.target;
      await fixture.manager.publish(
        devicePublication({ sequence: ++sequence, children: [child] }),
      );
      return {
        ...value,
        accepted: true,
        code: "ok",
        observed_locked: value.target,
      };
    },
  );
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(devicePublication({ children: [child] }));
  const accessory = fixture.manager.accessory!.bridgedAccessories[0]!,
    light = accessory.getService(Service.Lightbulb)!;
  const rejected = assert.rejects(
    light.getCharacteristic(Characteristic.Brightness).handleSetRequest(70),
  );
  await tick();
  assert.ok(request);
  const lock = accessory
    .getServiceById(Service.Switch, "halo:keypad_brightness_lock")!
    .getCharacteristic(Characteristic.On);
  assert.equal(await lock.handleSetRequest(true), true);
  assert.equal(await lock.handleGetRequest(), true);
  pending.resolve({
    ...request,
    accepted: true,
    code: "ok",
    observed_brightness: 70,
  });
  await rejected;
  assert.equal(
    await light.getCharacteristic(Characteristic.Brightness).handleGetRequest(),
    20,
  );
});
test("locking revokes a coalesced batch before any dimmer dispatch", async () => {
  let writes = 0;
  const instance = new DeviceKeypad(
    keypad(0),
    () => true,
    async (v) => {
      writes++;
      return v;
    },
    async (v) => v,
  );
  const rejected = assert.rejects(
    instance.light
      .getCharacteristic(Characteristic.Brightness)
      .handleSetRequest(50),
  );
  assert.equal(
    await instance.lock
      .getCharacteristic(Characteristic.On)
      .handleSetRequest(true),
    true,
  );
  await rejected;
  assert.equal(writes, 0);
});
test("offline local lock and additive motion retain accessory identity and unknown faults", async (context) => {
  const child = keypad(null);
  child.available = false;
  const fixture = deviceHarness(
    undefined,
    undefined,
    undefined,
    async (request) => ({
      ...request,
      accepted: true,
      code: "ok",
      observed_locked: request.target,
    }),
  );
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(devicePublication({ children: [child] }));
  const accessory = fixture.manager.accessory!.bridgedAccessories[0]!;
  assert.equal(accessory.getService(Service.MotionSensor), undefined);
  assert.equal(
    await accessory
      .getService(Service.Switch)!
      .getCharacteristic(Characteristic.On)
      .handleSetRequest(true),
    true,
  );
  await assert.rejects(
    accessory
      .getService(Service.Lightbulb)!
      .getCharacteristic(Characteristic.Brightness)
      .handleGetRequest(),
  );
  child.available = true;
  child.keypad!.brightness = 20;
  child.mappings = ["keypad_light", "motion"];
  child.values.motion = true;
  await fixture.manager.publish(
    devicePublication({ registry_revision: 2, sequence: 2, children: [child] }),
  );
  assert.equal(fixture.manager.accessory!.bridgedAccessories[0], accessory);
  const motion = accessory
    .getService(Service.MotionSensor)!
    .getCharacteristic(Characteristic.MotionDetected);
  assert.equal(await motion.handleGetRequest(), true);
  child.values.motion = null;
  await fixture.manager.publish(
    devicePublication({ registry_revision: 2, sequence: 3, children: [child] }),
  );
  await assert.rejects(motion.handleGetRequest());
});
