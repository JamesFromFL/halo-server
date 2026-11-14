/** Actual lamp state, separate from the finite override requested by a controller. */
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

export class DeviceLight {
  readonly accessory: Accessory;
  readonly service: Service;
  private current: DeviceChild;
  private observationGeneration = 0;
  constructor(
    value: DeviceChild,
    private readonly fresh: () => boolean,
    private readonly setLight: (target: boolean) => Promise<boolean>,
  ) {
    this.current = structuredClone(value);
    this.accessory = new Accessory(value.name, value.accessory_uuid);
    this.accessory
      .getService(Service.AccessoryInformation)!
      .setCharacteristic(Characteristic.Manufacturer, "Ring via Halo")
      .setCharacteristic(
        Characteristic.Model,
        value.model || "Ring Smart Lighting",
      )
      .setCharacteristic(Characteristic.SerialNumber, value.accessory_uuid);
    this.accessory.on(AccessoryEventTypes.IDENTIFY, (_paired, callback) =>
      callback(),
    );
    this.service = new Service.Lightbulb(value.name, "halo:lightbulb");
    this.service.setPrimaryService(true);
    const on = this.service.getCharacteristic(Characteristic.On);
    // WRITE_RESPONSE prevents HAP from replacing observed state with the request.
    // Returning to automatic mode can leave the actual lamp on due to motion.
    on.setProps({
      perms: [
        Perms.PAIRED_READ,
        Perms.PAIRED_WRITE,
        Perms.NOTIFY,
        Perms.WRITE_RESPONSE,
      ],
    });
    on.onGet(() => this.observed());
    on.onSet(async (target) => {
      if (typeof target !== "boolean")
        throw new HapStatusError(HAPStatus.INVALID_VALUE_IN_REQUEST);
      if (!this.current.light?.control_enabled)
        throw new HapStatusError(HAPStatus.READ_ONLY_CHARACTERISTIC);
      this.observed();
      const generation = this.observationGeneration;
      const readback = await this.setLight(target);
      if (
        !this.current.available ||
        !this.fresh() ||
        !this.current.light?.control_enabled
      )
        throw unavailable();
      // Changed physical state wins over an earlier readback. Repeated snapshots
      // of unchanged state (for example while pending) do not supersede it.
      const observed =
        generation === this.observationGeneration ? readback : this.observed();
      // This is provider readback, not an optimistic reflection of `target`.
      this.current.light.on = observed;
      on.updateValue(observed);
      this.service.updateCharacteristic(Characteristic.StatusFault, 0);
      // Controllers may omit a write-response request. Notify the real state
      // again after completion, using any newer observation already received.
      setImmediate(() => {
        if (this.known()) on.sendEventNotification(this.current.light!.on!);
      });
      return observed;
    });
    this.service
      .getCharacteristic(Characteristic.StatusFault)
      .onGet(() => (this.known() ? 0 : 1));
    this.accessory.addService(this.service);
    this.update(value);
  }
  private known(): boolean {
    return (
      this.current.available &&
      this.fresh() &&
      typeof this.current.light?.on === "boolean"
    );
  }
  private observed(): boolean {
    if (!this.known()) throw unavailable();
    return this.current.light!.on!;
  }
  update(value: DeviceChild): void {
    if (
      this.current.available !== value.available ||
      this.current.light?.on !== value.light?.on
    )
      this.observationGeneration++;
    this.current = structuredClone(value);
    this.accessory.displayName = value.name;
    this.accessory
      .getService(Service.AccessoryInformation)!
      .updateCharacteristic(Characteristic.Name, value.name)
      .updateCharacteristic(
        Characteristic.Model,
        value.model || "Ring Smart Lighting",
      );
    this.service.displayName = value.name;
    this.service.updateCharacteristic(Characteristic.Name, value.name);
    this.service
      .getCharacteristic(Characteristic.On)
      .updateValue(this.known() ? value.light!.on! : unavailable());
    this.service.updateCharacteristic(
      Characteristic.StatusFault,
      this.known() ? 0 : 1,
    );
  }
  invalidate(): void {
    this.update({
      ...this.current,
      available: false,
      light: { on: null, control_enabled: false },
    });
  }
}
