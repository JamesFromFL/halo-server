import assert from "node:assert/strict";
import { createCipheriv, createDecipheriv } from "node:crypto";
import { EventEmitter } from "node:events";
import type { Socket as NetSocket } from "node:net";
import { Duplex } from "node:stream";
import { test } from "node:test";
import { BoundedFrameWriter, CameraRecordingStream, DataStreamConnection, DataStreamParser, DataStreamReader, DataStreamWriter,
  Float32, Int64, PhysicalConnectionBudget, type CameraRecordingDelegate, type WriterLimits } from "@homebridge/hap-nodejs";

const tick = (): Promise<void> => new Promise(resolve => setImmediate(resolve));
const limits: WriterLimits = {maximumFrameBytes: 266240, maximumPendingFrames: 2, maximumPendingBytes: 532480,
  writeTimeoutMs: 1000, closeGraceMs: 5, closeTimeoutMs: 30};
class Socket extends Duplex {
  remoteAddress = "synthetic-no-network";
  writes: Buffer[] = [];
  callbacks: ((error?: Error | null) => void)[] = [];
  constructor() { super({highWaterMark: 16}); }
  override _read(): void {}
  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.writes.push(Buffer.from(chunk)); this.callbacks.push(callback);
  }
  setNoDelay(): this { return this; }
  setKeepAlive(): this { return this; }
  release(): void { this.callbacks.shift()?.(); }
  override _destroy(error: Error | null, callback: (error?: Error | null) => void): void {
    for (const release of this.callbacks.splice(0)) release(error ?? new Error("closed"));
    callback(error);
  }
}
class SlowDestroy extends Socket {
  destroyRelease: (() => void) | undefined;
  override _destroy(error: Error | null, callback: (error?: Error | null) => void): void {
    this.destroyRelease = () => super._destroy(error, callback);
  }
}
function encode(header: Record<string, unknown>, message: Record<string, unknown>, nonce: number, key: Buffer): Buffer {
  const h = new DataStreamWriter(), m = new DataStreamWriter();
  DataStreamParser.encode(header, h); DataStreamParser.encode(message, m);
  const plaintext = Buffer.concat([Buffer.from([h.length()]), h.getData(), m.getData()]);
  const frame = Buffer.alloc(4); frame[0] = 1; frame.writeUIntBE(plaintext.length, 1, 3);
  const iv = Buffer.alloc(12); iv.writeBigUInt64LE(BigInt(nonce), 4);
  const cipher = createCipheriv("chacha20-poly1305", key, iv, {authTagLength: 16}); cipher.setAAD(frame);
  return Buffer.concat([frame, cipher.update(plaintext), cipher.final(), cipher.getAuthTag()]);
}
async function fixture() {
  const key = Buffer.alloc(32, 7), socket = new Socket();
  const connection = new DataStreamConnection(socket as unknown as NetSocket, limits);
  connection.on("identification", (frame, identify) => {
    assert.equal(connection.decryptHDSFrame(frame, key), true);
    identify({connection: new EventEmitter() as never, accessoryToControllerEncryptionKey: key,
      controllerToAccessoryEncryptionKey: key, accessoryKeySalt: Buffer.alloc(32)});
  });
  let incoming = 0;
  const receive = (event: string, message: Record<string, unknown>): void => {
    socket.emit("data", encode({protocol: "dataSend", event}, message, ++incoming, key));
  };
  socket.emit("data", encode({protocol: "control", request: "hello", id: new Int64(1)}, {}, 0, key));
  socket.release(); await tick();
  return {connection, socket, key, receive};
}
const payload = (data = Buffer.from("fixture")): Record<string, unknown> => ({streamId: 1,
  packets: [{data, metadata: {dataType: "mediaFragment", dataSequenceNumber: 1, dataChunkSequenceNumber: 1,
    isLastDataChunk: true, dataTotalSize: data.length}}], endOfStream: false});

test("public awaited HDS retains exact released encrypted bytes and nonce", async () => {
  const {connection, socket, key} = await fixture(); const value = payload();
  let done = false; const sending = connection.sendEventAsync("dataSend", "data", value).then(() => { done = true; });
  assert.deepEqual(socket.writes.at(-1), encode({protocol: "dataSend", event: "data"}, value, 1, key));
  assert.equal(done, false); socket.release(); await sending; assert.equal(done, true);
  await connection.abortAndWait();
});

