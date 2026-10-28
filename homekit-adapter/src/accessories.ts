import { AccessoryEventTypes, Categories } from "@homebridge/hap-nodejs";
import { CameraAccessory, type AccessoryIdentity, type MotionState } from "./camera.js";
import { ProtocolError, type AccountScope, type AccessoryPublicationStatus, type JsonObject, type MediaScope, type PairingIdentity, type PairingStatus, type PairingReveal, type PairingReset } from "./protocol.js";
import { pairingSnapshot } from "./pairing.js";
import { pairingRevision } from "./storage.js";
import type { AdapterSession } from "./session.js";
import type { PairingStore } from "./storage.js";

export interface AccessoryConfigurationPage {
  scope: AccountScope | null; revision: number; generation: number; page_index: number;
  total_pages: number; entries: AccessoryIdentity[]; media_ready: boolean;
}
export interface AccessoryPublisher {
  readonly mediaReady: boolean;
  publish(camera: CameraAccessory): Promise<void>;
  unpublish(camera: CameraAccessory): Promise<void>;
}
/** Parent configuration only enables this after its native media worker preflight. */
export function hapPublisher(store: PairingStore): AccessoryPublisher {
  return {
    mediaReady: true,
    async publish(camera) {
      store.initialize();
      await camera.accessory.publish({username: camera.identity.username, pincode: store.pincode(camera.identity.username),
        category: camera.identity.doorbell ? Categories.VIDEO_DOORBELL : Categories.IP_CAMERA,
        port: 0, addIdentifyingMaterial: false}, false);
    },
    async unpublish(camera) { await camera.accessory.unpublish(); },
  };
}

interface Configuration extends Omit<AccessoryConfigurationPage, "page_index" | "total_pages"> { }
interface PendingConfiguration { header: AccessoryConfigurationPage; pages: Map<number, AccessoryConfigurationPage> }
interface Managed { camera: CameraAccessory; published: boolean; listening: boolean; advertised: boolean; stateTimer: NodeJS.Timeout | null }
interface EventState extends MotionState { scope: MediaScope; epoch: string; sequence: number; ttl_ms: number; expires_at_ms: number }
interface ReceivedEventState extends EventState { expiresAt: number }
interface CameraEvent { scope: MediaScope; epoch: string; sequence: number; event_id: string; type: "doorbell"; ttl_ms: number; expires_at_ms: number }
interface EventSync { scope: AccountScope | null; epoch: string; sequence: number; accepted: boolean; code: string }
export interface AccessoryManagerOptions {
  session: Pick<AdapterSession, "cameras" | "inventoryComplete" | "accountScope"> & Partial<Pick<AdapterSession, "camera" | "quiesce">>;
  store?: PairingStore;
  publisher?: AccessoryPublisher;
  syncEvents: (scope: AccountScope | null, epoch: string | null, sequence: number) => Promise<JsonObject>;
  onFailure?: (error: ProtocolError) => void;
  onStatus?: (status: AccessoryPublicationStatus) => Promise<void>;
  reportError?: (stage: string, code: string) => void;
  monotonicNow?: () => number;
}
const sameAccount = (left: AccountScope | null, right: AccountScope | null): boolean =>
  left === null || right === null ? left === right : left.profile_id === right.profile_id && left.account_generation === right.account_generation;
const key = (scope: MediaScope): string => JSON.stringify([scope.profile_id, scope.account_generation,
  scope.camera_id, scope.device_generation, scope.privacy_generation]);
const sameIdentity = (left: AccessoryIdentity, right: AccessoryIdentity): boolean =>
  left.camera_id === right.camera_id && left.accessory_id === right.accessory_id && left.username === right.username && left.doorbell === right.doorbell &&
  (left.recording?.enabled === true) === (right.recording?.enabled === true);
const unknownState: MotionState = {motion: null, synchronized: false, source_state: "connecting"};

