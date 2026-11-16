/** Unmodified bundled Halo UI, read-only synthetic loopback data. */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, extname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const require = createRequire(resolve(root, "web-ui/package.json"));
const { chromium } = require("@playwright/test");
const staticRoot = resolve(root, "src/halo_server/web/static");
const output = resolve(process.env.HALO_SCREENSHOT_OUTPUT || here);
await mkdir(output, { recursive: true });
const now = () => Date.now() / 1000;
const scope = { profile_id: "documentation-demo", account_generation: 1 };
const camera = (id, name, model, kind = "camera", light = false) => ({
  id,
  camera_id: id,
  name,
  model,
  source: "ring",
  kind,
  scope: { ...scope, device_generation: 1 },
  capabilities: ["video", "audio", "motion", ...(light ? ["light"] : [])],
  observed: {
    source_available: true,
    battery_percent: null,
    light: light ? false : null,
    siren: null,
  },
  homekit: {
    selected: true,
    published: true,
    paired: true,
    owner_warm: true,
    recording_active: true,
    recording_audio_active: true,
  },
});
const cameras = [
  camera("front-door-camera", "Front Door", "Video Doorbell Pro 2", "doorbell"),
  camera(
    "garden-camera",
    "Garden Floodlight",
    "Floodlight Cam Wired Pro",
    "camera",
    true,
  ),
  camera("living-room-camera", "Living Room", "Indoor Cam"),
];
const device = (id, name, kind, model, capabilities, observed, extra = {}) => ({
  id,
  name,
  kind,
  model,
  provider: "ring-hub",
  family: "hub",
  capability_evidence: ["ring-hub-device-schema"],
  location_id: "demo-home",
  location_name: "Home",
  capabilities,
  observed,
  available: true,
  source_health: "online",
  stale: false,
  observed_at: now(),
  supported: true,
  unsupported_reason: null,
  ...extra,
});
const command = {
  policy_revision: 1,
  enabled: true,
  provider_generation: 1,
  epoch: 1,
  observed_revision: 1,
  authorization: "allowed",
  pending: false,
  outcome_unknown: false,
  reason: null,
};
const devices = [
  device(
    "alarm",
    "Home Alarm",
    "security-panel",
    "Ring Alarm",
    ["mode"],
    {},
    {
      alarm: {
        ...command,
        mode: "home",
        phase: "idle",
        signal: "none",
        can_set_mode: true,
        allowed_targets: ["disarmed", "home", "away"],
        requested_mode: null,
      },
    },
  ),
  device(
    "contact",
    "Front Door Sensor",
    "contact-sensor",
    "Contact Sensor",
    ["contact"],
    { contact_open: false, battery_percent: 96, tampered: false },
  ),
  device(
    "motion",
    "Hallway Motion",
    "motion-sensor",
    "Motion Detector",
    ["motion"],
    { motion_detected: false, battery_percent: 93 },
  ),
  device(
    "keypad",
    "Entry Keypad",
    "keypad",
    "Alarm Keypad",
    ["keypad-brightness", "motion"],
    { keypad_brightness: 40, motion_detected: false },
    {
      keypad: {
        ...command,
        locked: true,
        brightness: 40,
        on_brightness: 40,
        can_set: false,
        can_set_lock: true,
        requested_brightness: null,
        reason: "brightness-locked",
      },
    },
  ),
  device(
    "garden-light",
    "Garden Path Lights",
    "beams-light",
    "Ring Smart Lighting",
    ["light"],
    { light_on: false },
    {
      light: {
        ...command,
        on_duration_seconds: 60,
        on: false,
        can_set: true,
        requested_on: null,
      },
    },
  ),
  device(
    "hallway-chime",
    "Hallway Chime",
    "chime",
    "Chime Pro",
    ["volume"],
    {},
    {
      provider: "ring-chime",
      family: "chime",
      chime: {
        ...command,
        sound_tests_enabled: false,
        can_set: true,
        requested_action: null,
        volume: 5,
        snooze_seconds_left: 0,
        automatic_nightlight: true,
        sound_active: false,
      },
    },
  ),
];
const mappings = [
  "security-system",
  "contact",
  "motion",
  "keypad-light",
  "light",
  null,
];
const bridge = {
  revision: 1,
  configured: true,
  enabled: true,
  auto_import: true,
  accessory_uuid: "11111111-1111-4111-8111-111111111111",
  username: "02:00:00:00:00:01",
  capacity: 149,
  selected_count: 5,
  retained_count: 5,
  overflow_count: 0,
  included_devices: [],
  excluded_devices: [],
  excluded_locations: [],
  excluded_kinds: [],
  excluded_mappings: [],
  publication: "implemented",
  status: "ready",
  truncated: false,
  runtime: {
    state: "ready",
    reason: null,
    boot_id: "demo-bridge",
    published: true,
    publication_enabled: true,
    publication_revision: 1,
    reset_pending: false,
    reset_operation_id: null,
  },
  devices: devices.map((item, i) => ({
    id: item.id,
    accessory_uuid: mappings[i] ? `demo-accessory-${i}` : null,
    mappings: mappings[i] ? [mappings[i]] : [],
    selected: !!mappings[i],
    excluded: false,
    eligible: !!mappings[i],
    reason: mappings[i] ? "ready" : "unsupported-mapping",
    present: true,
  })),
};
function snapshot() {
  const at = now();
  return {
    schema: 1,
    cursor: String(Date.now()),
    web: { boot_id: "demo-web", observed_at: at },
    daemon: { available: true, stale: false, observed_at: at, error: null },
    state: {
      schema: 1,
      epoch: "demo-state",
      observed_at: at,
      daemon: {
        boot_id: "demo-daemon",
        version: "0.1.0",
        uptime_seconds: 86400,
        stopping: false,
        release: { managed: true },
      },
      ring: {
        authenticated: true,
        restoring: false,
        events: "connected",
        inventory_complete: true,
      },
      scope,
      homekit: {
        enabled: true,
        adapter_boot_id: "demo-camera-adapter",
        state: "published",
        registry_revision: 1,
        selected_count: 3,
        published_count: 3,
        recording_revision: 1,
      },
      devices: cameras,
      noncamera: {
        schema: 1,
        revision: 1,
        policy_revision: 1,
        scope,
        state: "ready",
        complete: true,
        observed_at: at,
        devices,
        enabled: true,
        truncated: false,
        bridge,
        safety: {
          read_only: false,
          panic_enabled: false,
          emergency_dispatch_enabled: false,
        },
      },
      activity: {
        epoch: "demo-activity",
        cursor: 2,
        capacity: 256,
        truncated: false,
        entries: [
          {
            sequence: 1,
            at: at - 140,
            category: "camera",
            action: "camera_selected",
            outcome: "completed",
            camera_id: "garden-camera",
          },
          {
            sequence: 2,
            at: at - 60,
            category: "homekit",
            action: "bridge_published",
            outcome: "completed",
          },
        ],
      },
      truncated: false,
    },
  };
}
function cameraPage(kind) {
  return {
    ...scope,
    available: true,
    boot_id: "demo-camera-adapter",
    revision: 1,
    registry_revision: 1,
    total: cameras.length,
    warm_budget: { capacity: null, reserved: 3 },
    cameras: cameras.map((item) => ({
      ...item.scope,
      camera_id: item.camera_id,
      name: item.name,
      selected: true,
      available: true,
      published: true,
      paired_known: true,
      paired: true,
      pairing_revision: "a".repeat(64),
      pending_reset: false,
      has_light: item.capabilities.includes("light"),
      has_siren: false,
      light: item.observed.light,
      siren: null,
      owner_warm: true,
      recording_supported: true,
      warm_state: "ready",
      active: true,
      audio_active: true,
      configuration: null,
      ...(kind === "controls" && item.capabilities.includes("light")
        ? {
            lighting: {
              fresh: true,
              observed_age_seconds: 1,
              light: false,
              motion_duration_seconds: 60,
              manual_timer_status: "unverified",
              activation_source: null,
              reason: null,
            },
          }
        : {}),
      ...(kind === "privacy"
        ? {
            revision: 1,
            mode: "ring",
            source_fresh: true,
            ring_live_allowed: true,
            ring_recording_allowed: true,
            ring_snapshot_allowed: true,
            ring_audio_allowed: true,
            effective_live_allowed: true,
            effective_recording_allowed: true,
            effective_snapshot_allowed: true,
            effective_audio_allowed: true,
            reason: null,
          }
        : {}),
    })),
  };
}
const subscribers = new Set(),
  requests = [],
  errors = [];
