/** One bounded parent RPC queue shared by narrowly scoped device controls. */
import { randomUUID } from "node:crypto";
import { HAPStatus, HapStatusError } from "@homebridge/hap-nodejs";
import { ProtocolError } from "./protocol.js";
import {
  validateDeviceAlarm,
  validateDeviceLight,
  type DeviceAlarmRequest,
  type DeviceAlarmResult,
  type DeviceLightRequest,
  type DeviceLightResult,
  type DeviceEnvelope,
} from "./device-protocol.js";
const failure = (): Error =>
  new HapStatusError(HAPStatus.SERVICE_COMMUNICATION_FAILURE);
interface PendingCommand {
  request: DeviceEnvelope;
  timer: NodeJS.Timeout;
  resolve: (result: DeviceAlarmResult | DeviceLightResult) => void;
  reject: (error: Error) => void;
}
export interface DeviceCommandChannelOptions {
  boot: () => string | null;
  send: (message: DeviceEnvelope) => Promise<void>;
  fail: (code: string) => void;
  timeoutMs?: number;
}
/** This queue never owns a Ring command; disconnect cannot retry or cancel provider actuation. */
export class DeviceCommandRequests {
  private readonly pending = new Map<string, PendingCommand>();
  private closed = false;
  constructor(private readonly options: DeviceCommandChannelOptions) {}
  request(
    kind: "alarm_set",
    payload: DeviceAlarmRequest,
  ): Promise<DeviceAlarmResult>;
  request(
    kind: "light_set",
    payload: DeviceLightRequest,
  ): Promise<DeviceLightResult>;
  async request(
    kind: "alarm_set" | "light_set",
    payload: DeviceAlarmRequest | DeviceLightRequest,
  ): Promise<DeviceAlarmResult | DeviceLightResult> {
    (kind === "alarm_set" ? validateDeviceAlarm : validateDeviceLight)(payload);
    const boot = this.options.boot();
    if (this.closed || boot === null) throw new ProtocolError("not-ready");
    if (this.pending.size >= 4) throw new ProtocolError("busy");
    const message: DeviceEnvelope = {
      v: 1,
      kind,
      boot_id: boot,
      request_id: randomUUID(),
      payload: { ...structuredClone(payload) },
    };
    return new Promise<DeviceAlarmResult | DeviceLightResult>(
      (resolve, reject) => {
        const timer = setTimeout(() => {
          this.options.fail("device-command-timeout");
          this.close();
        }, this.options.timeoutMs ?? 15_000);
        this.pending.set(message.request_id, {
          request: message,
          timer,
          resolve,
          reject,
        });
        void this.options.send(message).catch(() => {
          this.options.fail("device-command-write-failed");
          this.close();
        });
      },
    );
  }
  receive(message: DeviceEnvelope): void {
    const pending = this.pending.get(message.request_id);
    if (
      this.closed ||
      message.kind !== `${pending?.request.kind}_result` ||
      message.boot_id !== this.options.boot() ||
      !pending
    )
      throw new ProtocolError("unowned-command-result");
    (message.kind === "alarm_set_result"
      ? validateDeviceAlarm
      : validateDeviceLight)(message.payload, true);
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
      if (!same) throw new ProtocolError("mismatched-command-result");
    }
    this.pending.delete(message.request_id);
    clearTimeout(pending.timer);
    pending.resolve(
      message.payload as unknown as DeviceAlarmResult | DeviceLightResult,
    );
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
