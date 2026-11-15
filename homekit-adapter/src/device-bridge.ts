import {
  Accessory,
  AccessoryEventTypes,
  Bridge,
  Categories,
  Characteristic,
  HAPStatus,
  HapStatusError,
  Service,
} from "@homebridge/hap-nodejs";
import { DeviceAlarm } from "./device-alarm.js";
import { DeviceKeypad } from "./device-keypad.js";
import { DeviceLight } from "./device-light.js";
import { ProtocolError, type PairingManifest } from "./protocol.js";
import { pairingSnapshot } from "./pairing.js";
import { pairingRevision, type PairingStore } from "./storage.js";
import {
  validateDevicePairing,
  validateDevicePublication,
  type DeviceChild,
  type DeviceMapping,
  type DevicePairingIdentity,
  type DevicePairingRequest,
  type DevicePublication,
  type AlarmMode,
  type DeviceAlarmRequest,
  type DeviceAlarmResult,
  type DeviceLightRequest,
  type DeviceLightResult,
  type DeviceKeypadRequest,
  type DeviceKeypadResult,
  type DeviceKeypadLockRequest,
  type DeviceKeypadLockResult,
} from "./device-protocol.js";

const sensors = {
  contact: {
    service: Service.ContactSensor,
    characteristic: Characteristic.ContactSensorState,
  },
  motion: {
    service: Service.MotionSensor,
    characteristic: Characteristic.MotionDetected,
  },
  leak: {
    service: Service.LeakSensor,
    characteristic: Characteristic.LeakDetected,
  },
  smoke: {
    service: Service.SmokeSensor,
    characteristic: Characteristic.SmokeDetected,
  },
  carbon_monoxide: {
    service: Service.CarbonMonoxideSensor,
    characteristic: Characteristic.CarbonMonoxideDetected,
  },
} as const;
function unavailable(): Error {
  return new HapStatusError(HAPStatus.SERVICE_COMMUNICATION_FAILURE);
}
/** Typed read-only services. An absent or stale observation never becomes a safe value. */
export class DeviceSensor {
  readonly accessory: Accessory;
  private current: DeviceChild;
  private readonly services = new Map<DeviceMapping, Service>();
  constructor(
    value: DeviceChild,
    private readonly fresh: () => boolean,
  ) {
    this.current = structuredClone(value);
    this.accessory = new Accessory(value.name, value.accessory_uuid);
    this.accessory
      .getService(Service.AccessoryInformation)!
      .setCharacteristic(Characteristic.Manufacturer, "Ring via Halo")
      .setCharacteristic(Characteristic.Model, value.model || "Ring sensor")
      .setCharacteristic(Characteristic.SerialNumber, value.accessory_uuid);
    // Identify is an acknowledgement only; it has no provider or command path.
    this.accessory.on(AccessoryEventTypes.IDENTIFY, (_paired, callback) =>
      callback(),
    );
    for (const mapping of value.mappings) {
      if (
        mapping === "security_system" ||
        mapping === "lightbulb" ||
        mapping === "keypad_light"
      )
        throw new ProtocolError("invalid-device-child");
      const definition = sensors[mapping],
        service = new definition.service(value.name, `halo:${mapping}`);
      const reading = service.getCharacteristic(definition.characteristic);
      reading.onGet(() => {
        const observation = this.observation(mapping);
        if (observation === null) throw unavailable();
        return mapping === "motion" ? observation : Number(observation);
      });
      service
        .getCharacteristic(Characteristic.StatusFault)
        .onGet(() => (this.observation(mapping) === null ? 1 : 0));
      this.accessory.addService(service);
      this.services.set(mapping, service);
    }
    this.update(value);
  }
  private observation(mapping: DeviceMapping): boolean | null {
    return this.fresh() && this.current.available
      ? this.current.values[mapping]
      : null;
  }
  update(value: DeviceChild): void {
    this.current = structuredClone(value);
    this.accessory.displayName = value.name;
    this.accessory
      .getService(Service.AccessoryInformation)!
      .updateCharacteristic(Characteristic.Name, value.name)
      .updateCharacteristic(Characteristic.Model, value.model || "Ring sensor");
    for (const [mapping, service] of this.services) {
      service.displayName = value.name;
      service.updateCharacteristic(Characteristic.Name, value.name);
      const observation = this.observation(mapping),
        characteristic = service.getCharacteristic(
          sensors[mapping].characteristic,
        );
      characteristic.updateValue(
        observation === null
          ? unavailable()
          : mapping === "motion"
            ? observation
            : Number(observation),
      );
      service.updateCharacteristic(
        Characteristic.StatusFault,
        observation === null ? 1 : 0,
      );
    }
  }
  invalidate(): void {
    this.update({
      ...this.current,
      available: false,
      values: {
        contact: null,
        motion: null,
        leak: null,
        smoke: null,
        carbon_monoxide: null,
      },
    });
  }
}
export interface DevicePublisher {
  publish(bridge: Bridge, identity: DevicePublication["bridge"]): Promise<void>;
  unpublish(bridge: Bridge): Promise<void>;
}
export function deviceHapPublisher(store: PairingStore): DevicePublisher {
  return {
    async publish(bridge, identity) {
      store.initialize();
      await bridge.publish(
        {
          username: identity.username,
          pincode: store.pincode(identity.username),
          category: Categories.BRIDGE,
          port: 0,
          addIdentifyingMaterial: false,
        },
        false,
      );
    },
    async unpublish(bridge) {
      await bridge.unpublish();
    },
  };
}
interface PairingBase extends DevicePairingIdentity {
  accepted: boolean;
  code: string;
  published: boolean;
  paired: boolean | null;
  paired_clients: number | null;
  pairing_revision: string | null;
}
export interface DevicePairingStatus extends PairingBase {
  pairing_manifest: PairingManifest | null;
}
export interface DevicePairingReveal extends PairingBase {
  pincode: string | null;
  setup_uri: string | null;
  ttl_ms: number;
}
export interface DevicePairingReset extends PairingBase {
  pairing_manifest: PairingManifest | null;
  quiesced: boolean;
}
export interface DeviceBridgeOptions {
  store: PairingStore;
  publisher: DevicePublisher;
  monotonicNow?: () => number;
  onAlarmSet?: (request: DeviceAlarmRequest) => Promise<DeviceAlarmResult>;
  onLightSet?: (request: DeviceLightRequest) => Promise<DeviceLightResult>;
  onKeypadSet?: (request: DeviceKeypadRequest) => Promise<DeviceKeypadResult>;
  onKeypadLock?: (
    request: DeviceKeypadLockRequest,
  ) => Promise<DeviceKeypadLockResult>;
}
const sameScope = (
  a: DevicePublication["scope"],
  b: DevicePublication["scope"],
): boolean =>
  a.profile_id === b.profile_id &&
  a.account_generation === b.account_generation;

