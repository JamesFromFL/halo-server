import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createServer } from "node:net";
import { test } from "node:test";
import {
  CameraController, DoorbellController, H264Level, H264Profile, SRTPCryptoSuites,
  type CameraStreamingDelegate,
} from "@homebridge/hap-nodejs";
import { verifyPublicApi } from "../src/public-api.js";
import { inheritedDescriptors } from "../src/main.js";

test("released public API imports/compiles without publication or pairing storage", context => {
  let publishes = 0;
  context.mock.method(createServer().constructor.prototype, "listen", () => {
    publishes++;
    throw new Error("network publication forbidden");
  });
  const before = existsSync("persist");
  assert.equal(verifyPublicApi(), "2.2.3");
  const delegate: CameraStreamingDelegate = {
    handleSnapshotRequest(_request, callback) { callback(new Error("synthetic-only")); },
    prepareStream(_request, callback) { callback(new Error("synthetic-only")); },
    handleStreamRequest(_request, callback) { callback(new Error("synthetic-only")); },
  };
  const options = {
    cameraStreamCount: 1,
    delegate,
    streamingOptions: {
      supportedCryptoSuites: [SRTPCryptoSuites.AES_CM_128_HMAC_SHA1_80],
      video: {
        codec: { profiles: [H264Profile.MAIN], levels: [H264Level.LEVEL3_1] },
        resolutions: [[640, 480, 15]] as [number, number, number][],
      },
    },
  };
  assert.ok(new CameraController(options));
  assert.ok(new DoorbellController(options));
  assert.equal(publishes, 0);
  assert.equal(existsSync("persist"), before);
});

test("bootstrap rejects missing, shared, noncanonical, standard or wrong-parent FDs", () => {
  for (const environment of [
    {},
    { HALO_CONTROL_FD: "0", HALO_BULK_FD: "4", HALO_PARENT_PID: "123" },
    { HALO_CONTROL_FD: "3", HALO_BULK_FD: "3", HALO_PARENT_PID: "123" },
    { HALO_CONTROL_FD: "03", HALO_BULK_FD: "4", HALO_PARENT_PID: "123" },
    { HALO_CONTROL_FD: "3", HALO_BULK_FD: "4", HALO_PARENT_PID: "456" },
  ]) assert.throws(() => inheritedDescriptors(environment, 123));
});


test("bootstrap refuses inherited Node loader and module path injection before attaching descriptors", () => {
  for (const name of ["NODE_OPTIONS", "NODE_PATH"]) {
    assert.throws(() => inheritedDescriptors({HALO_CONTROL_FD: "3", HALO_BULK_FD: "4", HALO_PARENT_PID: "123",
      [name]: "untrusted"}, 123), /runtime-injection-forbidden/);
  }
});