/** Bounded selected graph. Runtime scopes are never durable pairing identities. */
export class AccessoryManager {
  private readonly managed = new Map<string, Managed>();
  private readonly states = new Map<string, ReceivedEventState>();
  private readonly lastPress = new Map<string, number>();
  private readonly pressIds = new Set<string>();
  private configuration: Configuration | null = null;
  private pending: PendingConfiguration | null = null;
  private configurationTimer: NodeJS.Timeout | null = null;
  private appliedPages = new Map<number, AccessoryConfigurationPage>();
  private generation = 0;
  private closed = false;
  private failed = false;
  private chain: Promise<void> = Promise.resolve();
  private closing: Promise<void> | null = null;
  private syncing: Promise<void> | null = null;
  private syncAgain = false;
  private eventEpoch: string | null = null;
  private eventBaseline = 0;
  private eventSynchronized = false;
  private eventToken = 0;
  private publicationFenced = true;
  private statusLatest: AccessoryPublicationStatus | null = null;
  private statusDirty = false;
  private statusSending = false;

  constructor(private readonly options: AccessoryManagerOptions) { }
  private now(): number { return this.options.monotonicNow?.() ?? Number(process.hrtime.bigint() / 1_000_000n); }
  get count(): number { return this.managed.size; }
  get publishedCount(): number { return [...this.managed.values()].filter(value => value.published).length; }
  get isFailed(): boolean { return this.failed; }
  accessories(): CameraAccessory[] { return [...this.managed.values()].map(value => value.camera); }

  /** Cleanup claims are distinct from public HAP listener/advertisement evidence. */
  get publicationStatus(): AccessoryPublicationStatus | null {
    const header = this.pending?.header ?? this.configuration;
    if (!header) return null;
    const result: AccessoryPublicationStatus = {scope: structuredClone(header.scope), revision: header.revision,
      generation: header.generation, configured: 0, published: 0, advertised: 0};
    const config = this.configuration, session = this.options.session;
    if (this.closed || this.failed || this.publicationFenced || this.pending || !config?.scope ||
        config.generation !== this.generation || !session.inventoryComplete || !sameAccount(config.scope, session.accountScope)) return result;
    const cameras = new Map(session.cameras().map(camera => [camera.scope.camera_id, camera]));
    for (const identity of config.entries) {
      const managed = this.managed.get(identity.camera_id), camera = cameras.get(identity.camera_id);
      if (!managed || !camera || managed.camera.isRetired || !sameIdentity(identity, managed.camera.identity) ||
          key(managed.camera.scope) !== key(camera.scope) || !sameAccount(camera.scope, config.scope)) continue;
      result.configured++;
      if (managed.published && managed.listening) {
        result.published++;
        if (managed.advertised) result.advertised++;
      }
    }
    return result;
  }

  /** At most one write plus one replacement snapshot; no per-accessory tasks. */
  private notifyStatus(): void {
    if (!this.options.onStatus) return;
    const latest = this.publicationStatus;
    if (!latest || JSON.stringify(latest) === JSON.stringify(this.statusLatest)) return;
    this.statusLatest = latest; this.statusDirty = true;
    this.startStatus();
  }
  private startStatus(): void {
    if (this.statusSending) return;
    this.statusSending = true;
    void (async () => {
      while (this.statusDirty) {
        this.statusDirty = false;
        await this.options.onStatus!(structuredClone(this.statusLatest!));
      }
    })().catch(() => {
      this.failed = true; this.publicationFenced = true;
      this.options.onFailure?.(new ProtocolError("accessory-status-failed"));
    }).finally(() => {
      this.statusSending = false;
      if (this.statusDirty && !this.failed) this.startStatus();
    });
  }

  private observePublication(id: string, managed: Managed): void {
    managed.camera.accessory.on(AccessoryEventTypes.LISTENING, () => {
      if (this.managed.get(id) !== managed || managed.camera.isRetired || !managed.published) return;
      managed.listening = true; this.notifyStatus();
    });
    managed.camera.accessory.on(AccessoryEventTypes.ADVERTISED, () => {
      if (this.managed.get(id) !== managed || managed.camera.isRetired || !managed.published || !managed.listening) return;
      managed.advertised = true; this.notifyStatus();
    });
  }

