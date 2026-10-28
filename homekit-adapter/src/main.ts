import { randomUUID } from "node:crypto";
import { fstatSync } from "node:fs";
import { Socket } from "node:net";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { ControlChannel } from "./ipc.js";
import { BulkChannel } from "./bulk.js";
import { AdapterSession } from "./session.js";
import { AccessoryManager, hapPublisher, type AccessoryConfigurationPage } from "./accessories.js";
import { PairingStore } from "./storage.js";
import { FEATURES, ProtocolError, validateDirection, type Envelope } from "./protocol.js";
import { verifyPublicApi } from "./public-api.js";

export const HANDSHAKE_TIMEOUT_MS = 5_000;
export const IDLE_TIMEOUT_MS = 15_000;

/** Ending closes admission, while already owned cleanup RPCs must still settle. */
export async function receiveTerminalCleanup(session: AdapterSession, boot: string, message: Envelope): Promise<boolean> {
  if (message.boot_id !== boot || !["video_stop_result", "lease_stop_result"].includes(message.kind)) return false;
  return session.receive(message);
}

/** The inherited lock shares Python's flock ownership until this process exits. */
export function pairingDescriptors(environment: NodeJS.ProcessEnv, control: number, bulk: number): {pairing?: number; pairingLock?: number} {
  const directory = environment.HALO_PAIRING_FD, lock = environment.HALO_PAIRING_LOCK_FD;
  if (directory === undefined && lock === undefined) return {};
  const descriptor = (value: string | undefined): number => {
    if (!value || !/^[1-9][0-9]*$/.test(value)) throw new ProtocolError("invalid-bootstrap");
    const parsed = Number(value);
    if (!Number.isSafeInteger(parsed) || parsed < 3) throw new ProtocolError("invalid-bootstrap");
    return parsed;
  };
  const pairing = descriptor(directory), pairingLock = descriptor(lock);
  if (new Set([control, bulk, pairing, pairingLock]).size !== 4 || !fstatSync(pairing).isDirectory()) {
    throw new ProtocolError("invalid-bootstrap");
  }
  const metadata = fstatSync(pairingLock);
  if (!metadata.isFile() || metadata.uid !== process.getuid?.() || metadata.nlink !== 1 || (metadata.mode & 0o777) !== 0o600) {
    throw new ProtocolError("invalid-bootstrap");
  }
  // Intentionally retain pairingLock until process exit, including failed cleanup.
  return {pairing, pairingLock};
}

export function inheritedDescriptors(environment: NodeJS.ProcessEnv, parentPid: number): {
  control: number; bulk: number; parent: number; pairing?: number; pairingLock?: number;
} {
  if (["NODE_OPTIONS", "NODE_PATH"].some(name => Boolean(environment[name]))) {
    throw new ProtocolError("runtime-injection-forbidden");
  }
  const integer = (name: string): number => {
    const value = environment[name];
    if (!value || !/^[1-9][0-9]*$/.test(value)) throw new ProtocolError("invalid-bootstrap");
    const parsed = Number(value);
    if (!Number.isSafeInteger(parsed)) throw new ProtocolError("invalid-bootstrap");
    return parsed;
  };
  const control = integer("HALO_CONTROL_FD");
  const bulk = integer("HALO_BULK_FD");
  const parent = integer("HALO_PARENT_PID");
  if (control < 3 || bulk < 3 || control === bulk || parent !== parentPid) {
    throw new ProtocolError("invalid-bootstrap");
  }
  if (!fstatSync(control).isSocket() || !fstatSync(bulk).isSocket()) {
    throw new ProtocolError("invalid-bootstrap");
  }
  return { control, bulk, parent, ...pairingDescriptors(environment, control, bulk) };
}

