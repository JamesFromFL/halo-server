import { Accessory, CameraController, Characteristic, HAPStatus, Service } from "@homebridge/hap-nodejs";
import { ProtocolError, type AccessoryAudio, type AudioSettings } from "./protocol.js";

export const AUDIO_SETTINGS_TIMEOUT_MS = 7_000;
const settings = (value: AccessoryAudio): AudioSettings => ({microphone_muted: value.microphone_muted,
  speaker_muted: value.speaker_muted, microphone_volume: value.microphone_volume, speaker_volume: value.speaker_volume});
const equalSettings = (left: AudioSettings, right: AudioSettings): boolean =>
  left.microphone_muted === right.microphone_muted && left.speaker_muted === right.speaker_muted &&
  left.microphone_volume === right.microphone_volume && left.speaker_volume === right.speaker_volume;

export interface AudioControlOptions {
  fence: () => Promise<void>;
  persist: (expectedRevision: number, value: AudioSettings) => Promise<number>;
  synchronize: () => Promise<void>;
  reportError?: ((stage: string, code: string) => void) | undefined;
}

/** Public characteristic handlers delay successful writes until durable CAS acknowledgement. */
export class AudioControls {
  private state: AccessoryAudio;
  private desired: AudioSettings;
  private blocked = false;
  private retired = false;
  private pending: Promise<void> | null = null;
  private cancelWrite: ((error?: ProtocolError) => void) | null = null;
  private inFlight: {expected: number; next: AudioSettings} | null = null;
  private token: object | null = null;
  private owned: Promise<void> | null = null;
  private fencing: Promise<void> | null = null;
  private fenceFailed = false;
  private fenceFailure: unknown = null;
  private forcePersist = false;
  private stage = "persistence-failed";
  constructor(accessory: Accessory, private readonly controller: CameraController,
    initial: AccessoryAudio, private readonly options: AudioControlOptions) {
    this.state = structuredClone(initial);
    this.desired = settings(initial);
    this.apply();
    for (const [serviceType, mute, volume] of [
      [Service.Microphone, "microphone_muted", "microphone_volume"],
      [Service.Speaker, "speaker_muted", "speaker_volume"],
    ] as const) {
      const service = accessory.getService(serviceType);
      if (!service) continue;
      for (const [characteristic, field] of [[Characteristic.Mute, mute], [Characteristic.Volume, volume]] as const) {
        // CameraController uses the released legacy event API. Our public async
        // onSet handler owns persistence; public setters keep its getters current.
        service.getCharacteristic(characteristic).removeAllListeners("set").onSet(async value => {
          if (field.endsWith("muted") ? typeof value !== "boolean" :
            typeof value !== "number" || !Number.isSafeInteger(value) || value < 0 || value > 100) {
            this.report("invalid-value"); throw HAPStatus.INVALID_VALUE_IN_REQUEST;
          }
          try { await this.write(field, value as boolean | number); }
          catch { throw HAPStatus.SERVICE_COMMUNICATION_FAILURE; }
        });
      }
    }
  }
  get current(): AccessoryAudio { return structuredClone(this.state); }
  get available(): boolean { return !this.blocked && !this.retired; }
  get changing(): Promise<void> | null { return this.pending; }
  private report(code: string): void {
    try { this.options.reportError?.("audio-settings", code); } catch {}
  }

  private startFence(): void {
    this.blocked = true;
    if (this.fencing) return;
    this.fenceFailed = false; this.fenceFailure = null;
    try {
      const operation = this.options.fence();
      this.fencing = operation;
      void operation.then(() => {
        if (this.fencing === operation) this.fencing = null;
      }, error => {
        this.fenceFailed = true; this.fenceFailure = error;
        if (this.fencing === operation) this.fencing = null;
      });
    } catch (error) { this.fenceFailed = true; this.fenceFailure = error; }
  }