  private pairingTarget(identity: PairingIdentity): Managed {
    const config = this.configuration, managed = this.managed.get(identity.scope.camera_id);
    if (this.closed || this.failed || this.pending || !config || config.generation !== this.generation ||
        config.revision !== identity.registry_revision || !this.options.session.inventoryComplete ||
        !sameAccount(config.scope, identity.scope) || !sameAccount(config.scope, this.options.session.accountScope) ||
        !managed || managed.camera.isRetired || key(managed.camera.scope) !== key(identity.scope) ||
        managed.camera.identity.accessory_id !== identity.accessory_id || managed.camera.identity.username !== identity.username ||
        !this.options.store) throw new ProtocolError("pairing-not-ready");
    return managed;
  }
  pairingStatus(identity: PairingIdentity): PairingStatus {
    const result: PairingStatus = {...identity, accepted: false, code: "not-ready", published: false,
      paired: null, paired_clients: null, pairing_revision: null, pairing_manifest: null};
    try {
      const managed = this.pairingTarget(identity), snapshot = pairingSnapshot(this.options.store!, identity.username);
      return {...result, accepted: true, code: "ok", published: !this.publicationFenced && managed.published && managed.listening && managed.advertised, paired: snapshot.paired,
        paired_clients: snapshot.paired_clients, pairing_revision: snapshot.pairing_revision, pairing_manifest: snapshot.pairing_manifest};
    } catch {return result;}
  }
  pairingReveal(identity: PairingIdentity & {pairing_revision: string}): PairingReveal {
    const result: PairingReveal = {...identity, accepted: false, code: "not-ready", pincode: null, setup_uri: null, ttl_ms: 0};
    try {
      const managed = this.pairingTarget(identity);
      if (this.publicationFenced || !managed.published || !managed.listening || !managed.advertised) return result;
      const snapshot = pairingSnapshot(this.options.store!, identity.username);
      if (snapshot.pairing_revision !== identity.pairing_revision) return {...result, code: "conflict"};
      if (snapshot.paired || snapshot.pincode === null) return {...result, code: "policy-denied"};
      const uri = managed.camera.accessory.setupURI();
      if (uri.length !== 20 || !/^X-HM:\/\/[0-9A-Z]{13}$/.test(uri) || pairingRevision(this.options.store!.manifest(identity.username)) !== identity.pairing_revision) {
        return {...result, code: "conflict"};
      }
      return {...result, accepted: true, code: "ok", pincode: snapshot.pincode, setup_uri: uri, ttl_ms: 60000};
    } catch {return result;}
  }
  async preparePairingReset(identity: PairingIdentity & {pairing_revision: string}): Promise<PairingReset> {
    const result: PairingReset = {...identity, accepted: false, code: "not-ready", quiesced: false, quiesced_revision: null, pairing_manifest: null};
    let store: PairingStore;
    try {
      this.pairingTarget(identity); store = this.options.store!;
      if (pairingRevision(store.manifest(identity.username)) !== identity.pairing_revision) return {...result, code: "conflict"};
    } catch {return result;}
    // All new acquisition closes before any native or transport cleanup await.
    this.options.session.quiesce?.();
    this.closed = true; this.invalidate();
    for (const managed of this.managed.values()) managed.camera.retire();
    try {
      await this.close(true);
      const manifest = store.manifest(identity.username), revision = pairingRevision(manifest);
      if (revision !== identity.pairing_revision) return {...result, code: "conflict", quiesced: true};
      return {...result, accepted: true, code: "ok", quiesced: true, quiesced_revision: revision, pairing_manifest: manifest};
    } catch {return {...result, quiesced: true};}
    finally {store.close();}
  }

