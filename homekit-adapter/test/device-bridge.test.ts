import assert from "node:assert/strict";
import { test } from "node:test";
import {
  AccessoryEventTypes,
  Characteristic,
  HAPStatus,
  Service,
  type Bridge,
} from "@homebridge/hap-nodejs";
import { IdentifierCache } from "@homebridge/hap-nodejs/dist/lib/model/IdentifierCache.js";
import { DeviceSensor } from "../src/device-bridge.js";
import { DEVICE_MAPPINGS } from "../src/device-protocol.js";
import {
  deviceChild,
  deviceHarness,
  devicePublication,
} from "./device-fixtures.js";
const primary = {
  contact: [Service.ContactSensor, Characteristic.ContactSensorState],
  motion: [Service.MotionSensor, Characteristic.MotionDetected],
  leak: [Service.LeakSensor, Characteristic.LeakDetected],
  smoke: [Service.SmokeSensor, Characteristic.SmokeDetected],
  carbon_monoxide: [
    Service.CarbonMonoxideSensor,
    Characteristic.CarbonMonoxideDetected,
  ],
} as const;

for (const mapping of DEVICE_MAPPINGS)
  test(`device ${mapping} delivers true/false, faults unknown and denies writes`, async () => {
    let fresh = true;
    const value = deviceChild(3, [mapping]),
      sensor = new DeviceSensor(value, () => fresh),
      [serviceType, characteristicType] = primary[mapping];
    const service = sensor.accessory.getService(serviceType)!,
      reading = service.getCharacteristic(characteristicType),
      fault = service.getCharacteristic(Characteristic.StatusFault);
    assert.equal(
      await reading.handleGetRequest(),
      mapping === "motion" ? false : 0,
    );
    assert.equal(await fault.handleGetRequest(), 0);
    assert.ok(!reading.props.perms.includes("pw" as never));
    value.values[mapping] = true;
    sensor.update(value);
    assert.equal(
      await reading.handleGetRequest(),
      mapping === "motion" ? true : 1,
    );
    value.values[mapping] = null;
    sensor.update(value);
    assert.equal(reading.statusCode, HAPStatus.SERVICE_COMMUNICATION_FAILURE);
    await assert.rejects(
      reading.handleGetRequest(),
      (error) => error === HAPStatus.SERVICE_COMMUNICATION_FAILURE,
    );
    assert.equal(await fault.handleGetRequest(), 1);
    value.values[mapping] = true;
    sensor.update(value);
    fresh = false;
    await assert.rejects(
      reading.handleGetRequest(),
      (error) => error === HAPStatus.SERVICE_COMMUNICATION_FAILURE,
    );
    assert.equal(await fault.handleGetRequest(), 1);
  });
