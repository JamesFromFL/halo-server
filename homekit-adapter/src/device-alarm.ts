/** Readback-driven alarm service and one narrowly scoped parent RPC. */
import {
  Accessory,
  AccessoryEventTypes,
  Characteristic,
  HAPStatus,
  HapStatusError,
  Perms,
  Service,
} from "@homebridge/hap-nodejs";
import { type AlarmMode, type DeviceChild } from "./device-protocol.js";
const targetValues: Record<AlarmMode, number> = {
  home: 0,
  away: 1,
  disarmed: 3,
};
const failure = (): Error =>
  new HapStatusError(HAPStatus.SERVICE_COMMUNICATION_FAILURE);

export class DeviceAlarm {
  readonly accessory: Accessory;
  readonly service: Service;
  private current: DeviceChild;
  constructor(
    value: DeviceChild,
    private readonly fresh: () => boolean,
    private readonly setMode: (target: AlarmMode) => Promise<void>,
  ) {
    this.current = structuredClone(value);
    this.accessory = new Accessory(value.name, value.accessory_uuid);
    this.accessory
      .getService(Service.AccessoryInformation)!
      .setCharacteristic(Characteristic.Manufacturer, "Ring via Halo")
      .setCharacteristic(Characteristic.Model, value.model || "Ring Alarm")
      .setCharacteristic(Characteristic.SerialNumber, value.accessory_uuid);
    this.accessory.on(AccessoryEventTypes.IDENTIFY, (_paired, callback) =>
      callback(),
    );
    this.service = new Service.SecuritySystem(
      value.name,
      "halo:security_system",
    );
    this.service
      .getCharacteristic(Characteristic.SecuritySystemCurrentState)
      .onGet(
        () =>
          this.observedState() ??
          (() => {
            throw failure();
          })(),
      );
    this.service
      .getCharacteristic(Characteristic.StatusFault)
      .onGet(() => (this.observedState() === null ? 1 : 0));
    const target = this.service.getCharacteristic(
      Characteristic.SecuritySystemTargetState,
    );
    target.setProps({
      validValues: [0, 1, 3],
      // Keep the standard descriptor stable. Policy gates every setter below.
      perms: [Perms.PAIRED_READ, Perms.PAIRED_WRITE, Perms.NOTIFY],
    });
    target.onGet(() => {
      const mode = this.mode();
      if (mode === null) throw failure();
      return targetValues[mode];
    });
    target.onSet(async (value) => {
      const mode = Object.entries(targetValues).find(
        ([, item]) => item === value,
      )?.[0] as AlarmMode | undefined;
      if (mode === undefined)
        throw new HapStatusError(HAPStatus.INVALID_VALUE_IN_REQUEST);
      if (!this.current.alarm?.control_enabled)
        throw new HapStatusError(HAPStatus.READ_ONLY_CHARACTERISTIC);
      if (
        !this.current.available ||
        !this.fresh() ||
        this.observedState() === null
      )
        throw failure();
      await this.setMode(mode);
    });
    this.accessory.addService(this.service);
    this.update(value);
  }
  private mode(): AlarmMode | null {
    return this.current.available && this.fresh()
      ? (this.current.alarm?.mode ?? null)
      : null;
  }
  private observedState(): number | null {
    if (!this.current.available || !this.fresh()) return null;
    const state = this.current.alarm;
    if (state?.triggered === true) return 4;
    return state?.triggered === false && state.mode !== null
      ? targetValues[state.mode]
      : null;
  }
  update(value: DeviceChild): void {
    this.current = structuredClone(value);
    this.accessory.displayName = value.name;
    this.accessory
      .getService(Service.AccessoryInformation)!
      .updateCharacteristic(Characteristic.Name, value.name)
      .updateCharacteristic(Characteristic.Model, value.model || "Ring Alarm");
    this.service.displayName = value.name;
    this.service.updateCharacteristic(Characteristic.Name, value.name);
    const mode = this.mode(),
      observed = this.observedState(),
      target = this.service.getCharacteristic(
        Characteristic.SecuritySystemTargetState,
      );
    target.updateValue(mode === null ? failure() : targetValues[mode]);
    this.service
      .getCharacteristic(Characteristic.SecuritySystemCurrentState)
      .updateValue(observed === null ? failure() : observed);
    this.service.updateCharacteristic(
      Characteristic.StatusFault,
      observed === null ? 1 : 0,
    );
  }
  invalidate(): void {
    this.update({
      ...this.current,
      available: false,
      alarm: { mode: null, triggered: null, control_enabled: false },
    });
  }
}
