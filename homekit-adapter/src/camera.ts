import {
  Accessory, CameraController, Categories, Characteristic, DoorbellController,
  H264Level, H264Profile, HAPStatus, Service, SRTPCryptoSuites, StreamRequestTypes,
  type CameraStreamingDelegate,
} from "@homebridge/hap-nodejs";
import type { MediaScope } from "./protocol.js";
import type { NativeCamera, CameraDelegate } from "./session.js";
import { HapVideoDelegate } from "./video.js";

export interface AccessoryIdentity { camera_id: string; accessory_id: string; username: string; doorbell: boolean }
export interface MotionState { motion: boolean | null; synchronized: boolean; source_state: "connected" | "connecting" | "offline" }

/** These delegates intentionally expose no RTP endpoints or synthetic image bytes. */
export function unavailableStreamingDelegate(): CameraStreamingDelegate {
  return {
    handleSnapshotRequest(_request, callback) { callback(HAPStatus.SERVICE_COMMUNICATION_FAILURE); },
    prepareStream(_request, callback) { callback(new Error("media-not-ready")); },
    handleStreamRequest(request, callback) {
      if (request.type === StreamRequestTypes.STOP) callback();
      else callback(new Error("media-not-ready"));
    },
  };
}

/** Independent camera graph; Python gates publication on its native output preflight. */
export class CameraAccessory {
  readonly accessory: Accessory;
  readonly controller: CameraController;
  private current: NativeCamera;
  private retired = false;
  readonly streamingDelegate: HapVideoDelegate | null;

  constructor(readonly identity: AccessoryIdentity, camera: NativeCamera, options: {native?: CameraDelegate; failure?: () => void} = {}) {
    this.current = structuredClone(camera);
    const name = camera.name || (identity.doorbell ? "Ring Doorbell" : "Ring Camera");
    this.accessory = new Accessory(name, identity.accessory_id);
    this.accessory.category = identity.doorbell ? Categories.VIDEO_DOORBELL : Categories.IP_CAMERA;
    this.accessory.getService(Service.AccessoryInformation)!
      .setCharacteristic(Characteristic.Manufacturer, "Ring via Halo")
      .setCharacteristic(Characteristic.Model, camera.model || "Ring Camera")
      .setCharacteristic(Characteristic.SerialNumber, identity.accessory_id)
      .setCharacteristic(Characteristic.FirmwareRevision, "0.1.0");
    this.accessory.on("identify", (_paired, callback) => callback());
    this.streamingDelegate = options.native ? new HapVideoDelegate(options.native, {
      active: () => !this.retired && this.streamingActive(),
      forceStop: hapId => this.controller.forceStopStreamingSession(hapId),
      failure: options.failure ?? (() => {}),
    }) : null;
    const controllerOptions = {
      cameraStreamCount: 2,
      delegate: this.streamingDelegate ?? unavailableStreamingDelegate(),
      streamingOptions: {
        supportedCryptoSuites: [SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80],
        video: {codec: {profiles: [H264Profile.BASELINE, H264Profile.MAIN, H264Profile.HIGH],
          levels: [H264Level.LEVEL3_1, H264Level.LEVEL3_2, H264Level.LEVEL4_0]},
          resolutions: [[320, 240, 15], [640, 360, 30], [640, 480, 30], [1280, 720, 30], [1920, 1080, 30]] as [number, number, number][]},
      },
      sensors: {motion: true},
    };
    this.controller = identity.doorbell ? new DoorbellController({...controllerOptions, name}) : new CameraController(controllerOptions);
    this.accessory.configureController(this.controller);
    for (const service of this.accessory.services.filter(service => service.UUID === Service.CameraRTPStreamManagement.UUID)) {
      service.getCharacteristic(Characteristic.Active).on("change", () => {
        void this.streamingDelegate?.activeChanged().catch(() => options.failure?.());
      });
    }
    this.applyState({motion: null, synchronized: false, source_state: "connecting"});
  }

  get scope(): MediaScope { return {...this.current.scope}; }
  get isRetired(): boolean { return this.retired; }
  private streamingActive(): boolean {
    const services = this.accessory.services.filter(service => service.UUID === Service.CameraRTPStreamManagement.UUID);
    return services.length > 0 && services.every(service => service.getCharacteristic(Characteristic.Active).value === true ||
      service.getCharacteristic(Characteristic.Active).value === 1);
  }
  synchronizePolicy(): Promise<void> { return this.streamingDelegate?.synchronizePolicy() ?? Promise.resolve(); }
  closeStreams(): Promise<void> { return this.streamingDelegate?.close() ?? Promise.resolve(); }
  update(camera: NativeCamera): void {
    if (this.retired) return;
    this.current = structuredClone(camera);
    const name = camera.name || (this.identity.doorbell ? "Ring Doorbell" : "Ring Camera");
    this.accessory.displayName = name;
    this.accessory.getService(Service.AccessoryInformation)!
      .updateCharacteristic(Characteristic.Name, name)
      .updateCharacteristic(Characteristic.Model, camera.model || "Ring Camera");
  }
  applyState(state: MotionState): void {
    const active = !this.retired && state.synchronized && state.source_state === "connected" && state.motion !== null;
    const sensor = this.controller.motionService!;
    sensor.updateCharacteristic(Characteristic.MotionDetected, active && state.motion === true);
    sensor.updateCharacteristic(Characteristic.StatusActive, active);
    sensor.updateCharacteristic(Characteristic.StatusFault,
      active ? Characteristic.StatusFault.NO_FAULT : Characteristic.StatusFault.GENERAL_FAULT);
  }
  ring(): void {
    if (!this.retired && this.controller instanceof DoorbellController) this.controller.ringDoorbell();
  }
  retire(): void {
    this.retired = true;
    this.applyState({motion: null, synchronized: false, source_state: "offline"});
  }
}
