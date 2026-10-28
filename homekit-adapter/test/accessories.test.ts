import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";
import { Accessory, Characteristic, DoorbellController, HAPStatus, Service, StreamRequestTypes } from "@homebridge/hap-nodejs";
import { AccessoryManager, hapPublisher, type AccessoryConfigurationPage, type AccessoryPublisher } from "../src/accessories.js";
import { CameraAccessory, unavailableStreamingDelegate, type AccessoryIdentity } from "../src/camera.js";
import type { AccountScope, JsonObject, MediaScope } from "../src/protocol.js";
import type { NativeCamera } from "../src/session.js";
import type { PairingStore } from "../src/storage.js";

const profile = "00000000-0000-0000-0000-000000000002";
const account: AccountScope = {profile_id: profile, account_generation: 1};
const scope: MediaScope = {...account, camera_id: "front", device_generation: 1, privacy_generation: 1};
const identity: AccessoryIdentity = {camera_id: "front", accessory_id: "00000000-0000-0000-0000-000000000003",
  username: "02:11:22:33:44:55", doorbell: true};
const camera = (changes: Partial<NativeCamera> = {}): NativeCamera => ({scope: {...scope}, name: "Front Door", family: "fixture",
  kind: "doorbell", model: "Fixture", capabilities: {video: true, audio: false, snapshot: false, talkback: false,
    light: false, siren: false, doorbell: true, battery: false, evidence: ["halo:video", "halo:doorbell"]},
  connection_state: null, battery_percent: null, observed_format: null, ...changes});
const page = (changes: Partial<AccessoryConfigurationPage> = {}): AccessoryConfigurationPage => ({scope: {...account},
  revision: 1, generation: 1, page_index: 0, total_pages: 1, entries: [{...identity}], media_ready: false, ...changes});