test("stalled encrypted socket has finite bytes and rejects 10000 extra frames before encoding", async () => {
  const {connection, socket} = await fixture(), value = payload(Buffer.alloc(262144));
  const first = connection.sendEventAsync("dataSend", "data", value), second = connection.sendEventAsync("dataSend", "data", value);
  void first.catch(() => {}); void second.catch(() => {});
  const before = socket.writableLength;
  for (let index = 0; index < 10000; index++) await assert.rejects(connection.sendEventAsync("dataSend", "data", value), /busy/);
  assert.equal(socket.writableLength, before); assert.ok(before <= limits.maximumPendingBytes);
  await connection.abortAndWait(); await assert.rejects(first); await assert.rejects(second);
});

test("local callback plus drain gates completion, and preabort does not encode", async () => {
  const socket = new Socket(), writer = new BoundedFrameWriter(socket, limits); let encoded = 0, done = false;
  assert.throws(() => writer.send(() => { encoded++; return Buffer.alloc(1); }, {signal: AbortSignal.abort()}), /aborted/);
  assert.equal(encoded, 0);
  const sending = writer.send(() => Buffer.alloc(100)).then(() => { done = true; });
  await tick(); assert.equal(done, false); assert.equal(socket.writableNeedDrain, true);
  socket.release(); await sending; assert.equal(done, true); assert.equal(writer.chargedBytes, 0); await writer.abort();
});

test("postenqueue abort fences all output and retains failed physical close ownership", async () => {
  const socket = new SlowDestroy(), writer = new BoundedFrameWriter(socket, limits), budget = new PhysicalConnectionBudget(1);
  const abort = new AbortController(); assert.equal(budget.admit(socket), true);
  const sending = writer.send(() => Buffer.alloc(100), {signal: abort.signal}); void sending.catch(() => {}); abort.abort();
  await assert.rejects(sending, /aborted/); await assert.rejects(writer.abort(), /cleanup-pending/);
  assert.equal(writer.chargedBytes, limits.maximumFrameBytes); assert.equal(budget.count, 1);
  assert.throws(() => writer.send(() => Buffer.alloc(1)), /closed/);
  socket.destroyRelease!(); await tick(); assert.equal(writer.chargedBytes, 0); assert.equal(budget.count, 0);
});

test("write watchdog and graceful-close escalation hard destroy stalled output", async () => {
  const first = new Socket(), writer = new BoundedFrameWriter(first, {...limits, writeTimeoutMs: 10});
  await assert.rejects(writer.send(() => Buffer.alloc(100)), /timeout/); await tick(); assert.equal(first.destroyed, true);
  const second = new Socket(), other = new BoundedFrameWriter(second, limits);
  const pending = other.send(() => Buffer.alloc(100)); void pending.catch(() => {});
  await other.close(); await assert.rejects(pending); assert.equal(other.retirementComplete, true);
});

test("global admission changes every public server limit before another HDS allocation", async () => {
  const budget = new PhysicalConnectionBudget(1), socket = new SlowDestroy();
  class Server extends EventEmitter { maxConnections = 9; dropMaxConnection = false; }
  const first = new Server(), second = new Server();
  budget.registerServer(first as never, 4); budget.registerServer(second as never, 2);
  assert.equal(budget.admit(socket), true); assert.equal(first.maxConnections, 0); assert.equal(second.maxConnections, 0);
  socket.destroy(); assert.equal(first.maxConnections, 0); socket.destroyRelease!(); await tick();
  assert.equal(first.maxConnections, 4); assert.equal(second.maxConnections, 2);
});

test("bounded serializer rejects cycles, depth, keys, huge strings, proxies and getters", () => {
  const cycle: Record<string, unknown> = {}; cycle.self = cycle;
  let deep: unknown = 1; for (let i = 0; i < 20; i++) deep = {value: deep};
  const keys = Object.fromEntries(Array.from({length: 1025}, (_, i) => [String(i), 1]));
  const getter = Object.defineProperty({}, "value", {enumerable: true, get() { throw new Error("getter executed"); }});
  for (const value of [cycle, deep, keys, "x".repeat(65537), new Proxy({}, {}), getter]) {
    const writer = new DataStreamWriter(128); assert.throws(() => DataStreamParser.encode(value, writer)); assert.equal(writer.length(), 0);
  }
});

test("recording rejects empty packets and frame encoder rejects oversized data before hidden growth", async () => {
  const {connection} = await fixture(); let closed=0;
  const delegate: CameraRecordingDelegate={updateRecordingActive(){},updateRecordingConfiguration(){},
    async *handleRecordingStreamRequest(){yield {data:Buffer.alloc(0),isLast:true};},closeRecordingStream(){closed++;}};
  new CameraRecordingStream(connection,delegate,2,6).startStreaming();await tick();assert.equal(closed,1);
  await connection.abortAndWait();
  const writer = new DataStreamWriter(16); assert.throws(() => DataStreamParser.encode({data: Buffer.alloc(1048576)}, writer));
  assert.ok(writer.getData().buffer.byteLength <= 16);
});

