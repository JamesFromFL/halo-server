/** Readback-driven alarm service and one narrowly scoped parent RPC. */
import { randomUUID } from "node:crypto";
import {
  Accessory,
  AccessoryEventTypes,
  Characteristic,
  HAPStatus,
  HapStatusError,
  Perms,
  Service,
} from "@homebridge/hap-nodejs";
import { ProtocolError } from "./protocol.js";
import {
  validateDeviceAlarm,
  type AlarmMode,
  type DeviceAlarmRequest,
  type DeviceAlarmResult,
  type DeviceChild,
  type DeviceEnvelope,
} from "./device-protocol.js";
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
interface PendingAlarm {
  request: DeviceEnvelope;
  timer: NodeJS.Timeout;
  resolve: (result: DeviceAlarmResult) => void;
  reject: (error: Error) => void;
}
export interface DeviceAlarmChannelOptions {
  boot: () => string | null;
  send: (message: DeviceEnvelope) => Promise<void>;
  fail: (code: string) => void;
  timeoutMs?: number;
}
/** This queue never owns a Ring command; disconnect cannot retry or cancel provider actuation. */
export class DeviceAlarmRequests {
  private readonly pending = new Map<string, PendingAlarm>();
  private closed = false;
  constructor(private readonly options: DeviceAlarmChannelOptions) {}
  async request(payload: DeviceAlarmRequest): Promise<DeviceAlarmResult> {
    validateDeviceAlarm(payload);
    const boot = this.options.boot();
    if (this.closed || boot === null) throw new ProtocolError("not-ready");
    if (this.pending.size >= 4) throw new ProtocolError("busy");
    const message: DeviceEnvelope = {
      v: 1,
      kind: "alarm_set",
      boot_id: boot,
      request_id: randomUUID(),
      payload: { ...structuredClone(payload) },
    };
    return new Promise<DeviceAlarmResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.options.fail("alarm-command-timeout");
        this.close();
      }, this.options.timeoutMs ?? 15_000);
      this.pending.set(message.request_id, {
        request: message,
        timer,
        resolve,
        reject,
      });
      void this.options.send(message).catch(() => {
        this.options.fail("alarm-command-write-failed");
        this.close();
      });
    });
  }
  receive(message: DeviceEnvelope): void {
    const pending = this.pending.get(message.request_id);
    if (
      this.closed ||
      message.kind !== "alarm_set_result" ||
      message.boot_id !== this.options.boot() ||
      !pending
    )
      throw new ProtocolError("unowned-alarm-result");
    validateDeviceAlarm(message.payload, true);
    for (const key of [
      "scope",
      "registry_revision",
      "sequence",
      "accessory_uuid",
      "device_id",
      "target",
    ]) {
      const left = message.payload[key],
        right = pending.request.payload[key];
      const same =
        key === "scope"
          ? (left as DeviceAlarmRequest["scope"]).profile_id ===
              (right as DeviceAlarmRequest["scope"]).profile_id &&
            (left as DeviceAlarmRequest["scope"]).account_generation ===
              (right as DeviceAlarmRequest["scope"]).account_generation
          : left === right;
      if (!same) throw new ProtocolError("mismatched-alarm-result");
    }
    this.pending.delete(message.request_id);
    clearTimeout(pending.timer);
    pending.resolve(message.payload as unknown as DeviceAlarmResult);
  }
  close(): void {
    if (this.closed) return;
    this.closed = true;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(failure());
    }
    this.pending.clear();
  }
}