  async configure(page: AccessoryConfigurationPage): Promise<void> {
    if (this.closed || this.failed) throw new ProtocolError("accessory-manager-retired");
    page = structuredClone(page);
    if (!Number.isSafeInteger(page.generation) || page.generation < 1 || page.generation < this.generation ||
        !Number.isSafeInteger(page.revision) || page.revision < 0 || !Number.isSafeInteger(page.page_index) ||
        !Number.isSafeInteger(page.total_pages) || page.total_pages < 1 || page.total_pages > 8 ||
        page.page_index < 0 || page.page_index >= page.total_pages || !Array.isArray(page.entries) || page.entries.length > 32 ||
        typeof page.media_ready !== "boolean" || page.scope === null && page.entries.length !== 0) throw new ProtocolError("invalid-accessory-configuration");
    if (page.generation === this.generation && this.appliedPages.size) {
      if (JSON.stringify(this.appliedPages.get(page.page_index)) !== JSON.stringify(page)) throw new ProtocolError("accessory-configuration-conflict");
      return;
    }
    if (page.generation > this.generation) {
      this.generation = page.generation;
      this.pending = {header: page, pages: new Map()};
      this.appliedPages.clear();
      this.invalidate();
      if (this.configurationTimer) clearTimeout(this.configurationTimer);
      this.configurationTimer = setTimeout(() => {
        this.configurationTimer = null; this.failed = true; this.invalidate();
        this.options.onFailure?.(new ProtocolError("accessory-configuration-timeout"));
      }, 30_000);
    }
    const pending = this.pending;
    if (!pending || pending.header.revision !== page.revision || pending.header.total_pages !== page.total_pages ||
        pending.header.media_ready !== page.media_ready || !sameAccount(pending.header.scope, page.scope)) {
      throw new ProtocolError("accessory-configuration-conflict");
    }
    const previous = pending.pages.get(page.page_index);
    if (previous && JSON.stringify(previous) !== JSON.stringify(page)) throw new ProtocolError("accessory-configuration-conflict");
    pending.pages.set(page.page_index, page);
    if (pending.pages.size !== page.total_pages) return;
    const entries: AccessoryIdentity[] = [];
    for (let index = 0; index < page.total_pages; index++) entries.push(...pending.pages.get(index)!.entries);
    if (entries.length > 256 || new Set(entries.map(value => value.camera_id)).size !== entries.length ||
        new Set(entries.map(value => value.accessory_id)).size !== entries.length ||
        new Set(entries.map(value => value.username)).size !== entries.length) throw new ProtocolError("duplicate-accessory-identity");
    if (entries.length && !this.options.store) throw new ProtocolError("pairing-store-unavailable");
    this.options.store?.preflight();
    this.configuration = {scope: structuredClone(page.scope), revision: page.revision,
      generation: page.generation, entries, media_ready: page.media_ready};
    this.appliedPages = pending.pages; this.pending = null;
    if (this.configurationTimer) clearTimeout(this.configurationTimer);
    this.configurationTimer = null;
    await this.reconcile();
  }

  /** Fence events synchronously before any queued graph mutation or IPC await. */
  invalidate(): void {
    this.publicationFenced = true; this.notifyStatus();
    this.eventToken++; this.eventSynchronized = false; this.states.clear(); this.lastPress.clear();
    for (const managed of this.managed.values()) {
      if (managed.stateTimer) clearTimeout(managed.stateTimer);
      managed.stateTimer = null; managed.camera.applyState(unknownState);
    }
  }

