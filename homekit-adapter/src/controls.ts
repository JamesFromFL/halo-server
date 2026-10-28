import { Characteristic, HAPStatus, HapStatusError, Service, type Accessory } from "@homebridge/hap-nodejs";
import type { CameraDelegate, NativeCamera } from "./session.js";

type Control = "light" | "siren";
/** Capability-gated public HAP services. Construct/restore/update never actuates Ring. */
export class CameraControls {
  private readonly services = new Map<Control, Service>();
  private retired = false;
  constructor(private readonly accessory: Accessory, camera: NativeCamera, private readonly native: CameraDelegate | undefined,
    private readonly active: () => boolean, private readonly synchronize: () => Promise<void>) {this.update(camera);}
  get available(): boolean {return this.services.size > 0;}
  retire(): void {this.retired = true;}
  update(camera: NativeCamera): void {
    for (const control of ["light", "siren"] as const) {
      const supported = camera.capabilities[control] && camera.capabilities.evidence.some(value => value === `sdk:${control}` || value === `halo:${control}`);
      const existing = this.services.get(control);
      if (!supported) {if (existing) {this.accessory.removeService(existing);this.services.delete(control);} continue;}
      if (existing) continue;
      const service = control === "light" ? new Service.Lightbulb("Camera Light", "halo-light") : new Service.Switch("Camera Siren", "halo-siren");
      service.addOptionalCharacteristic(Characteristic.StatusFault);
      service.updateCharacteristic(Characteristic.StatusFault, Characteristic.StatusFault.GENERAL_FAULT);
      service.getCharacteristic(Characteristic.On).onGet(async () => {
        try {
          this.admit(control, service);await this.synchronize();this.admit(control, service);
          const state = await this.native!.controlState();this.admit(control, service);
          const value = state[control];
          if (value === null) throw new Error("unknown");
          service.updateCharacteristic(Characteristic.StatusFault, Characteristic.StatusFault.NO_FAULT);
          return value;
        } catch {
          service.updateCharacteristic(Characteristic.StatusFault, Characteristic.StatusFault.GENERAL_FAULT);
          throw new HapStatusError(HAPStatus.SERVICE_COMMUNICATION_FAILURE);
        }
      }).onSet(async value => {
        try {
          if (typeof value !== "boolean" && value !== 0 && value !== 1) throw new Error("invalid");
          this.admit(control, service);await this.synchronize();this.admit(control, service);
          const result = await this.native!.setControl(control, value === true || value === 1);this.admit(control, service);
          if (result !== "accepted") throw new Error("uncertain");
          const state = await this.native!.controlState();this.admit(control, service);
          if (state[control] === null || state[control] !== (value === true || value === 1)) throw new Error("unconfirmed");
          service.updateCharacteristic(Characteristic.StatusFault, Characteristic.StatusFault.NO_FAULT);
        } catch {
          service.updateCharacteristic(Characteristic.StatusFault, Characteristic.StatusFault.GENERAL_FAULT);
          throw new HapStatusError(HAPStatus.SERVICE_COMMUNICATION_FAILURE);
        }
      });
      this.services.set(control, service);this.accessory.addService(service);
    }
  }
  private admit(control: Control, service: Service): void {
    if (this.retired || !this.active() || !this.native || this.services.get(control) !== service) throw new Error("control-unavailable");
  }
}