const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
const deferred = <T>(): {promise: Promise<T>; resolve: (value: T) => void; reject: (error: Error) => void} => {
  let resolve!: (value: T) => void, reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
class FakePublisher implements AccessoryPublisher {
  mediaReady = true;
  published: CameraAccessory[] = [];
  unpublished: CameraAccessory[] = [];
  publishBarrier: Promise<void> | null = null;
  failClose = false;
  async publish(value: CameraAccessory): Promise<void> { this.published.push(value); await this.publishBarrier; }
  async unpublish(value: CameraAccessory): Promise<void> {
    this.unpublished.push(value);
    if (this.failClose) throw new Error("synthetic cleanup failure");
  }
}
function harness(options: {publisher?: AccessoryPublisher; sync?: () => Promise<JsonObject>; noStore?: boolean; clock?: () => number} = {}) {
  const session = {cameras: () => [current], inventoryComplete: true, accountScope: {...account} as AccountScope | null};
  let current = camera();
  const publisher = options.publisher ?? new FakePublisher();
  let preflights = 0, closes = 0;
  const store = {preflight: () => { preflights++; }, close: () => { closes++; }} as unknown as PairingStore;
  const manager = new AccessoryManager({session, ...(options.noStore ? {} : {store}), publisher,
    ...(options.clock === undefined ? {} : {monotonicNow: options.clock}),
    syncEvents: options.sync ?? (async () => ({scope: account, epoch: "epoch-a", sequence: 10, accepted: true, code: "ok"}))});
  return {manager, session, publisher, store, setCamera: (value: NativeCamera) => { current = value; },
    get preflights() { return preflights; }, get closes() { return closes; }};
}
const motion = (value: CameraAccessory): unknown => value.controller.motionService!.getCharacteristic(Characteristic.MotionDetected).value;
const active = (value: CameraAccessory): unknown => value.controller.motionService!.getCharacteristic(Characteristic.StatusActive).value;
const state = (changes: Partial<Parameters<AccessoryManager["eventState"]>[0]> = {}): Parameters<AccessoryManager["eventState"]>[0] =>
  ({scope, epoch: "epoch-a", sequence: 10, motion: false, ttl_ms: 0,
    expires_at_ms: Number(process.hrtime.bigint() / 1_000_000n) + 30_000, source_state: "connected", synchronized: true, ...changes});
const press = (sequence: number, changes: Partial<Parameters<AccessoryManager["cameraEvent"]>[0]> = {}): Parameters<AccessoryManager["cameraEvent"]>[0] =>
  ({scope, epoch: "epoch-a", sequence, event_id: randomUUID(), type: "doorbell", ttl_ms: 2000,
    expires_at_ms: Number(process.hrtime.bigint() / 1_000_000n) + 2000, ...changes});

test("public camera graphs preserve UUID and service identity across renames without storage or publication", () => {
  const before = new CameraAccessory(identity, camera());
  const after = new CameraAccessory(identity, camera({name: "Renamed Door"}));
  const graph = (value: CameraAccessory) => Accessory.serialize(value.accessory).services.map(service => [service.UUID, service.subtype]);
  assert.equal(before.accessory.UUID, after.accessory.UUID);
  assert.deepEqual(graph(before), graph(after));
  assert.ok(before.controller instanceof DoorbellController);
  assert.ok(before.accessory.getService(Service.Doorbell));
  assert.equal(motion(before), false); assert.equal(active(before), false);
  assert.equal(before.accessory.getService(Service.AccessoryInformation)!.getCharacteristic(Characteristic.SerialNumber).value, identity.accessory_id);
  assert.equal(before.accessory.getService(Service.Microphone), undefined);
  assert.equal(before.accessory.getService(Service.CameraRecordingManagement), undefined);
  before.retire(); after.retire();
});

test("media delegates fail clearly and STOP remains an idempotent release", () => {
  const delegate = unavailableStreamingDelegate();
  delegate.handleSnapshotRequest({width: 320, height: 240}, (error, bytes) => {
    assert.equal(error, HAPStatus.SERVICE_COMMUNICATION_FAILURE); assert.equal(bytes, undefined);
  });
  delegate.prepareStream({} as Parameters<typeof delegate.prepareStream>[0], (error, endpoint) => {
    assert.match(error!.message, /media-not-ready/); assert.equal(endpoint, undefined);
  });
  delegate.handleStreamRequest({type: StreamRequestTypes.STOP, sessionID: randomUUID()}, error => assert.equal(error, undefined));
});

test("configuration pages apply atomically, reject identity conflicts, and keep the real publisher gated", async () => {
  const value = harness({publisher: hapPublisher({} as PairingStore)});
  await value.manager.configure(page({total_pages: 2}));
  assert.equal(value.manager.count, 0); assert.equal(value.preflights, 0);
  await value.manager.configure(page({total_pages: 2, page_index: 1, entries: []}));
  assert.equal(value.manager.count, 1); assert.equal(value.manager.publishedCount, 0);
  await value.manager.configure(page({total_pages: 2, page_index: 1, entries: []}));
  await assert.rejects(value.manager.configure(page({total_pages: 2, page_index: 1, entries: [{...identity}]})), /configuration-conflict/);
  await value.manager.configure(page({generation: 2, media_ready: true}));
  assert.equal(value.manager.publishedCount, 0);
  await value.manager.close(); assert.equal(value.closes, 1);
  const noStore = harness({noStore: true});
  await assert.rejects(noStore.manager.configure(page()), /pairing-store-unavailable/);
  await noStore.manager.close();
});

test("rename and incomplete inventory preserve objects, while account retirement unpublishes without destroying pairings", async () => {
  const value = harness();
  await value.manager.configure(page({media_ready: true}));
  const original = value.manager.accessories()[0]!;
  value.setCamera(camera({name: "Renamed"})); await value.manager.reconcile();
  assert.equal(value.manager.accessories()[0], original); assert.equal(original.accessory.displayName, "Renamed");
  value.session.inventoryComplete = false; value.manager.invalidate(); await value.manager.reconcile();
  assert.equal(value.manager.accessories()[0], original); assert.equal(active(original), false);
  value.session.accountScope = null; await value.manager.reconcile();
  assert.equal(value.manager.count, 0); assert.equal(original.isRetired, true);
  assert.deepEqual((value.publisher as FakePublisher).unpublished, [original]);
  await value.manager.close();
});

test("account replacement during pending publication compensates before replacement can be created", async () => {
  const publisher = new FakePublisher(), barrier = deferred<void>(); publisher.publishBarrier = barrier.promise;
  const value = harness({publisher});
  const first = value.manager.configure(page({media_ready: true})); await tick();
  const retiring = value.manager.configure(page({generation: 2, scope: null, entries: []}));
  assert.equal(publisher.published.length, 1);
  barrier.resolve(); await first; await retiring;
  assert.equal(value.manager.count, 0); assert.equal(publisher.unpublished.length, 1);
  await value.manager.close();
});

test("failed unpublish retains the old claim and rejects replacement instead of losing ownership", async () => {
  const publisher = new FakePublisher(), value = harness({publisher});
  await value.manager.configure(page({media_ready: true})); publisher.failClose = true;
  await assert.rejects(value.manager.configure(page({generation: 2, entries: []})), /synthetic cleanup failure/);
  assert.equal(value.manager.count, 1); assert.equal(value.manager.isFailed, true);
  await assert.rejects(value.manager.configure(page({generation: 3})), /manager-retired/);
  await assert.rejects(value.manager.close(), /synthetic cleanup failure/);
  assert.equal(value.closes, 0);
});

test("a partially failed publish retains its cleanup claim and concurrent close shares retirement", async () => {
  const publisher = new FakePublisher();
  publisher.publish = async camera => { publisher.published.push(camera); throw new Error("partial publish"); };
  publisher.failClose = true;
  const value = harness({publisher});
  await assert.rejects(value.manager.configure(page({media_ready: true})), /synthetic cleanup failure/);
  assert.equal(value.manager.count, 1); assert.equal(value.manager.publishedCount, 1);
  const first = value.manager.close(), second = value.manager.close();
  assert.equal(first, second);
  await assert.rejects(first, /synthetic cleanup failure/);
  assert.equal(value.closes, 0);
});

test("an incomplete configuration has a fixed whole-assembly deadline", async context => {
  context.mock.timers.enable({apis: ["setTimeout"]});
  const failures: string[] = [];
  const manager = new AccessoryManager({session: {cameras: () => [], inventoryComplete: false, accountScope: account},
    syncEvents: async () => ({}), onFailure: error => failures.push(error.code)});
  await manager.configure(page({total_pages: 2}));
  context.mock.timers.tick(29_999);
  await manager.configure(page({total_pages: 2}));
  context.mock.timers.tick(1);
  assert.deepEqual(failures, ["accessory-configuration-timeout"]);
  assert.equal(manager.count, 0); await manager.close();
});

test("event snapshots wait for synchronization, expire from receipt, and stale scopes cannot revive motion", async context => {
  context.mock.timers.enable({apis: ["setTimeout"]});
  let now = 0;
  const sync = deferred<JsonObject>(), value = harness({sync: () => sync.promise, clock: () => now});
  await value.manager.configure(page());
  const accessory = value.manager.accessories()[0]!;
  value.manager.eventState(state({motion: true, ttl_ms: 100, expires_at_ms: 100}));
  assert.equal(motion(accessory), false); assert.equal(active(accessory), false);
  now += 101; context.mock.timers.tick(101);
  sync.resolve({scope: account, epoch: "epoch-a", sequence: 10, accepted: true, code: "ok"}); await tick();
  assert.equal(motion(accessory), false); assert.equal(active(accessory), true);
  value.manager.eventState(state({motion: true, ttl_ms: 100, expires_at_ms: now + 100, sequence: 11}));
  assert.equal(motion(accessory), true);
  value.manager.eventState(state({scope: {...scope, privacy_generation: 2}, motion: false, sequence: 12}));
  assert.equal(motion(accessory), true);
  now += 101; context.mock.timers.tick(101); assert.equal(motion(accessory), false);
  value.manager.invalidate(); assert.equal(active(accessory), false);
  await value.manager.close();
});

test("doorbell presses are live, scoped, deduplicated, and do not invent motion", async context => {
  const value = harness(); await value.manager.configure(page()); await tick();
  const accessory = value.manager.accessories()[0]!, rings: number[] = [];
  context.mock.method(accessory.controller as DoorbellController, "ringDoorbell", () => { rings.push(1); });
  value.manager.cameraEvent(press(11)); assert.equal(rings.length, 0);
  value.manager.eventState(state());
  const event = press(11); value.manager.cameraEvent(event); value.manager.cameraEvent(event);
  value.manager.cameraEvent({...event, sequence: 12});
  value.manager.cameraEvent(press(12, {scope: {...scope, account_generation: 2}}));
  value.manager.cameraEvent(press(10)); value.manager.cameraEvent(press(13, {epoch: "old-epoch"}));
  assert.equal(rings.length, 1); assert.equal(motion(accessory), false);
  value.manager.cameraEvent(press(17)); assert.equal(rings.length, 2); // Global journal skips are valid.
  value.manager.invalidate(); value.manager.cameraEvent(press(18)); assert.equal(rings.length, 2);
  await value.manager.close();
});

test("absolute monotonic deadlines reject motion and doorbells already delayed beyond their source lifetime", async context => {
  let now = 1000;
  const value = harness({clock: () => now}); await value.manager.configure(page()); await tick();
  const accessory = value.manager.accessories()[0]!, rings: number[] = [];
  const changes: unknown[] = [];
  accessory.controller.motionService!.getCharacteristic(Characteristic.MotionDetected)
    .on("change", change => changes.push(change.newValue));
  context.mock.method(accessory.controller as DoorbellController, "ringDoorbell", () => { rings.push(1); });
  value.manager.eventState(state({expires_at_ms: 0}));
  const queuedPress = press(11, {expires_at_ms: 3000});
  const queuedMotion = state({motion: true, ttl_ms: 2000, expires_at_ms: 3000, sequence: 11});
  // Bytes may have been written while fresh, then wait in a socket/Node event-loop stall.
  now = 3001;
  value.manager.cameraEvent(queuedPress); value.manager.eventState(queuedMotion);
  assert.equal(rings.length, 0); assert.equal(motion(accessory), false);
  assert.equal(changes.includes(true), false); // No transient stale HomeKit notification.
  value.manager.cameraEvent(press(12, {expires_at_ms: 5001})); assert.equal(rings.length, 1);
  await value.manager.close();
});