  /** Parent pages are the durable authority, including recovery after uncertain writes. */
  update(value: AccessoryAudio): Promise<void> {
    if (this.retired || value.revision < this.state.revision) return Promise.resolve();
    if (value.microphone !== this.state.microphone || value.speaker !== this.state.speaker ||
      value.revision === this.state.revision && !equalSettings(value, this.state)) throw new ProtocolError("audio-settings-conflict");
    const changed = !equalSettings(value, this.state);
    if (changed) this.startFence();
    if (this.token && changed && !(this.inFlight && value.revision === this.inFlight.expected + 1 &&
        equalSettings(value, this.inFlight.next))) {
      // An independently changed durable row supersedes the entire pending
      // full-snapshot write. Never rebase its old unmute values onto that new
      // revision. Our own pre-reply page is accepted only with exact CAS and
      // settings correlation.
      this.token = null;
      this.cancelWrite?.(new ProtocolError("audio-settings-conflict"));
    }
    const cleanup = this.fencing ?? Promise.resolve();
    this.state = structuredClone(value); this.apply();
    if (!this.token) { this.desired = settings(value); this.forcePersist = false; }
    return cleanup.then(() => {
      if (this.fenceFailed) throw this.fenceFailure;
      if (!this.pending && !this.owned && this.state.revision === value.revision) this.blocked = false;
    });
  }
  private apply(): void {
    this.controller.setMicrophoneMuted(this.state.microphone_muted);
    this.controller.setMicrophoneVolume(this.state.microphone_volume);
    this.controller.setSpeakerMuted(this.state.speaker_muted);
    this.controller.setSpeakerVolume(this.state.speaker_volume);
  }
  private write(field: keyof AudioSettings, value: boolean | number): Promise<void> {
    if (this.retired) return Promise.reject(new ProtocolError("audio-settings-retired"));
    if (!this.token && this.owned) return Promise.reject(new ProtocolError("audio-settings-cleanup-pending"));
    this.desired = {...this.desired, [field]: value};
    if (this.token) {
      // A newly revoking intent fences synchronously, even while the previous
      // durable state is being installed into aggregate camera policy.
      if (!equalSettings(this.desired, this.state) && !this.blocked) this.startFence();
      return this.pending!;
    }
    if (equalSettings(this.desired, this.state) && !this.blocked) return Promise.resolve();
    this.forcePersist = this.blocked;
    this.startFence();
    const token = {}; this.token = token;
    let timer: NodeJS.Timeout | undefined;
    const deadline = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new ProtocolError("audio-settings-timeout")), AUDIO_SETTINGS_TIMEOUT_MS);
    });
    const cancelled = new Promise<never>((_resolve, reject) => {
      this.cancelWrite = error => reject(error ?? new ProtocolError("audio-settings-retired"));
    });
    const operation = Promise.resolve().then(() => this.flush(token));
    this.owned = operation;
    void operation.then(() => { if (this.owned === operation) this.owned = null; },
      () => { if (this.owned === operation) this.owned = null; });
    const pending = Promise.race([operation, deadline, cancelled]).catch(error => {
      if (this.token === token) {
        this.token = null; this.blocked = true; this.desired = settings(this.state);
      }
      this.report(error instanceof ProtocolError && error.code === "audio-settings-timeout" ? "timeout" :
        error instanceof ProtocolError && error.code === "audio-settings-retired" ? "retired" :
        error instanceof ProtocolError && error.code === "audio-settings-conflict" ? "conflict" : this.stage);
      throw error;
    }).finally(() => {
      if (timer) clearTimeout(timer);
      if (this.pending === pending) { this.pending = null; this.cancelWrite = null; }
    });
    this.pending = pending;
    return this.pending;
  }

  private assertCurrent(token: object): void {
    if (this.retired || this.token !== token) throw new ProtocolError("audio-settings-retired");
  }
  private async flush(token: object): Promise<void> {
    while (true) {
      this.assertCurrent(token);
      while (this.forcePersist || !equalSettings(this.desired, this.state)) {
        const next = {...this.desired}, expected = this.state.revision;
        this.forcePersist = false; this.stage = "persistence-failed";
        const flight = {expected, next}; this.inFlight = flight;
        let revision: number;
        try { revision = await this.options.persist(expected, next); }
        finally { if (this.inFlight === flight) this.inFlight = null; }
        this.assertCurrent(token);
        if (revision < this.state.revision || revision === this.state.revision && !equalSettings(next, this.state)) {
          this.stage = "conflict"; throw new ProtocolError("audio-settings-conflict");
        }
        this.state = {...this.state, ...next, revision}; this.apply();
      }
      this.stage = "cleanup-failed";
      if (this.fencing) await this.fencing;
      this.assertCurrent(token);
      if (this.fenceFailed) throw this.fenceFailure;
      this.blocked = false; this.stage = "policy-failed";
      await this.options.synchronize(); this.assertCurrent(token);
      if (!equalSettings(this.desired, this.state) || this.blocked || this.fencing) continue;
      // Complete admission synchronously before resolving callbacks. A new
      // request after this point owns a new bounded operation, not an intent
      // accidentally acknowledged in the old promise's finalizer window.
      this.token = null; this.pending = null; this.cancelWrite = null; this.owned = null;
      return;
    }
  }
  retire(): void { this.retired = true; this.blocked = true; this.cancelWrite?.(); }
  settle(): Promise<void> { return this.pending?.catch(() => {}) ?? Promise.resolve(); }
}
