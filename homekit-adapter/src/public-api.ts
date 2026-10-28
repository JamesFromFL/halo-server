import {
  Accessory, CameraController, DoorbellController, HAPLibraryVersion, HAPStorage,
  Service, Characteristic,
} from "@homebridge/hap-nodejs";
import type {
  CameraStreamingDelegate, CameraRecordingDelegate,
} from "@homebridge/hap-nodejs";

export type PublicCameraDelegates = {
  streaming: CameraStreamingDelegate;
  recording: CameraRecordingDelegate;
};

/** Import/compile gate only; no publication, pair store or native media is started. */
export function verifyPublicApi(): string {
  if ([Accessory, CameraController, DoorbellController, HAPStorage.setCustomStoragePath,
    Service.MotionSensor, Characteristic.MotionDetected].some(value => typeof value !== "function")) {
    throw new Error("hap-public-api-unavailable");
  }
  const version = HAPLibraryVersion();
  if (version !== "2.2.3") throw new Error("hap-version-mismatch");
  return version;
}
