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
  private readonly children = new Map<string, DeviceSensor>();
  private readonly identities = new Map<
    string,
    { uuid: string; mappings: string }
  >();
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
          known.mappings !== child.mappings.join())
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
        .setCharacteristic(Characteristic.Model, "Ring Sensor Bridge")
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
    this.deadline = Math.min(this.now() + 15_000, value.expires_at_ms);
    for (const child of [...value.children].sort((a, b) =>
      a.device_id.localeCompare(b.device_id),
    )) {
      const sensor = this.children.get(child.device_id);
      if (sensor) sensor.update(child);
      else {
        const sensor = new DeviceSensor(child, () => this.fresh());
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
