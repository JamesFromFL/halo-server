import { HAPStorage } from "@homebridge/hap-nodejs";
import { STORAGE_RECOVERY_REQUIRED } from "./storage.js";

/** Configure before any HAP storage access. Only process exit retires all cached model holders. */
export function enablePairingDurability(fence: () => void): void {
  HAPStorage.setDurabilityGuard(true);
  HAPStorage.setFailureHandler(() => {
    try {fence();} finally {
      // A fixed code only: never leak disk errors, paths, records or pairing keys.
      try {process.stderr.write(`Halo HAP adapter: ${STORAGE_RECOVERY_REQUIRED}\n`);}
      finally {process.exit(1);}
    }
  });
}
