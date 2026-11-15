/** Keypad backlight and its durable Halo lock, separate from security modes. */
import {
  Accessory,
  AccessoryEventTypes,
  Characteristic,
  HAPStatus,
  HapStatusError,
  Perms,
  Service,
} from "@homebridge/hap-nodejs";
import type { DeviceChild } from "./device-protocol.js";
const unavailable = (): Error =>
  new HapStatusError(HAPStatus.SERVICE_COMMUNICATION_FAILURE);
const writable = [
  Perms.PAIRED_READ,
  Perms.PAIRED_WRITE,
  Perms.NOTIFY,
  Perms.WRITE_RESPONSE,
];
interface Batch {
  on?: boolean;
  brightness?: number;
  fence: number;
  waiters: {
    resolve: (value: number) => void;
    reject: (error: unknown) => void;
  }[];
}

export class DeviceKeypad {
  readonly accessory: Accessory;
  readonly light: Service;
  readonly lock: Service;
  private motion?: Service;
  private current: DeviceChild;
  private observationGeneration = 0;
  private lockGeneration = 0;
  private brightnessFence = 0;
  private batch: Batch | null = null;
  constructor(
    value: DeviceChild,
    private readonly fresh: () => boolean,
    private readonly setBrightness: (target: number) => Promise<number>,
    private readonly setLock: (target: boolean) => Promise<boolean>,
  ) {
    this.current = structuredClone(value);
    this.accessory = new Accessory(value.name, value.accessory_uuid);
    this.accessory
      .getService(Service.AccessoryInformation)!
      .setCharacteristic(Characteristic.Manufacturer, "Ring via Halo")
      .setCharacteristic(Characteristic.Model, value.model || "Ring keypad")
      .setCharacteristic(Characteristic.SerialNumber, value.accessory_uuid);
    this.accessory.on(AccessoryEventTypes.IDENTIFY, (_paired, callback) =>
      callback(),
    );
    this.light = new Service.Lightbulb(value.name, "halo:keypad_light");
    this.light.setPrimaryService(true);
    this.lock = new Service.Switch(
      "Brightness lock",
      "halo:keypad_brightness_lock",
    );
    const on = this.light.getCharacteristic(Characteristic.On);
    const brightness = this.light.getCharacteristic(Characteristic.Brightness);
    on.setProps({ perms: writable });
    brightness.setProps({ perms: writable });
    on.onGet(() => this.observed() > 0);
    brightness.onGet(() => this.observed());
    on.onSet(async (value) => {
      if (typeof value !== "boolean")
        throw new HapStatusError(HAPStatus.INVALID_VALUE_IN_REQUEST);
      this.admitBrightness();
      // Home often writes On=true along with Brightness in one transaction. An
      // already-on write must not start a competing brightness=100 command.
      if (value && this.observed() > 0) return true;
      return (await this.enqueue({ on: value })) > 0;
    });
    brightness.onSet(async (value) => {
      if (
        typeof value !== "number" ||
        !Number.isInteger(value) ||
        value < 0 ||
        value > 100
      )
        throw new HapStatusError(HAPStatus.INVALID_VALUE_IN_REQUEST);
      this.admitBrightness();
      return this.enqueue({ brightness: value });
    });
    this.light
      .getCharacteristic(Characteristic.StatusFault)
      .onGet(() => (this.known() ? 0 : 1));
    const lock = this.lock.getCharacteristic(Characteristic.On);
    lock.setProps({ perms: writable });
    lock.onGet(() => {
      if (!this.fresh()) throw unavailable();
      return this.current.keypad!.locked;
    });
    lock.onSet(async (target) => {
      if (typeof target !== "boolean")
        throw new HapStatusError(HAPStatus.INVALID_VALUE_IN_REQUEST);
      if (!this.fresh() || !this.current.keypad?.control_enabled)
        throw unavailable();
      // Revoke an unsent coalesced dimmer request immediately, without pretending
      // that persistence already succeeded or waiting behind its busy lane.
      if (target) this.brightnessFence++;
      const generation = this.lockGeneration;
      const persisted = await this.setLock(target);
      if (!this.fresh() || !this.current.keypad?.control_enabled)
        throw unavailable();
      const observed =
        generation === this.lockGeneration
          ? persisted
          : this.current.keypad.locked;
      this.current.keypad.locked = observed;
      this.render();
      setImmediate(() => {
        if (this.fresh())
          lock.sendEventNotification(this.current.keypad!.locked);
      });
      return observed;
    });
    this.accessory.addService(this.light);
    this.accessory.addService(this.lock);
    this.update(value);
  }
  private known(): boolean {
    return (
      this.fresh() &&
      this.current.available &&
      typeof this.current.keypad?.brightness === "number"
    );
  }
  private observed(): number {
    if (!this.known()) throw unavailable();
    return this.current.keypad!.brightness!;
  }
  private admitBrightness(): void {
    this.observed();
    if (!this.current.keypad?.control_enabled || this.current.keypad.locked)
      throw new HapStatusError(HAPStatus.READ_ONLY_CHARACTERISTIC);
  }
  private enqueue(value: {
    on?: boolean;
    brightness?: number;
  }): Promise<number> {
    if (!this.batch) {
      this.batch = { fence: this.brightnessFence, waiters: [] };
      setImmediate(() => void this.flush());
    }
    Object.assign(this.batch, value);
    return new Promise((resolve, reject) =>
      this.batch!.waiters.push({ resolve, reject }),
    );
  }
  private async flush(): Promise<void> {
    const batch = this.batch;
    this.batch = null;
    if (!batch) return;
    try {
      this.admitBrightness();
      if (batch.fence !== this.brightnessFence) throw unavailable();
      const target =
        batch.on === false
          ? 0
          : (batch.brightness ?? this.current.keypad!.on_brightness);
      const generation = this.observationGeneration;
      const result = await this.setBrightness(target);
      this.admitBrightness();
      if (batch.fence !== this.brightnessFence) throw unavailable();
      const observed =
        generation === this.observationGeneration ? result : this.observed();
      this.current.keypad!.brightness = observed;
      if (observed > 0) this.current.keypad!.on_brightness = observed;
      this.render();
      for (const waiter of batch.waiters) waiter.resolve(observed);
      setImmediate(() => {
        if (this.known()) {
          this.light
            .getCharacteristic(Characteristic.On)
            .sendEventNotification(this.observed() > 0);
          this.light
            .getCharacteristic(Characteristic.Brightness)
            .sendEventNotification(this.observed());
        }
      });
    } catch (error) {
      for (const waiter of batch.waiters) waiter.reject(error);
    }
  }
  update(value: DeviceChild): void {
    if (
      this.current.available !== value.available ||
      this.current.keypad?.brightness !== value.keypad?.brightness
    )
      this.observationGeneration++;
    if (this.current.keypad?.locked !== value.keypad?.locked)
      this.lockGeneration++;
    if (
      this.current.available !== value.available ||
      this.current.keypad?.control_enabled !== value.keypad?.control_enabled ||
      this.current.keypad?.locked !== value.keypad?.locked
    )
      this.brightnessFence++;
    this.current = structuredClone(value);
    this.accessory.displayName = value.name;
    this.accessory
      .getService(Service.AccessoryInformation)!
      .updateCharacteristic(Characteristic.Name, value.name)
      .updateCharacteristic(Characteristic.Model, value.model || "Ring keypad");
    this.light.displayName = value.name;
    this.light.updateCharacteristic(Characteristic.Name, value.name);
    if (value.mappings.includes("motion") && !this.motion) {
      this.motion = new Service.MotionSensor(
        `${value.name} Motion`,
        "halo:motion",
      );
      this.motion.getCharacteristic(Characteristic.MotionDetected).onGet(() => {
        const v = this.motionObserved();
        if (v === null) throw unavailable();
        return v;
      });
      this.motion
        .getCharacteristic(Characteristic.StatusFault)
        .onGet(() => (this.motionObserved() === null ? 1 : 0));
      this.accessory.addService(this.motion);
    }
    this.render();
  }
  private motionObserved(): boolean | null {
    return this.fresh() && this.current.available
      ? this.current.values.motion
      : null;
  }
  private render(): void {
    this.light
      .getCharacteristic(Characteristic.On)
      .updateValue(this.known() ? this.observed() > 0 : unavailable());
    this.light
      .getCharacteristic(Characteristic.Brightness)
      .updateValue(this.known() ? this.observed() : unavailable());
    this.light.updateCharacteristic(
      Characteristic.StatusFault,
      this.known() ? 0 : 1,
    );
    this.lock
      .getCharacteristic(Characteristic.On)
      .updateValue(this.fresh() ? this.current.keypad!.locked : unavailable());
    if (this.motion) {
      const value = this.motionObserved();
      this.motion
        .getCharacteristic(Characteristic.MotionDetected)
        .updateValue(value === null ? unavailable() : value);
      this.motion.updateCharacteristic(
        Characteristic.StatusFault,
        value === null ? 1 : 0,
      );
    }
  }
  invalidate(): void {
    this.update({
      ...this.current,
      available: false,
      values: { ...this.current.values, motion: null },
      keypad: {
        ...this.current.keypad!,
        brightness: null,
        control_enabled: false,
      },
    });
  }
}