/** Supervised graph/events only; real publication remains gated by live output. */
async function run(): Promise<number> {
  if (process.version !== "v26.12.1") throw new ProtocolError("node-version-mismatch");
  const descriptors = inheritedDescriptors(process.env, process.ppid);
  const store = descriptors.pairing === undefined ? undefined : new PairingStore(descriptors.pairing);
  verifyPublicApi();
  const control = new Socket({ fd: descriptors.control, readable: true, writable: true });
  const bulk = new Socket({ fd: descriptors.bulk, readable: true, writable: true });
  const channel = new ControlChannel(control);
  let boot: string | null = null;
  let session: AdapterSession | null = null;
  let transfer: BulkChannel | null = null;
  let accessories: AccessoryManager | null = null;
  let ending = false;
  let failed = false;
  let idle: NodeJS.Timeout | null = null;
  const hello = setTimeout(() => fail("handshake-timeout"), HANDSHAKE_TIMEOUT_MS);
  const parentWatch = setInterval(() => {
    if (process.ppid !== descriptors.parent) fail("parent-retired");
  }, 5_000);
  const renewIdle = (): void => {
    if (idle !== null) clearTimeout(idle);
    idle = setTimeout(() => fail("parent-idle"), IDLE_TIMEOUT_MS);
  };
  const fail = (code: string): void => {
    if (ending) return;
    failed = true;
    ending = true;
    // Fixed protocol code only; never log peer payloads, keys or arbitrary errors.
    process.stderr.write(`Halo HAP adapter: ${code}\n`);
    session?.close();
    accessories?.invalidate();
    void accessories?.close().catch(() => {});
    void transfer?.close("cancelled").catch(() => {});
    channel.close();
  };
  const reply = (message: Envelope, kind: Envelope["kind"], payload = {}): Promise<void> =>
    channel.send({ ...message, kind, payload });

  channel.on("message", (message: Envelope) => {
    void (async () => {
      if (ending) {
        if (session && boot) await receiveTerminalCleanup(session, boot, message);
        return;
      }
      validateDirection(message, "python");
      if (boot === null) {
        if (message.kind !== "hello") throw new ProtocolError("hello-required");
        boot = message.boot_id;
        clearTimeout(hello);
        renewIdle();
        await reply(message, "hello_ack", {
          role: "hap", build_id: message.payload.build_id, protocol: 1,
          features: [...FEATURES], pid: process.pid,
        });
        transfer = new BulkChannel(bulk, {
          bootId: boot, role: "hap",
          sendControl: async (kind, payload, requestId) => {
            if (boot === null) throw new ProtocolError("session-closed");
            await channel.send({v: 1, kind, boot_id: boot,
              request_id: requestId ?? randomUUID(), payload});
          },
          onFailure: () => fail("bulk-failed"),
        });
        session = new AdapterSession(boot, {send: message => channel.send(message), bulk: transfer,
          onFailure: () => fail("session-failed"),
          onInventory: () => { void accessories?.reconcile().catch(() => fail("accessory-reconciliation-failed")); },
          onInvalidate: () => {
            accessories?.invalidate();
            void accessories?.reconcile().catch(() => fail("accessory-reconciliation-failed"));
          }});
        const adapterSession = session;
        accessories = new AccessoryManager({session: adapterSession,
          ...(store === undefined ? {} : {store, publisher: hapPublisher(store)}),
          syncEvents: (scope, epoch, sequence) => adapterSession.synchronizeEvents(scope, epoch, sequence),
          onFailure: () => fail("accessory-reconciliation-failed")});
        transfer.start();
        return;
      }
      if (message.boot_id !== boot) throw new ProtocolError("stale-boot");
      renewIdle();
      if (message.kind === "hello") throw new ProtocolError("duplicate-hello");
      if (message.kind === "ping") {
        await reply(message, "pong");
      } else if (message.kind === "shutdown") {
        ending = true;
        accessories?.invalidate();
        await accessories?.close();
        session?.close();
        await transfer?.close("cancelled");
        await reply(message, "shutdown_ack");
        channel.close();
      } else if (message.kind === "accessory_config") {
        if (!accessories) throw new ProtocolError("session-not-ready");
        let accepted = false;
        try { await accessories.configure(message.payload as unknown as AccessoryConfigurationPage); accepted = true; }
        catch (error) {
          if (!(error instanceof ProtocolError)) throw error;
        }
        await reply(message, "accessory_config_ack", {
          scope: message.payload.scope, revision: message.payload.revision,
          generation: message.payload.generation, page_index: message.payload.page_index,
          accepted, code: accepted ? "ok" : "not-ready",
        });
      } else if (message.kind === "event_state") {
        accessories?.eventState(message.payload as unknown as Parameters<AccessoryManager["eventState"]>[0]);
      } else if (message.kind === "camera_event") {
        accessories?.cameraEvent(message.payload as unknown as Parameters<AccessoryManager["cameraEvent"]>[0]);
      } else {
        if (!session) throw new ProtocolError("session-not-ready");
        await session.receive(message);
      }
    })().catch(error => {
      // Shutdown write failure must still close instead of waiting for idle expiry.
      if (ending) { failed = true; channel.close(); }
      else fail(error instanceof ProtocolError ? error.code : "control-failed");
    });
  });
  channel.on("failure", () => { failed = true; });
  bulk.on("error", () => fail("bulk-io"));
  bulk.on("end", () => fail("bulk-retired"));
  const signal = (): void => {
    ending = true; accessories?.invalidate(); session?.close();
    void accessories?.close().catch(() => {});
    void transfer?.close("cancelled").catch(() => {}); channel.close();
  };
  process.once("SIGTERM", signal);
  process.once("SIGINT", signal);
  await new Promise<void>(resolve => channel.once("closed", resolve));
  clearTimeout(hello);
  clearInterval(parentWatch);
  if (idle !== null) clearTimeout(idle);
  (session as AdapterSession | null)?.close();
  await (accessories as AccessoryManager | null)?.close();
  if (accessories === null) store?.close();
  await (transfer as BulkChannel | null)?.close("cancelled");
  bulk.destroy();
  process.removeListener("SIGTERM", signal);
  process.removeListener("SIGINT", signal);
  return failed ? 1 : 0;
}

// Importing helpers in tests must not launch a process or attach descriptors.
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  void run().then(code => { process.exitCode = code; }).catch(() => {
    process.stderr.write("Halo HAP adapter: bootstrap-failed\n");
    process.exitCode = 1;
  });
}
