// Offline differential-test helper, not part of the production entry point.
import { decodeEnvelope } from "../src/protocol.js";
const chunks: Buffer[] = [];
let length = 0;
for await (const chunk of process.stdin) {
  const bytes = Buffer.from(chunk);
  length += bytes.length;
  if (length > 1024 * 1024) throw new Error("test-corpus-too-large");
  chunks.push(bytes);
}
const corpus = JSON.parse(Buffer.concat(chunks).toString("utf8")) as string[];
const result = corpus.map(encoded => {
  try { decodeEnvelope(Buffer.from(encoded, "base64")); return true; }
  catch { return false; }
});
process.stdout.write(JSON.stringify(result) + "\n");
