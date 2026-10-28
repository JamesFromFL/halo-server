import { randomUUID } from "node:crypto";
import { fstatSync } from "node:fs";
import { Socket } from "node:net";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { ControlChannel } from "./ipc.js";
import { BulkChannel } from "./bulk.js";
import { AdapterSession } from "./session.js";
import { FEATURE, ProtocolError, type Envelope } from "./protocol.js";
import { verifyPublicApi } from "./public-api.js";

export const HANDSHAKE_TIMEOUT_MS = 5_000;
export const IDLE_TIMEOUT_MS = 15_000;

export function inheritedDescriptors(environment: NodeJS.ProcessEnv, parentPid: number): {
  control: number; bulk: number; parent: number;
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
  return { control, bulk, parent };
}

/** S06 synthetic lifecycle only. No accessory is created or published. */
async function run(): Promise<number> {
  if (process.version !== "v26.12.1") throw new ProtocolError("node-version-mismatch");
  const descriptors = inheritedDescriptors(process.env, process.ppid);
  verifyPublicApi();
  const control = new Socket({ fd: descriptors.control, readable: true, writable: true });
  const bulk = new Socket({ fd: descriptors.bulk, readable: true, writable: true });
  const channel = new ControlChannel(control);
  let boot: string | null = null;
  let session: AdapterSession | null = null;
  let transfer: BulkChannel | null = null;
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
    void transfer?.close("cancelled").catch(() => {});
    channel.close();
  };
  const reply = (message: Envelope, kind: Envelope["kind"], payload = {}): Promise<void> =>
    channel.send({ ...message, kind, payload });

  channel.on("message", (message: Envelope) => {
    void (async () => {
      if (ending) return;
      if (boot === null) {
        if (message.kind !== "hello") throw new ProtocolError("hello-required");
        boot = message.boot_id;
        clearTimeout(hello);
        renewIdle();
        await reply(message, "hello_ack", {
          role: "hap", build_id: message.payload.build_id, protocol: 1,
          features: [FEATURE], pid: process.pid,
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
          onFailure: () => fail("session-failed")});
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
        session?.close();
        await transfer?.close("cancelled");
        await reply(message, "shutdown_ack");
        channel.close();
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
  const signal = (): void => { ending = true; session?.close(); void transfer?.close("cancelled").catch(() => {}); channel.close(); };
  process.once("SIGTERM", signal);
  process.once("SIGINT", signal);
  await new Promise<void>(resolve => channel.once("closed", resolve));
  clearTimeout(hello);
  clearInterval(parentWatch);
  if (idle !== null) clearTimeout(idle);
  (session as AdapterSession | null)?.close();
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