/** A process has exactly one immutable account and bridge identity. Python owns all Ring access. */
export class DeviceBridgeManager {
  private bridge: Bridge | null = null;
  private publication: DevicePublication | null = null;
  private readonly children = new Map<
    string,
    DeviceSensor | DeviceAlarm | DeviceLight | DeviceKeypad
  >();
  private readonly identities = new Map<
    string,
    { uuid: string; mappings: string }
  >();
  private readonly pendingCommands = new Set<string>();
  private readonly commandEpochs = new Map<string, number>();
  private expiry: NodeJS.Timeout | null = null;
  private deadline = 0;
  private claimed = false;
  private listening = false;
  private advertised = false;
  private fenced = false;
  private closed = false;
  private closing: Promise<void> | null = null;
  constructor(private readonly options: DeviceBridgeOptions) {}
  private now(): number {
    return (
      this.options.monotonicNow?.() ??
      Number(process.hrtime.bigint() / 1_000_000n)
    );
  }
  get accessory(): Bridge | null {
    return this.bridge;
  }
  get published(): boolean {
    return (
      !this.fenced &&
      !this.closed &&
      this.claimed &&
      this.listening &&
      this.advertised
    );
  }
  get count(): number {
    return this.children.size;
  }
  private fresh(): boolean {
    return !this.fenced && !this.closed && this.now() < this.deadline;
  }
  invalidate(): void {
    this.fenced = true;
    this.deadline = 0;
    if (this.expiry) clearTimeout(this.expiry);
    this.expiry = null;
    for (const sensor of this.children.values()) sensor.invalidate();
  }
  async publish(value: DevicePublication): Promise<void> {
    validateDevicePublication(value);
    if (this.closed || this.fenced) throw new ProtocolError("not-ready");
    const previous = this.publication;
    if (
      previous &&
      (!sameScope(previous.scope, value.scope) ||
        previous.bridge.accessory_uuid !== value.bridge.accessory_uuid ||
        previous.bridge.username !== value.bridge.username)
    )
      throw new ProtocolError("stale-scope");
    if (
      previous &&
      (value.registry_revision < previous.registry_revision ||
        value.sequence <= previous.sequence)
    )
      throw new ProtocolError("conflict");
    if (value.expires_at_ms <= this.now()) throw new ProtocolError("not-ready");
    // A registry revision owns the graph. Observation-only snapshots cannot change it.
    const graph = (snapshot: DevicePublication): string =>
      JSON.stringify(
        snapshot.children
          .map((child) => [
            child.device_id,
            child.accessory_uuid,
            child.mappings,
          ])
          .sort((a, b) => String(a[0]).localeCompare(String(b[0]))),
      );
    if (
      previous &&
      value.registry_revision === previous.registry_revision &&
      graph(value) !== graph(previous)
    )
      throw new ProtocolError("conflict");
    let additions = 0;
    for (const child of value.children) {
      const known = this.identities.get(child.device_id);
      if (
        known &&
        (known.uuid !== child.accessory_uuid ||
          (known.mappings !== child.mappings.join() &&
            !(
              known.mappings === "keypad_light" &&
              child.mappings.join() === "keypad_light,motion"
            )))
      )
        throw new ProtocolError("conflict");
      if (!known) additions++;
      for (const [id, identity] of this.identities)
        if (id !== child.device_id && identity.uuid === child.accessory_uuid)
          throw new ProtocolError("conflict");
    }
    if (this.identities.size + additions > 1024)
      throw new ProtocolError("device-capacity");
    this.options.store.preflight();
    if (!this.bridge) {
      this.bridge = new Bridge(value.bridge.name, value.bridge.accessory_uuid);
      this.bridge.disableUnusedIDPurge();
      this.bridge
        .getService(Service.AccessoryInformation)!
        .setCharacteristic(Characteristic.Manufacturer, "Halo")
        .setCharacteristic(Characteristic.Model, "Ring Device Bridge")
        .setCharacteristic(
          Characteristic.SerialNumber,
          value.bridge.accessory_uuid,
        );
      this.bridge.on(AccessoryEventTypes.IDENTIFY, (_paired, callback) =>
        callback(),
      );
      this.bridge.on(AccessoryEventTypes.LISTENING, () => {
        if (this.claimed && !this.fenced && !this.closed) this.listening = true;
      });
      this.bridge.on(AccessoryEventTypes.ADVERTISED, () => {
        if (this.claimed && this.listening && !this.fenced && !this.closed)
          this.advertised = true;
      });
    }
    const target = new Set(value.children.map((child) => child.device_id));
    for (const [id, sensor] of this.children)
      if (!target.has(id)) {
        sensor.invalidate();
        this.bridge.removeBridgedAccessory(sensor.accessory);
        this.children.delete(id);
      }
    const previousChildren = new Map(
      (previous?.children ?? []).map((child) => [child.device_id, child]),
    );
    const currentChildren = new Map(
      value.children.map((child) => [child.device_id, child]),
    );
    for (const id of new Set([
      ...previousChildren.keys(),
      ...currentChildren.keys(),
    ])) {
      const authority = (child: DeviceChild | undefined): string =>
        JSON.stringify([
          child?.accessory_uuid,
          child?.available,
          child?.alarm?.control_enabled,
          child?.light?.control_enabled,
        ]);
      if (
        (previous !== null && this.now() >= this.deadline) ||
        authority(previousChildren.get(id)) !==
          authority(currentChildren.get(id))
      )
        this.commandEpochs.set(id, (this.commandEpochs.get(id) ?? 0) + 1);
      for (const kind of ["set", "lock"] as const) {
        const authority = (child: DeviceChild | undefined): string =>
          JSON.stringify([
            child?.accessory_uuid,
            child?.keypad?.control_enabled,
            ...(kind === "set"
              ? [child?.available, child?.keypad?.locked]
              : []),
          ]);
        const lane = `${id}:keypad_${kind}`;
        if (
          (previous !== null && this.now() >= this.deadline) ||
          authority(previousChildren.get(id)) !==
            authority(currentChildren.get(id))
        )
          this.commandEpochs.set(lane, (this.commandEpochs.get(lane) ?? 0) + 1);
      }
    }
    this.deadline = Math.min(this.now() + 15_000, value.expires_at_ms);
    for (const child of [...value.children].sort((a, b) =>
      a.device_id.localeCompare(b.device_id),
    )) {
      const sensor = this.children.get(child.device_id);
      if (sensor) {
        sensor.update(child);
        this.identities.set(child.device_id, {
          uuid: child.accessory_uuid,
          mappings: child.mappings.join(),
        });
      } else {
        const sensor = child.mappings.includes("security_system")
          ? new DeviceAlarm(
              child,
              () => this.fresh(),
              (target) => this.setAlarm(child.device_id, target),
            )
          : child.mappings.includes("lightbulb")
            ? new DeviceLight(
                child,
                () => this.fresh(),
                (target) => this.setLight(child.device_id, target),
              )
            : child.mappings.includes("keypad_light")
              ? new DeviceKeypad(
                  child,
                  () => this.fresh(),
                  async (target) =>
                    (await this.setKeypad(
                      child.device_id,
                      target,
                      false,
                    )) as number,
                  async (target) =>
                    (await this.setKeypad(
                      child.device_id,
                      target,
                      true,
                    )) as boolean,
                )
              : new DeviceSensor(child, () => this.fresh());
        this.bridge.addBridgedAccessory(sensor.accessory);
        this.children.set(child.device_id, sensor);
        this.identities.set(child.device_id, {
          uuid: child.accessory_uuid,
          mappings: child.mappings.join(),
        });
      }
    }
    this.bridge.displayName = value.bridge.name;
    this.bridge
      .getService(Service.AccessoryInformation)!
      .updateCharacteristic(Characteristic.Name, value.bridge.name);
    this.publication = structuredClone(value);
    if (this.expiry) clearTimeout(this.expiry);
    this.expiry = setTimeout(
      () => {
        this.expiry = null;
        for (const sensor of this.children.values()) sensor.invalidate();
      },
      Math.max(1, this.deadline - this.now()),
    );
    try {
      if (!this.claimed) {
        this.claimed = true;
        await this.options.publisher.publish(this.bridge, value.bridge);
      }
    } catch (error) {
      this.invalidate();
      throw error;
    }
  }
  async setAlarm(deviceId: string, target: AlarmMode): Promise<void> {
    const publication = this.publication;
    const child = publication?.children.find(
      (item) => item.device_id === deviceId,
    );
    if (
      !publication ||
      !child ||
      !this.fresh() ||
      !this.published ||
      !child.available ||
      !child.alarm?.control_enabled ||
      child.alarm.triggered === null ||
      child.alarm.mode === null ||
      !this.options.onAlarmSet ||
      !["disarmed", "home", "away"].includes(target)
    )
      throw unavailable();
    if (this.pendingCommands.has(deviceId) || this.pendingCommands.size >= 4)
      throw new HapStatusError(HAPStatus.RESOURCE_BUSY);
    this.pendingCommands.add(deviceId);
    const epoch = this.commandEpochs.get(deviceId);
    const request: DeviceAlarmRequest = {
      scope: structuredClone(publication.scope),
      registry_revision: publication.registry_revision,
      sequence: publication.sequence,
      device_id: child.device_id,
      accessory_uuid: child.accessory_uuid,
      target,
    };
    try {
      const result = await this.options.onAlarmSet(request);
      const current = this.publication,
        now = current?.children.find((item) => item.device_id === deviceId);
      if (
        !result.accepted ||
        this.commandEpochs.get(deviceId) !== epoch ||
        !this.fresh() ||
        !this.published ||
        !current ||
        !sameScope(current.scope, request.scope) ||
        current.registry_revision !== request.registry_revision ||
        !now?.available ||
        now.accessory_uuid !== request.accessory_uuid ||
        !now.alarm?.control_enabled
      )
        throw unavailable();
      // The parent only accepts after observed readback. CurrentState is never set here.
    } finally {
      this.pendingCommands.delete(deviceId);
    }
  }
  async setLight(deviceId: string, target: boolean): Promise<boolean> {
    const publication = this.publication,
      child = publication?.children.find((item) => item.device_id === deviceId);
    if (
      !publication ||
      !child ||
      !this.fresh() ||
      !this.published ||
      !child.available ||
      !child.light?.control_enabled ||
      typeof child.light.on !== "boolean" ||
      typeof target !== "boolean" ||
      !this.options.onLightSet
    )
      throw unavailable();
    if (this.pendingCommands.has(deviceId) || this.pendingCommands.size >= 4)
      throw new HapStatusError(HAPStatus.RESOURCE_BUSY);
    this.pendingCommands.add(deviceId);
    const epoch = this.commandEpochs.get(deviceId);
    const request: DeviceLightRequest = {
      scope: structuredClone(publication.scope),
      registry_revision: publication.registry_revision,
      sequence: publication.sequence,
      device_id: child.device_id,
      accessory_uuid: child.accessory_uuid,
      target,
    };
    try {
      const result = await this.options.onLightSet(request);
      const current = this.publication,
        now = current?.children.find((item) => item.device_id === deviceId);
      if (
        !result.accepted ||
        typeof result.observed_on !== "boolean" ||
        this.commandEpochs.get(deviceId) !== epoch ||
        !this.fresh() ||
        !this.published ||
        !current ||
        !sameScope(current.scope, request.scope) ||
        current.registry_revision !== request.registry_revision ||
        !now?.available ||
        now.accessory_uuid !== request.accessory_uuid ||
        !now.light?.control_enabled
      )
        throw unavailable();
      return result.observed_on;
    } finally {
      this.pendingCommands.delete(deviceId);
    }
  }
  private async setKeypad(
    deviceId: string,
    target: number | boolean,
    lock: boolean,
  ): Promise<number | boolean> {
    const publication = this.publication;
    const child = publication?.children.find(
      (item) => item.device_id === deviceId,
    );
    const callback = lock
      ? this.options.onKeypadLock
      : this.options.onKeypadSet;
    if (
      !publication ||
      !child?.keypad?.control_enabled ||
      !this.fresh() ||
      !this.published ||
      !callback ||
      !child.mappings.includes("keypad_light") ||
      (lock
        ? typeof target !== "boolean"
        : typeof target !== "number" ||
          !Number.isInteger(target) ||
          target < 0 ||
          target > 100 ||
          !child.available ||
          child.keypad.locked ||
          child.keypad.brightness === null)
    )
      throw unavailable();
    const lane = `${deviceId}:keypad_${lock ? "lock" : "set"}`;
    if (this.pendingCommands.has(lane) || this.pendingCommands.size >= 4)
      throw new HapStatusError(HAPStatus.RESOURCE_BUSY);
    this.pendingCommands.add(lane);
    const epoch = this.commandEpochs.get(lane);
    const identity = {
      scope: structuredClone(publication.scope),
      registry_revision: publication.registry_revision,
      sequence: publication.sequence,
      device_id: child.device_id,
      accessory_uuid: child.accessory_uuid,
    };
    try {
      const result = lock
        ? await this.options.onKeypadLock!({
            ...identity,
            target: target as boolean,
          })
        : await this.options.onKeypadSet!({
            ...identity,
            target: target as number,
          });
      const current = this.publication,
        now = current?.children.find((item) => item.device_id === deviceId);
      const observed =
        "observed_locked" in result
          ? result.observed_locked
          : result.observed_brightness;
      if (
        !result.accepted ||
        observed === null ||
        this.commandEpochs.get(lane) !== epoch ||
        !this.fresh() ||
        !this.published ||
        !current ||
        !sameScope(current.scope, identity.scope) ||
        current.registry_revision !== identity.registry_revision ||
        now?.accessory_uuid !== identity.accessory_uuid ||
        !now.keypad?.control_enabled ||
        (!lock && (!now.available || now.keypad.locked))
      )
        throw unavailable();
      return observed;
    } finally {
      this.pendingCommands.delete(lane);
    }
  }
  private base(identity: DevicePairingIdentity): PairingBase {
    return {
      ...identity,
      accepted: false,
      code: "not-ready",
      published: false,
      paired: null,
      paired_clients: null,
      pairing_revision: null,
    };
  }
  private matches(identity: DevicePairingIdentity): boolean {
    const value = this.publication;
    return (
      !this.closed &&
      !this.fenced &&
      value !== null &&
      sameScope(value.scope, identity.scope) &&
      value.registry_revision === identity.registry_revision &&
      value.bridge.accessory_uuid === identity.accessory_uuid &&
      value.bridge.username === identity.username
    );
  }
  status(identity: DevicePairingIdentity): DevicePairingStatus {
    validateDevicePairing(identity);
    const result: DevicePairingStatus = {
      ...this.base(identity),
      pairing_manifest: null,
    };
    if (!this.matches(identity)) return result;
    try {
      const snapshot = pairingSnapshot(this.options.store, identity.username);
      return {
        ...result,
        accepted: true,
        code: "ok",
        published: this.published,
        paired: snapshot.paired,
        paired_clients: snapshot.paired_clients,
        pairing_revision: snapshot.pairing_revision,
        pairing_manifest: snapshot.pairing_manifest,
      };
    } catch {
      return result;
    }
  }
  reveal(request: DevicePairingRequest): DevicePairingReveal {
    validateDevicePairing(request, true);
    const { pairing_revision: expected, ...identity } = request;
    const status = this.status(identity),
      { pairing_manifest: _manifest, ...base } = status;
    const result: DevicePairingReveal = {
      ...base,
      accepted: false,
      code: "not-ready",
      pincode: null,
      setup_uri: null,
      ttl_ms: 0,
    };
    if (!status.accepted || !this.published || !this.bridge) return result;
    if (status.pairing_revision !== expected)
      return { ...result, code: "conflict" };
    if (status.paired) return { ...result, code: "policy-denied" };
    try {
      const snapshot = pairingSnapshot(this.options.store, identity.username),
        uri = this.bridge.setupURI();
      if (
        snapshot.pairing_revision !== expected ||
        snapshot.pincode === null ||
        snapshot.paired ||
        !/^X-HM:\/\/[0-9A-Z]{13}$/.test(uri) ||
        pairingRevision(this.options.store.manifest(identity.username)) !==
          expected
      )
        return { ...result, code: "conflict" };
      return {
        ...result,
        accepted: true,
        code: "ok",
        pincode: snapshot.pincode,
        setup_uri: uri,
        ttl_ms: 60_000,
      };
    } catch {
      return result;
    }
  }
  async prepareReset(
    request: DevicePairingRequest,
  ): Promise<DevicePairingReset> {
    validateDevicePairing(request, true);
    const { pairing_revision: expected, ...identity } = request,
      status = this.status(identity);
    const result: DevicePairingReset = {
      ...status,
      accepted: false,
      code: "not-ready",
      pairing_manifest: null,
      quiesced: false,
    };
    if (!status.accepted) return result;
    if (status.pairing_revision !== expected)
      return { ...result, code: "conflict" };
    this.invalidate();
    try {
      if (this.bridge && this.claimed)
        await this.options.publisher.unpublish(this.bridge);
      this.claimed = false;
      this.listening = false;
      this.advertised = false;
      const snapshot = pairingSnapshot(this.options.store, identity.username);
      if (snapshot.pairing_revision !== expected)
        return {
          ...result,
          published: false,
          code: "conflict",
          quiesced: true,
        };
      return {
        ...result,
        published: false,
        accepted: true,
        code: "ok",
        pairing_manifest: snapshot.pairing_manifest,
        quiesced: true,
      };
    } catch {
      return { ...result, published: false, code: "unavailable" };
    }
  }
  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closed = true;
    this.invalidate();
    this.closing = (async () => {
      try {
        if (this.bridge && this.claimed)
          await this.options.publisher.unpublish(this.bridge);
        this.claimed = false;
      } finally {
        this.options.store.close();
      }
    })();
    return this.closing;
  }
}