test("public recording stream ignores wrong/early ACK and honors only queued EOS", async () => {
  const {connection, socket, receive} = await fixture(); let acknowledged = 0, closed = 0;
  const delegate: CameraRecordingDelegate = {updateRecordingActive() {}, updateRecordingConfiguration() {},
    async *handleRecordingStreamRequest() { yield {data: Buffer.from("init"), isLast: true}; },
    closeRecordingStream() { closed++; }, acknowledgeStream() { acknowledged++; }};
  const stream = new CameraRecordingStream(connection, delegate, 2, 3); stream.startStreaming();
  receive("ack", {streamId: 3, endOfStream: true}); assert.equal(acknowledged, 0);
  socket.release(); await tick(); socket.release(); await tick();
  receive("ack", {streamId: 99, endOfStream: true}); receive("ack", {streamId: 3, endOfStream: false});
  assert.equal(acknowledged, 0); receive("ack", {streamId: 3, endOfStream: true});
  assert.equal(acknowledged, 1); assert.equal(closed, 0); receive("ack", {streamId: 3, endOfStream: true});
  assert.equal(acknowledged, 1); await connection.abortAndWait();
});

test("public recording close aborts a stalled DATA write before another generator packet", async () => {
  const {connection, socket} = await fixture(); let aborted = false, yielded = 0;
  const delegate: CameraRecordingDelegate = {updateRecordingActive() {}, updateRecordingConfiguration() {},
    async *handleRecordingStreamRequest(_id, signal) {
      signal!.addEventListener("abort", () => { aborted = true; }, {once: true});
      yielded++; yield {data: Buffer.alloc(300000), isLast: false};
      yielded++; yield {data: Buffer.alloc(100), isLast: true};
    }, closeRecordingStream() {}};
  const stream = new CameraRecordingStream(connection, delegate, 2, 4); stream.startStreaming();
  socket.release(); await tick(); stream.close(3);
  assert.equal(aborted, true); await connection.abortAndWait(); await tick(); assert.equal(yielded, 1);
});


test("multi-chunk initialization retains mediaInitialization metadata across its whole packet", async () => {
  const {connection, socket, key, receive} = await fixture();
  const delegate: CameraRecordingDelegate = {updateRecordingActive() {}, updateRecordingConfiguration() {},
    async *handleRecordingStreamRequest() {yield {data: Buffer.alloc(300000), isLast: false}; yield {data: Buffer.alloc(10), isLast: true};},
    closeRecordingStream() {}};
  const stream = new CameraRecordingStream(connection, delegate, 2, 5); stream.startStreaming();
  for (let i=0; i<8; i++) {socket.release(); await tick();}
  const messages: {packets: {metadata: {dataType: string; dataSequenceNumber: number; dataChunkSequenceNumber: number}}[]}[] = [];
  for (let index=2; index<socket.writes.length; index++) {
    const frame=socket.writes[index]!, iv=Buffer.alloc(12);iv.writeBigUInt64LE(BigInt(index),4);
    const decipher=createDecipheriv("chacha20-poly1305",key,iv,{authTagLength:16});
    decipher.setAAD(frame.subarray(0,4));decipher.setAuthTag(frame.subarray(-16));
    const plain=Buffer.concat([decipher.update(frame.subarray(4,-16)),decipher.final()]);
    messages.push(DataStreamParser.decode(new DataStreamReader(plain.subarray(1+plain[0]!))));
  }
  assert.deepEqual(messages.map(value=>value.packets[0]!.metadata.dataType), ["mediaInitialization","mediaInitialization","mediaFragment"]);
  assert.deepEqual(messages.map(value=>value.packets[0]!.metadata.dataSequenceNumber),[1,1,2]);
  assert.deepEqual(messages.map(value=>value.packets[0]!.metadata.dataChunkSequenceNumber),[1,2,1]);
  receive("ack",{streamId:5,endOfStream:true});await connection.abortAndWait();
});

test("recording shape rejects a proxy Buffer without invoking its length trap", async () => {
  const {connection}=await fixture();let reads=0;
  const proxy=new Proxy(Buffer.alloc(1),{get(){reads++;throw new Error("proxy trap");}});
  const value={streamId:1,packets:[{data:proxy,metadata:{dataType:"mediaFragment",dataSequenceNumber:1,
    dataChunkSequenceNumber:1,isLastDataChunk:true,dataTotalSize:1}}]};
  await assert.rejects(connection.sendEventAsync("dataSend","data",value));assert.equal(reads,0);await connection.abortAndWait();
});


