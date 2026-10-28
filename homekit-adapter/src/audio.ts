import { Accessory, CameraController, Characteristic, HAPStatus, Service } from "@homebridge/hap-nodejs";
import { ProtocolError, type AccessoryAudio, type AudioSettings } from "./protocol.js";

export const AUDIO_SETTINGS_TIMEOUT_MS = 7_000;
const settings = (value: AccessoryAudio): AudioSettings => ({microphone_muted: value.microphone_muted,
  speaker_muted: value.speaker_muted, microphone_volume: value.microphone_volume, speaker_volume: value.speaker_volume});
const equalSettings = (left: AudioSettings, right: AudioSettings): boolean =>
  (Object.keys(left) as (keyof AudioSettings)[]).every(key => left[key] === right[key]);

export interface AudioControlOptions {
  fence: () => Promise<void>;
  persist: (expectedRevision: number, value: AudioSettings) => Promise<number>;
  synchronize: () => Promise<void>;
}

/** Public characteristic handlers delay successful writes until durable CAS acknowledgement. */
export class AudioControls {
  private state: AccessoryAudio;
  private blocked = false;
  private retired = false;
  private pending: Promise<void> | null = null;
  private cancelWrite: (() => void) | null = null;
  constructor(accessory: Accessory, private readonly controller: CameraController,
    initial: AccessoryAudio, private readonly options: AudioControlOptions) {
    this.state = structuredClone(initial);
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
            typeof value !== "number" || !Number.isSafeInteger(value) || value < 0 || value > 100) throw HAPStatus.INVALID_VALUE_IN_REQUEST;
          try { await this.write(field, value as boolean | number); }
          catch { throw HAPStatus.SERVICE_COMMUNICATION_FAILURE; }
        });
      }
    }
  }
  get current(): AccessoryAudio { return structuredClone(this.state); }
  get available(): boolean { return !this.blocked && !this.retired; }
  get changing(): Promise<void> | null { return this.pending; }

  /** Parent pages are the durable authority, including recovery after uncertain writes. */
  update(value: AccessoryAudio): Promise<void> {
    if (this.retired || value.revision < this.state.revision) return Promise.resolve();
    if (value.microphone !== this.state.microphone || value.speaker !== this.state.speaker ||
      value.revision === this.state.revision && !equalSettings(value, this.state)) throw new ProtocolError("audio-settings-conflict");
    const changed = !equalSettings(value, this.state);
    if (changed) this.blocked = true;
    const cleanup = changed ? this.options.fence() : Promise.resolve();
    this.state = structuredClone(value); this.apply();
    return cleanup.then(() => { if (!this.pending && this.state.revision === value.revision) this.blocked = false; });
  }
  private apply(): void {
    this.controller.setMicrophoneMuted(this.state.microphone_muted);
    this.controller.setMicrophoneVolume(this.state.microphone_volume);
    this.controller.setSpeakerMuted(this.state.speaker_muted);
    this.controller.setSpeakerVolume(this.state.speaker_volume);
  }
  private write(field: keyof AudioSettings, value: boolean | number): Promise<void> {
    if (this.retired || this.pending) return Promise.reject(new ProtocolError("audio-settings-busy"));
    const next = {...settings(this.state), [field]: value};
    if (equalSettings(next, this.state) && !this.blocked) return Promise.resolve();
    this.blocked = true;
    // fence() retires local sessions synchronously, before either IPC or disk awaits.
    const cleanup = this.options.fence(), expected = this.state.revision;
    const saved = this.options.persist(expected, next);
    const operation = Promise.all([cleanup, saved]).then(([, revision]) => revision);
    void operation.catch(() => {});
    let timer: NodeJS.Timeout | undefined;
    const deadline = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new ProtocolError("audio-settings-timeout")), AUDIO_SETTINGS_TIMEOUT_MS);
    });
    const cancelled = new Promise<never>((_resolve, reject) => {
      this.cancelWrite = () => reject(new ProtocolError("audio-settings-retired"));
    });
    this.pending = (async () => {
      try {
        const revision = await Promise.race([operation, deadline, cancelled]);
        if (this.retired || revision < this.state.revision || revision === this.state.revision && !equalSettings(next, this.state)) {
          throw new ProtocolError("audio-settings-retired");
        }
        this.state = {...this.state, ...next, revision}; this.apply();
        this.blocked = false;
        await this.options.synchronize();
      } catch (error) { this.blocked = true; throw error; }
      finally { if (timer) clearTimeout(timer); this.pending = null; this.cancelWrite = null; }
    })();
    return this.pending;
  }
  retire(): void { this.retired = true; this.blocked = true; this.cancelWrite?.(); }
  settle(): Promise<void> { return this.pending?.catch(() => {}) ?? Promise.resolve(); }
}