const server = createServer(async (request, response) => {
  const path = new URL(request.url, "http://localhost").pathname;
  requests.push({ method: request.method, path });
  const send = (result) => {
    response.writeHead(200, {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    });
    response.end(JSON.stringify({ ok: true, result }));
  };
  if (request.method !== "GET") {
    errors.push(`Blocked write: ${request.method} ${path}`);
    response.writeHead(405);
    return response.end();
  }
  if (path === "/api/web/v1/session")
    return send({
      authenticated: true,
      setup_required: false,
      csrf_token: "synthetic-documentation-token",
      reauthenticated: false,
      session_id: "synthetic-documentation-session",
    });
  if (path === "/api/web/v1/state") return send(snapshot());
  if (path === "/api/web/v1/events") {
    response.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-store",
    });
    subscribers.add(response);
    response.write(`event: state\ndata: ${JSON.stringify(snapshot())}\n\n`);
    request.on("close", () => subscribers.delete(response));
    return;
  }
  if (
    ["selection", "pairing", "recording", "controls", "privacy"].some(
      (kind) => path === `/api/web/v1/${kind}`,
    )
  )
    return send(cameraPage(path.split("/").at(-1)));
  if (path === "/api/web/v1/devices/bridge/pairing")
    return send({
      ...scope,
      registry_revision: 1,
      publication_revision: 1,
      accessory_id: bridge.accessory_uuid,
      username: bridge.username,
      boot_id: bridge.runtime.boot_id,
      available: true,
      published: true,
      paired_known: true,
      paired: true,
      paired_clients: 1,
      pairing_revision: "b".repeat(64),
      pending_reset: false,
      reset_operation_id: null,
    });
  if (path.startsWith("/api/")) {
    errors.push(`Unexpected API: ${path}`);
    response.writeHead(404);
    return response.end();
  }
  const file = resolve(staticRoot, path === "/" ? "index.html" : `.${path}`);
  if (relative(staticRoot, file).startsWith("..")) {
    response.writeHead(403);
    return response.end();
  }
  try {
    const body = await readFile(file);
    response.writeHead(200, {
      "Content-Type":
        {
          ".html": "text/html",
          ".js": "application/javascript",
          ".css": "text/css",
          ".png": "image/png",
          ".svg": "image/svg+xml",
        }[extname(file)] || "application/octet-stream",
    });
    response.end(body);
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 1280, height: 1050 },
  colorScheme: "dark",
});
await context.route("**/*", async (route) => {
  if (new URL(route.request().url()).origin === base) return route.continue();
  errors.push(`Blocked external request: ${route.request().url()}`);
  return route.abort();
});
await context.addInitScript(() =>
  localStorage.setItem("halo-appearance", "system"),
);
const page = await context.newPage();
page.on("pageerror", (error) => errors.push(error.message));
async function open(path, theme = "dark") {
  await page.emulateMedia({ colorScheme: theme });
  await page.goto(`${base}/#${path}`);
  await page.getByRole("link", { name: "Overview", exact: true }).waitFor();
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(300);
  assert.equal(await page.locator('[role="alert"]').count(), 0);
}
async function capture(name) {
  await page.evaluate(() => document.activeElement?.blur());
  await page.screenshot({ path: resolve(output, `${name}.png`) });
}
try {
  await page.setViewportSize({ width: 1280, height: 900 });
  await open("/");
  await page.getByText("Halo is responding", { exact: true }).waitFor();
  await capture("halo-server-overview");
  await page.setViewportSize({ width: 1280, height: 800 });
  await open("/devices", "light");
  await page.getByText("Hallway Chime", { exact: true }).waitFor();
  await page
    .getByLabel("Filter other devices by location")
    .evaluate((el) =>
      window.scrollTo(0, window.scrollY + el.getBoundingClientRect().top - 105),
    );
  await capture("halo-server-devices");
  await page.setViewportSize({ width: 1280, height: 1210 });
  await open("/devices/front-door-camera");
  await page
    .getByRole("switch", { name: "Enable HomeKit Secure Video support" })
    .waitFor();
  await page
    .getByRole("heading", { name: "Privacy", exact: true })
    .evaluate((el) =>
      window.scrollTo(0, window.scrollY + el.getBoundingClientRect().top - 80),
    );
  await capture("halo-server-camera");
  await page.setViewportSize({ width: 1280, height: 1240 });
  await open("/integrations", "light");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.getByText("Connected to HomeKit", { exact: true }).waitFor();
  await capture("halo-server-integrations");
  assert.deepEqual(errors, []);
  assert.ok(requests.every((request) => request.method === "GET"));
  await writeFile(
    resolve(output, "capture-report.json"),
    JSON.stringify(
      {
        screenshots: 4,
        viewportWidth: 1280,
        errors,
        requests,
        source: "Unmodified production static bundle; synthetic read-only data",
      },
      null,
      2,
    ),
  );
} finally {
  await browser.close();
  for (const response of subscribers) response.end();
  await new Promise((done) => server.close(done));
}