test("bridge publishes once, adds/removes sensors explicitly and preserves selected unavailable children", async (context) => {
  const fixture = deviceHarness();
  context.after(() => fixture.manager.close());
  const first = devicePublication({
    children: [deviceChild(3), deviceChild(4, ["motion"])],
  });
  await fixture.manager.publish(first);
  assert.equal(fixture.manager.count, 2);
  assert.equal(fixture.manager.published, true);
  assert.equal(fixture.publisher.published.length, 1);
  const missing = structuredClone(first);
  missing.sequence++;
  missing.children[0]!.available = false;
  missing.children[0]!.values.contact = null;
  await fixture.manager.publish(missing);
  assert.equal(fixture.manager.count, 2);
  const selected = {
    ...missing,
    registry_revision: 2,
    sequence: 3,
    children: [deviceChild(4, ["motion"]), deviceChild(5, ["smoke"])],
  };
  await fixture.manager.publish(selected);
  assert.equal(fixture.manager.count, 2);
  assert.equal(fixture.publisher.published.length, 1);
  assert.equal(fixture.publisher.unpublished.length, 0);
  assert.deepEqual(
    fixture.manager.accessory!.bridgedAccessories.map((child) => child.UUID),
    selected.children.map((child) => child.accessory_uuid),
  );
});
test("bridge rejects stale account, identity, revision, replay and unversioned graph changes atomically", async (context) => {
  const fixture = deviceHarness();
  context.after(() => fixture.manager.close());
  const first = devicePublication();
  await fixture.manager.publish(first);
  for (const change of [
    { scope: { ...first.scope, account_generation: 2 } },
    {
      scope: {
        ...first.scope,
        profile_id: "00000000-0000-4000-8000-000000000009",
      },
    },
    { bridge: { ...first.bridge, username: "02:11:22:33:44:56" } },
    { registry_revision: 0 },
    { sequence: 1 },
    { expires_at_ms: 1 },
    { children: [] },
    {
      registry_revision: 2,
      children: [
        {
          ...first.children[0]!,
          accessory_uuid: "00000000-0000-4000-8000-000000000099",
        },
      ],
    },
    { registry_revision: 2, children: [deviceChild(3, ["motion"])] },
  ])
    await assert.rejects(
      fixture.manager.publish({ ...first, sequence: 2, ...change }),
    );
  assert.equal(fixture.manager.count, 1);
  assert.equal(fixture.manager.published, true);
  assert.equal(fixture.publisher.published.length, 1);
});
test("149 is the strict bridge limit; a rejected replacement does not change the live graph", async (context) => {
  const fixture = deviceHarness();
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(devicePublication());
  await assert.rejects(
    fixture.manager.publish(
      devicePublication({
        registry_revision: 2,
        sequence: 2,
        children: Array.from({ length: 150 }, (_, i) => deviceChild(i + 3)),
      }),
    ),
    /device-capacity/,
  );
  assert.equal(fixture.manager.count, 1);
  await fixture.manager.publish(
    devicePublication({
      registry_revision: 2,
      sequence: 2,
      children: Array.from({ length: 149 }, (_, i) => deviceChild(i + 3)),
    }),
  );
  assert.equal(fixture.manager.count, 149);
});
test("sensor IDs remain stable across reorder, rename, removal/reinclude and reconstructed process graph", async (context) => {
  const fixture = deviceHarness();
  context.after(() => fixture.manager.close());
  const cache = new IdentifierCache("02:11:22:33:44:55");
  const ids = (bridge: Bridge) => {
    bridge._assignIDs(cache);
    return Object.fromEntries(
      bridge.bridgedAccessories.map((child) => [
        child.UUID,
        {
          aid: child.aid,
          services: child.services.map((service) => ({
            uuid: service.UUID,
            subtype: service.subtype,
            iid: service.iid,
            characteristics: service.characteristics.map((c) => [
              c.UUID,
              c.iid,
            ]),
          })),
        },
      ]),
    );
  };
  const first = devicePublication({
    children: [deviceChild(3), deviceChild(4, ["carbon_monoxide", "smoke"])],
  });
  await fixture.manager.publish(first);
  const expected = ids(fixture.manager.accessory!);
  await fixture.manager.publish({
    ...first,
    sequence: 2,
    children: [
      { ...first.children[1]!, name: "Renamed Hall" },
      first.children[0]!,
    ],
  });
  assert.deepEqual(ids(fixture.manager.accessory!), expected);
  await fixture.manager.publish({
    ...first,
    registry_revision: 2,
    sequence: 3,
    children: [],
  });
  ids(fixture.manager.accessory!);
  await fixture.manager.publish({
    ...first,
    registry_revision: 3,
    sequence: 4,
  });
  assert.deepEqual(ids(fixture.manager.accessory!), expected);
  const restarted = deviceHarness();
  context.after(() => restarted.manager.close());
  await restarted.manager.publish({
    ...first,
    registry_revision: 3,
    children: [...first.children].reverse(),
  });
  assert.deepEqual(ids(restarted.manager.accessory!), expected);
});
test("expiry faults values and reads enforce the monotonic deadline before an event loop timer fires", async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const fixture = deviceHarness();
  context.after(() => fixture.manager.close());
  await fixture.manager.publish(devicePublication({ expires_at_ms: 100 }));
  const sensor = fixture.manager.accessory!.bridgedAccessories[0]!,
    reading = sensor
      .getService(Service.ContactSensor)!
      .getCharacteristic(Characteristic.ContactSensorState);
  fixture.now = 100;
  await assert.rejects(
    reading.handleGetRequest(),
    (error) => error === HAPStatus.SERVICE_COMMUNICATION_FAILURE,
  );
  context.mock.timers.tick(100);
  assert.equal(reading.statusCode, HAPStatus.SERVICE_COMMUNICATION_FAILURE);
  await fixture.manager.publish(
    devicePublication({ sequence: 2, expires_at_ms: 200 }),
  );
  assert.equal(await reading.handleGetRequest(), 0);
});
test("publication requires actual listening and advertised events; identify has no actuation callback", async (context) => {
  const fixture = deviceHarness();
  context.after(() => fixture.manager.close());
  fixture.publisher.events = false;
  await fixture.manager.publish(devicePublication());
  assert.equal(fixture.manager.published, false);
  fixture.manager.accessory!.emit(
    AccessoryEventTypes.LISTENING,
    12345,
    "fixture",
  );
  assert.equal(fixture.manager.published, false);
  fixture.manager.accessory!.emit(AccessoryEventTypes.ADVERTISED);
  assert.equal(fixture.manager.published, true);
  let acknowledgements = 0;
  fixture.manager.accessory!.emit(
    AccessoryEventTypes.IDENTIFY,
    true,
    () => acknowledgements++,
  );
  fixture.manager.accessory!.bridgedAccessories[0]!.emit(
    AccessoryEventTypes.IDENTIFY,
    true,
    () => acknowledgements++,
  );
  assert.equal(acknowledgements, 2);
});
test("publication failure and parent fencing invalidate values; close is idempotent and owns claimed cleanup", async () => {
  const fixture = deviceHarness();
  fixture.publisher.fail = true;
  await assert.rejects(
    fixture.manager.publish(devicePublication()),
    /fixture failure/,
  );
  assert.equal(fixture.manager.published, false);
  await assert.rejects(
    fixture.manager.publish(devicePublication({ sequence: 2 })),
    /not-ready/,
  );
  const reading = fixture.manager
    .accessory!.bridgedAccessories[0]!.getService(Service.ContactSensor)!
    .getCharacteristic(Characteristic.ContactSensorState);
  await assert.rejects(
    reading.handleGetRequest(),
    (error) => error === HAPStatus.SERVICE_COMMUNICATION_FAILURE,
  );
  const close = fixture.manager.close();
  assert.equal(fixture.manager.close(), close);
  await close;
  assert.equal(fixture.closes, 1);
  assert.equal(fixture.publisher.unpublished.length, 1);
});
