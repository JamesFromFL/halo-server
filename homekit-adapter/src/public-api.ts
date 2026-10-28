import {
  Accessory, CameraController, DoorbellController, HAPLibraryVersion, HAPStorage,
  Service, Characteristic, PhysicalConnectionBudget, DataStreamConnection, HAPStorageError,
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
    HAPStorage.setFailureHandler, HAPStorage.setDurabilityGuard, HAPStorageError,
    Service.MotionSensor, Characteristic.MotionDetected, PhysicalConnectionBudget,
    DataStreamConnection.prototype.sendEventAsync, DataStreamConnection.prototype.abortAndWait,
    CameraController.prototype.closeRecordingStreams].some(value => typeof value !== "function")) {
    throw new Error("hap-public-api-unavailable");
  }
  const version = HAPLibraryVersion();
  if (version !== "2.2.3-halo.2") throw new Error("hap-version-mismatch");
  return version;
}
