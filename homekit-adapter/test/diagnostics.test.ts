import {test} from "node:test";
import assert from "node:assert/strict";
import {diagnosticPayload, InteropDiagnostics} from "../src/diagnostics.js";

test("interop diagnostics never forward arbitrary error data or peer fields", () => {
  assert.equal(diagnosticPayload("unknown-stage", "timeout"), null);
  assert.deepEqual(diagnosticPayload("video-start", "unsupported-audio-format"),
    {code: "interop-video-start", message: "unsupported-audio-format"});
  for (const secret of ["031-45-154", "X-HM://0023ISYWYABCD", "srtp_key=private", "arbitrary-exception"]) {
    assert.deepEqual(diagnosticPayload("video-prepare", secret),
      {code: "interop-video-prepare", message: "operation-failed"});
  }
});

test("diagnostic backpressure and time budget stay bounded without changing operation outcomes", async () => {
  const notices: unknown[] = []; let finish!: () => void, now = 0;
  const diagnostics = new InteropDiagnostics(payload => {
    notices.push(payload); return new Promise<void>(resolve => { finish = resolve; });
  }, () => now);
  for (let i = 0; i < 100; i++) diagnostics.report("video-prepare", "invalid-video-prepare");
  diagnostics.report("audio-settings", "timeout");
  assert.equal(notices.length, 1);
  finish(); await new Promise(resolve => setImmediate(resolve));
  diagnostics.report("video-prepare", "invalid-video-prepare"); assert.equal(notices.length, 1);
  now = 5_000; diagnostics.report("video-prepare", "invalid-video-prepare"); assert.equal(notices.length, 2);
  finish(); await new Promise(resolve => setImmediate(resolve));
  const failed = new InteropDiagnostics(async () => { throw new Error("private exception"); });
  assert.doesNotThrow(() => failed.report("snapshot", "unavailable"));
  await new Promise(resolve => setImmediate(resolve));
});