  reconcile(): Promise<void> {
    const work = this.chain.then(() => this.reconcileNow());
    this.chain = work.catch(error => { if (this.closed) return; this.failed = true; this.invalidate();
      this.options.onFailure?.(error instanceof ProtocolError ? error : new ProtocolError("accessory-reconciliation-failed")); });
    return work;
  }
  private async reconcileNow(): Promise<void> {
    if (this.failed) throw new ProtocolError("accessory-cleanup-pending");
    this.publicationFenced = true; this.notifyStatus();
    const config = this.configuration;
    const session = this.options.session;
    if (this.closed || config === null || config.scope === null || !sameAccount(config.scope, session.accountScope)) {
      await this.retireAll(); return;
    }
    if (this.pending || config.generation !== this.generation) return;
    const selected = new Map(config.entries.map(entry => [entry.camera_id, entry]));
    for (const [id, managed] of this.managed) {
      const identity = selected.get(id);
      if (!identity || !sameIdentity(identity, managed.camera.identity)) await this.retire(id, managed);
    }
    if (!session.inventoryComplete) return;
    const cameras = new Map(session.cameras().map(camera => [camera.scope.camera_id, camera]));
    for (const [id, managed] of this.managed) {
      const camera = cameras.get(id);
      if (!camera || !sameAccount(camera.scope, config.scope)) await this.retire(id, managed);
    }
    for (const identity of config.entries) {
      if (this.closed || config !== this.configuration || config.generation !== this.generation) return;
      const camera = cameras.get(identity.camera_id);
      if (!camera || !sameAccount(camera.scope, config.scope)) continue;
      let managed = this.managed.get(identity.camera_id);
      if (managed && (managed.camera.scope.device_generation !== camera.scope.device_generation ||
          !sameAccount(managed.camera.scope, camera.scope))) {
        await this.retire(identity.camera_id, managed); managed = undefined;
      }
      if (managed && key(managed.camera.scope) !== key(camera.scope)) {
        this.invalidate();
        managed.camera.update(camera);
      }
      if (!managed) {
        const native = this.options.session.camera?.(identity.camera_id);
        managed = {camera: new CameraAccessory(identity, camera, {
          ...(native === undefined ? {} : {native}),
          ...(this.options.reportError === undefined ? {} : {reportError: this.options.reportError}),
          failure: () => { this.failed = true; this.invalidate(); this.options.onFailure?.(new ProtocolError("video-cleanup-failed")); },
        }), published: false, listening: false, advertised: false, stateTimer: null};
        this.managed.set(identity.camera_id, managed);
        this.observePublication(identity.camera_id, managed);
      } else {
        managed.camera.update(camera);
        await managed.camera.updateAudio(identity.audio ?? null);
        await managed.camera.updateRecording(identity.recording ?? null);
      }
      await managed.camera.synchronizePolicy();
      if (this.closed || config !== this.configuration || config.generation !== this.generation) return;
      const refreshed = this.options.session.cameras().find(value => value.scope.camera_id === identity.camera_id);
      if (refreshed && refreshed.scope.device_generation === managed.camera.scope.device_generation &&
          sameAccount(refreshed.scope, managed.camera.scope)) managed.camera.update(refreshed);
      if (!managed.published && config.media_ready && this.options.publisher?.mediaReady) {
        const generation = this.generation;
        // Retain the claim even when an asynchronous publish partially fails.
        managed.published = true;
        try { await this.options.publisher.publish(managed.camera); }
        catch (error) {
          // publish may have opened sockets before rejecting. Retain this claim
          // until compensating unpublish succeeds.
          await this.options.publisher.unpublish(managed.camera);
          managed.published = false; managed.listening = false; managed.advertised = false;
          throw error;
        }
        if (this.closed || generation !== this.generation) await this.retire(identity.camera_id, managed);
      }
    }
    this.publicationFenced = false; this.notifyStatus();
    if (!this.closed && this.managed.size && !this.eventSynchronized && !this.syncing) void this.synchronizeEvents().catch(() => {});
  }
  private async retire(id: string, managed: Managed): Promise<void> {
    managed.camera.retire();
    managed.listening = false; managed.advertised = false; this.notifyStatus();
    await managed.camera.closeStreams();
    if (managed.stateTimer) clearTimeout(managed.stateTimer);
    managed.stateTimer = null;
    if (managed.published) {
      if (!this.options.publisher) throw new ProtocolError("accessory-cleanup-pending");
      await this.options.publisher.unpublish(managed.camera);
      managed.published = false;
    }
    this.managed.delete(id); this.states.delete(id); this.lastPress.delete(id);
  }
  private async retireAll(): Promise<void> {
    for (const [id, managed] of this.managed) await this.retire(id, managed);
  }

