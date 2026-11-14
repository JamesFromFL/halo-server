/** Dedicated supervised process; receives normalized sensor states, never Ring credentials. */
import { fstatSync } from "node:fs";
import { Socket } from "node:net";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { ProtocolError } from "./protocol.js";
import { PairingStore, STORAGE_RECOVERY_REQUIRED } from "./storage.js";
import { enablePairingDurability } from "./storage-boundary.js";
import { verifyPublicApi } from "./public-api.js";
import { DeviceAlarmRequests } from "./device-alarm.js";
import { DeviceControlChannel } from "./device-ipc.js";
import { DeviceBridgeManager, deviceHapPublisher } from "./device-bridge.js";
import {
  validateDeviceEnvelope,
  type DeviceEnvelope,
  type DeviceKind,
  type DevicePairingIdentity,
  type DevicePairingRequest,
  type DevicePublication,
} from "./device-protocol.js";

/** Pinned private directory and inherited lock remain owned until process exit. */
export function deviceDescriptors(
  environment: NodeJS.ProcessEnv,
  parentPid: number,
  reset = false,
): { control: number; pairing: number; pairingLock: number; parent: number } {
  if (
    environment.NODE_OPTIONS ||
    environment.NODE_PATH ||
    process.platform !== "linux"
  )
    throw new ProtocolError("invalid-bootstrap");
  const fd = (name: string): number => {
    const value = environment[name];
    if (
      !value ||
      !/^[1-9][0-9]*$/.test(value) ||
      !Number.isSafeInteger(Number(value))
    )
      throw new ProtocolError("invalid-bootstrap");
    return Number(value);
  };
  const control = reset ? 0 : fd("HALO_DEVICE_CONTROL_FD"),
    pairing = fd("HALO_PAIRING_FD"),
    pairingLock = fd("HALO_PAIRING_LOCK_FD"),
    parent = fd("HALO_PARENT_PID");
  if (
    parent !== parentPid ||
    pairing < 3 ||
    pairingLock < 3 ||
    (!reset && control < 3) ||
    new Set([control, pairing, pairingLock]).size !== 3 ||
    (!reset && !fstatSync(control).isSocket()) ||
    !fstatSync(pairing).isDirectory()
  )
    throw new ProtocolError("invalid-bootstrap");
  const metadata = fstatSync(pairingLock);
  if (
    !metadata.isFile() ||
    metadata.uid !== process.getuid?.() ||
    metadata.nlink !== 1 ||
    (metadata.mode & 0o777) !== 0o600
  )
    throw new ProtocolError("invalid-bootstrap");
  return { control, pairing, pairingLock, parent };
}
async function run(): Promise<number> {
  if (process.version !== "v26.12.1")
    throw new ProtocolError("node-version-mismatch");
  const descriptors = deviceDescriptors(process.env, process.ppid);
  verifyPublicApi();
  let manager: DeviceBridgeManager | null = null;
  let alarms: DeviceAlarmRequests | null = null;
  enablePairingDurability(() => manager?.invalidate());
  const store = new PairingStore(descriptors.pairing);
  manager = new DeviceBridgeManager({
    store,
    publisher: deviceHapPublisher(store),
    onAlarmSet: (request) =>
      alarms
        ? alarms.request(request)
        : Promise.reject(new ProtocolError("not-ready")),
  });
  const channel = new DeviceControlChannel(
    new Socket({ fd: descriptors.control, readable: true, writable: true }),
  );
  let boot: string | null = null,
    ending = false,
    failed = false,
    idle: NodeJS.Timeout | null = null,
    pending = 0,
    chain = Promise.resolve();
  const fail = (code: string): void => {
    if (ending) return;
    failed = true;
    ending = true;
    manager!.invalidate();
    alarms?.close();
    process.stderr.write(`Halo device bridge: ${code}\n`);
    channel.close();
  };
  alarms = new DeviceAlarmRequests({
    boot: () => boot,
    send: (message) => channel.send(message),
    fail,
  });
  const handshake = setTimeout(() => fail("handshake-timeout"), 5000);
  const parentWatch = setInterval(() => {
    if (process.ppid !== descriptors.parent) fail("parent-retired");
  }, 5000);
  const renew = (): void => {
    if (idle) clearTimeout(idle);
    idle = setTimeout(() => fail("parent-idle"), 15000);
  };
  const reply = (
    message: DeviceEnvelope,
    kind: DeviceKind,
    payload: Record<string, unknown> = {},
  ): Promise<void> => channel.send({ ...message, kind, payload });
  const handle = async (message: DeviceEnvelope): Promise<void> => {
    if (ending) return;
    validateDeviceEnvelope(message, "python");
    if (boot === null) {
      if (message.kind !== "hello") throw new ProtocolError("hello-required");
      boot = message.boot_id;
      clearTimeout(handshake);
      renew();
      await reply(message, "hello_ack", {
        role: "device-hap",
        build_id: message.payload.build_id,
        protocol: 1,
        pid: process.pid,
      });
      return;
    }
    if (message.boot_id !== boot) throw new ProtocolError("stale-boot");
    renew();
    switch (message.kind) {
      case "alarm_set_result":
        alarms!.receive(message);
        break;
      case "ping":
        await reply(message, "pong");
        break;
      case "publish": {
        const publication = message.payload as unknown as DevicePublication;
        let code = "ok";
        try {
          await manager!.publish(publication);
        } catch (error) {
          if (
            !(error instanceof ProtocolError) ||
            ![
              "not-ready",
              "stale-scope",
              "conflict",
              "device-capacity",
            ].includes(error.code)
          )
            throw error;
          code =
            error.code === "device-capacity" ? "invalid-request" : error.code;
        }
        await reply(message, "publish_result", {
          scope: publication.scope,
          registry_revision: publication.registry_revision,
          sequence: publication.sequence,
          accepted: code === "ok",
          code,
        });
        break;
      }
      case "status":
        await reply(message, "status_result", {
          ...manager!.status(
            message.payload as unknown as DevicePairingIdentity,
          ),
        });
        break;
      case "reveal":
        await reply(message, "reveal_result", {
          ...manager!.reveal(
            message.payload as unknown as DevicePairingRequest,
          ),
        });
        break;
      case "reset_prepare":
        await reply(message, "reset_prepare_result", {
          ...(await manager!.prepareReset(
            message.payload as unknown as DevicePairingRequest,
          )),
        });
        break;
      case "shutdown":
        ending = true;
        manager!.invalidate();
        await manager!.close();
        await reply(message, "shutdown_ack");
        channel.close();
        break;
      default:
        throw new ProtocolError("unexpected-device-message");
    }
  };
  channel.on("message", (message: DeviceEnvelope) => {
    if (ending) return;
    if (++pending > 32) {
      fail("request-capacity");
      return;
    }
    // Ordered application makes revision checks and reset admission atomic.
    chain = chain
      .then(() => handle(message))
      .catch((error) => {
        if (ending) {
          failed = true;
          channel.close();
        } else
          fail(error instanceof ProtocolError ? error.code : "control-failed");
      })
      .finally(() => {
        pending--;
      });
  });
  channel.on("failure", () => {
    failed = true;
    manager!.invalidate();
  });
  const signal = (): void => {
    ending = true;
    manager!.invalidate();
    channel.close();
  };
  process.once("SIGTERM", signal);
  process.once("SIGINT", signal);
  await new Promise<void>((resolve) => channel.once("closed", resolve));
  ending = true;
  alarms.close();
  manager.invalidate();
  clearTimeout(handshake);
  clearInterval(parentWatch);
  if (idle) clearTimeout(idle);
  await chain;
  await manager.close();
  process.removeListener("SIGTERM", signal);
  process.removeListener("SIGINT", signal);
  return failed ? 1 : 0;
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  void run()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      const code =
        error instanceof ProtocolError &&
        error.code === STORAGE_RECOVERY_REQUIRED
          ? STORAGE_RECOVERY_REQUIRED
          : "bootstrap-failed";
      process.stderr.write(`Halo device bridge: ${code}\n`);
      process.exitCode = 1;
    });
}
