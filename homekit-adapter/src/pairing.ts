import { AccessoryInfo } from "@homebridge/hap-nodejs";
import { ProtocolError, type PairingManifest } from "./protocol.js";
import { pairingRevision, type PairingStore } from "./storage.js";

export interface PairingSnapshot {
  pairing_revision: string; pairing_manifest: PairingManifest; paired: boolean; paired_clients: number; pincode: string | null;
}
/** Public HAP models only; a snapshot does not publish or change an accessory. */
export function pairingSnapshot(store: PairingStore, username: string): PairingSnapshot {
  store.initialize();
  const manifest = store.manifest(username), revision = pairingRevision(manifest);
  const information = AccessoryInfo.load(username);
  if ((information === null) !== (manifest.accessory_info === null)) throw new ProtocolError("pairing-state-conflict");
  const paired = information?.paired() ?? false, count = information?.listPairings().length ?? 0;
  if (paired !== (count > 0) || count > 256 || pairingRevision(store.manifest(username)) !== revision) {
    throw new ProtocolError("pairing-state-conflict");
  }
  return {pairing_revision: revision, pairing_manifest: manifest, paired, paired_clients: count, pincode: information?.pincode ?? null};
}
