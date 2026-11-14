/** Offline manifest-CAS reset: the inherited directory belongs to the device bridge. */
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { decodeResetInput, resetPairing } from "./pairing-reset.js";
import { deviceDescriptors } from "./device-main.js";
import { verifyPublicApi } from "./public-api.js";
import { enablePairingDurability } from "./storage-boundary.js";
async function run(): Promise<number> {
  if (process.version !== "v26.12.1") throw new Error("invalid-bootstrap");
  const descriptors = deviceDescriptors(process.env, process.ppid, true);
  verifyPublicApi();
  enablePairingDurability(() => {});
  const chunks: Buffer[] = [];
  let length = 0;
  const deadline = setTimeout(
    () => process.stdin.destroy(new Error("timeout")),
    5000,
  );
  let input: ReturnType<typeof decodeResetInput>;
  try {
    for await (const chunk of process.stdin) {
      const bytes = Buffer.from(chunk as Uint8Array);
      length += bytes.length;
      if (length > 4096) throw new Error("invalid-request");
      chunks.push(bytes);
    }
    input = decodeResetInput(Buffer.concat(chunks));
  } finally {
    clearTimeout(deadline);
  }
  const result = resetPairing(descriptors.pairing, input);
  await new Promise<void>((resolve, reject) =>
    process.stdout.write(JSON.stringify(result) + "\n", (error) =>
      error ? reject(error) : resolve(),
    ),
  );
  return result.accepted ? 0 : 1;
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
)
  void run()
    .then((code) => {
      process.exitCode = code;
    })
    .catch(() => {
      process.exitCode = 1;
    });
