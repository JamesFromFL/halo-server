import { AccessoryEventTypes, type Bridge } from "@homebridge/hap-nodejs";
import {
  DeviceBridgeManager,
  type DevicePublisher,
  type DeviceBridgeOptions,
} from "../src/device-bridge.js";
import type {
  DeviceChild,
  DeviceMapping,
  DevicePublication,
} from "../src/device-protocol.js";
import type { PairingStore } from "../src/storage.js";
export const deviceProfile = "00000000-0000-4000-8000-000000000001";
export const deviceBridgeUuid = "00000000-0000-4000-8000-000000000002";
export function deviceChild(
  index = 3,
  mappings: DeviceMapping[] = ["contact"],
): DeviceChild {
  return {
    device_id: index.toString(16).padStart(64, "0"),
    accessory_uuid: `00000000-0000-4000-8000-${index.toString(16).padStart(12, "0")}`,
    name: `Sensor ${index}`,
    model: "Fixture Sensor",
    mappings,
    available: true,
    alarm: null,
    light: null,
    values: {
      contact: null,
      motion: null,
      leak: null,
      smoke: null,
      carbon_monoxide: null,
      ...Object.fromEntries(mappings.map((mapping) => [mapping, false])),
    },
  };
}
export function devicePublication(
  changes: Partial<DevicePublication> = {},
): DevicePublication {
  return {
    scope: { profile_id: deviceProfile, account_generation: 1 },
    registry_revision: 1,
    sequence: 1,
    expires_at_ms: 15001,
    bridge: {
      accessory_uuid: deviceBridgeUuid,
      username: "02:11:22:33:44:55",
      name: "Halo Bridge",
    },
    children: [deviceChild()],
    ...changes,
  };
}
export class DeviceFakePublisher implements DevicePublisher {
  readonly published: Bridge[] = [];
  readonly unpublished: Bridge[] = [];
  events = true;
  fail = false;
  barrier: Promise<void> | null = null;
  async publish(bridge: Bridge): Promise<void> {
    this.published.push(bridge);
    await this.barrier;
    if (this.events) {
      bridge.emit(AccessoryEventTypes.LISTENING, 12345, "fixture");
      bridge.emit(AccessoryEventTypes.ADVERTISED);
    }
    if (this.fail) throw new Error("fixture failure");
  }
  async unpublish(bridge: Bridge): Promise<void> {
    this.unpublished.push(bridge);
  }
}
export function deviceHarness(
  onAlarmSet?: DeviceBridgeOptions["onAlarmSet"],
  onLightSet?: DeviceBridgeOptions["onLightSet"],
) {
  let now = 1,
    closes = 0,
    preflights = 0;
  const publisher = new DeviceFakePublisher();
  const store = {
    preflight: () => {
      preflights++;
    },
    close: () => {
      closes++;
    },
  } as unknown as PairingStore;
  const manager = new DeviceBridgeManager({
    publisher,
    store,
    monotonicNow: () => now,
    ...(onAlarmSet ? { onAlarmSet } : {}),
    ...(onLightSet ? { onLightSet } : {}),
  });
  return {
    manager,
    publisher,
    store,
    set now(value: number) {
      now = value;
    },
    get closes() {
      return closes;
    },
    get preflights() {
      return preflights;
    },
  };
}