test("bounded generic dataSend preserves released non-recording audio shape",async()=>{
  const {connection,socket}=await fixture();
  const message={streamId:new Int64(7),packets:[{data:Buffer.from("audio"),metadata:{rms:new Float32(0.2),sequenceNumber:new Int64(1)}}],endOfStream:false};
  const sending=connection.sendEventAsync("dataSend","data",message);socket.release();await sending;await connection.abortAndWait();
});

test("missing final ACK retires only its physical stream after the finite timeout", async context => {
  const first=await fixture(),other=await fixture();let closed=0;
  const delegate:CameraRecordingDelegate={updateRecordingActive(){},updateRecordingConfiguration(){},
    async *handleRecordingStreamRequest(){yield {data:Buffer.from("init"),isLast:true};},closeRecordingStream(){closed++;}};
  context.mock.timers.enable({apis:["setTimeout"]});
  try {
    new CameraRecordingStream(first.connection,delegate,2,8).startStreaming();
    for(let i=0;i<4;i++){first.socket.release();await tick();}
    context.mock.timers.tick(12000);assert.equal(closed,1);
    first.socket.release();await tick();context.mock.timers.tick(limits.closeGraceMs+1);await tick();
    assert.equal(first.socket.destroyed,true);assert.equal(other.socket.destroyed,false);
    const healthy=other.connection.sendEventAsync("control","healthy",{});other.socket.release();await healthy;
  } finally {
    await Promise.all([first.connection.abortAndWait(),other.connection.abortAndWait()]);context.mock.timers.reset();
  }
});

test("physical disconnect aborts pending delegate input and joins its logical cleanup",async()=>{
  const {connection,socket}=await fixture();let closed=0,finished=0,aborted=false;
  const budget=new PhysicalConnectionBudget(1);assert.equal(budget.admit(socket),true);
  const delegate:CameraRecordingDelegate={updateRecordingActive(){},updateRecordingConfiguration(){},
    async *handleRecordingStreamRequest(_id,signal){
      try {
        yield {data:Buffer.from("init"),isLast:false};
        await new Promise<never>((_resolve,reject)=>signal!.addEventListener("abort",()=>{aborted=true;reject(new Error("closed"));},{once:true}));
      } finally {finished++;}
    },closeRecordingStream(){closed++;}};
  new CameraRecordingStream(connection,delegate,2,9).startStreaming();
  for(let i=0;i<4;i++){socket.release();await tick();}
  socket.destroy();await tick();await connection.abortAndWait();await tick();
  assert.equal(aborted,true);assert.equal(closed,1);assert.equal(finished,1);assert.equal(budget.count,0);
});

test("CLOSE cannot skip its synchronous delivery fence when both writer slots are occupied",async()=>{
  const {connection,socket}=await fixture();let aborted=false,closed=0;
  const delegate:CameraRecordingDelegate={updateRecordingActive(){},updateRecordingConfiguration(){},
    async *handleRecordingStreamRequest(_id,signal){signal!.addEventListener("abort",()=>{aborted=true;},{once:true});
      yield {data:Buffer.alloc(100),isLast:false};},closeRecordingStream(){closed++;}};
  const stream=new CameraRecordingStream(connection,delegate,2,10);stream.startStreaming();await tick();
  assert.ok(socket.writableLength>100); // OPEN plus DATA remain charged and undrained.
  assert.doesNotThrow(()=>stream.close(3));assert.equal(aborted,true);assert.equal(closed,1);
  await connection.abortAndWait();assert.equal(socket.destroyed,true);
});

test("a rejected OPEN response owns its rejection and never starts the native generator",async()=>{
  const {connection,socket}=await fixture();let started=0,closed=0;
  const first=connection.sendEventAsync("control","occupied",{}),second=connection.sendEventAsync("control","occupied",{});
  void first.catch(()=>{});void second.catch(()=>{});
  const delegate:CameraRecordingDelegate={updateRecordingActive(){},updateRecordingConfiguration(){},
    async *handleRecordingStreamRequest(){started++;yield {data:Buffer.from("init"),isLast:true};},
    closeRecordingStream(){closed++;}};
  new CameraRecordingStream(connection,delegate,2,11).startStreaming();await tick();
  await connection.abortAndWait();await tick();
  assert.equal(started,0);assert.equal(closed,1);assert.equal(socket.destroyed,true);
  await assert.rejects(first);await assert.rejects(second);
});