  async synchronizeEvents(): Promise<void> {
    this.syncAgain = true;
    if (this.syncing) return this.syncing;
    this.syncing = (async () => {
      while (this.syncAgain && !this.closed && !this.failed) {
        this.syncAgain = false;
        const config = this.configuration, token = this.eventToken;
        if (!config?.scope || this.pending || config.generation !== this.generation || !this.options.session.inventoryComplete ||
            !sameAccount(config.scope, this.options.session.accountScope)) return;
        this.eventSynchronized = false;
        const reply = await this.options.syncEvents(config.scope, this.eventEpoch, this.eventBaseline) as unknown as EventSync;
        if (this.closed || token !== this.eventToken || config !== this.configuration) { this.syncAgain = true; continue; }
        if (!reply.accepted || reply.code !== "ok" || !sameAccount(reply.scope, config.scope)) throw new ProtocolError("event-sync-rejected");
        this.eventEpoch = reply.epoch; this.eventBaseline = reply.sequence; this.eventSynchronized = true;
        for (const [id, state] of this.states) if (state.epoch === this.eventEpoch) this.applyState(id, state);
      }
    })().catch(error => { this.eventSynchronized = false;
      for (const managed of this.managed.values()) managed.camera.applyState(unknownState);
      throw error;
    }).finally(() => { this.syncing = null; });
    return this.syncing;
  }

  eventState(value: EventState): void {
    if (this.closed || this.failed) return;
    const managed = this.managed.get(value.scope.camera_id);
    if (!managed || key(managed.camera.scope) !== key(value.scope) || !this.options.session.inventoryComplete) return;
    const previous = this.states.get(value.scope.camera_id);
    if (previous && previous.epoch === value.epoch && value.sequence < previous.sequence) return;
    // Python and Node share Linux CLOCK_MONOTONIC. Receipt-relative TTL alone
    // would revive a stale event after a socket or Node event-loop stall.
    const state: ReceivedEventState = {...structuredClone(value), expiresAt: Math.min(value.expires_at_ms, this.now() + value.ttl_ms)};
    this.states.set(value.scope.camera_id, state);
    if (!state.synchronized) {
      this.eventSynchronized = false; managed.camera.applyState(unknownState);
      void this.synchronizeEvents().catch(() => {});
    } else if (this.eventSynchronized && state.epoch === this.eventEpoch) this.applyState(value.scope.camera_id, state);
  }
  private applyState(id: string, state: ReceivedEventState): void {
    const managed = this.managed.get(id);
    if (!managed || key(managed.camera.scope) !== key(state.scope)) return;
    if (managed.stateTimer) clearTimeout(managed.stateTimer);
    managed.stateTimer = null;
    const remaining = state.motion === true ? state.expiresAt - this.now() : 0;
    // Decide expiry before notifying HAP. A transient true followed by false
    // would still send an obsolete motion alert to Home controllers.
    managed.camera.applyState(state.motion === true && remaining <= 0 ? {...state, motion: false} : state);
    if (state.motion === true) {
      if (remaining > 0) managed.stateTimer = setTimeout(() => {
        managed.stateTimer = null;
        if (this.states.get(id) === state && this.eventSynchronized) managed.camera.applyState({...state, motion: false});
      }, remaining);
    }
  }
  cameraEvent(value: CameraEvent): void {
    if (this.closed || this.failed || !this.eventSynchronized || value.epoch !== this.eventEpoch ||
        value.sequence <= this.eventBaseline || value.ttl_ms < 1 || value.ttl_ms > 2000 ||
        value.expires_at_ms <= this.now() || this.pressIds.has(value.event_id)) return;
    const managed = this.managed.get(value.scope.camera_id), state = this.states.get(value.scope.camera_id);
    if (!managed || !managed.camera.identity.doorbell || key(managed.camera.scope) !== key(value.scope) ||
        !state?.synchronized || state.epoch !== this.eventEpoch || value.sequence <= (this.lastPress.get(value.scope.camera_id) ?? this.eventBaseline)) return;
    this.lastPress.set(value.scope.camera_id, value.sequence); this.pressIds.add(value.event_id);
    if (this.pressIds.size > 512) this.pressIds.delete(this.pressIds.values().next().value!);
    managed.camera.ring();
  }

  close(preserveStore = false): Promise<void> {
    if (this.closing) return this.closing;
    this.closed = true; this.invalidate();
    if (this.configurationTimer) clearTimeout(this.configurationTimer);
    this.configurationTimer = null;
    this.closing = (async () => {
      await this.chain;
      await this.retireAll();
      if (!preserveStore) this.options.store?.close();
    })();
    return this.closing;
  }
}
