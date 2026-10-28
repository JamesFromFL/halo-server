import { Socket } from 'node:net';
import { Duplex } from 'node:stream';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
import { createCipheriv, createDecipheriv, randomUUID } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
process.env.HALO_TEST_NETWORK_RECORD = __NETWORK__;
createRequire(import.meta.url)(__GUARD__);
const root = __ROOT__, hap = await import(__HAP__);
const { ControlChannel } = await import(`${root}/ipc.js`), { BulkChannel } = await import(`${root}/bulk.js`);
const { AdapterSession } = await import(`${root}/session.js`), { HapRecordingDelegate } = await import(`${root}/recording.js`);
const { FEATURES } = await import(`${root}/protocol.js`);
const mode = __MODE__, audio = mode === 'audio', key = Buffer.alloc(32, 7), parts = new Map();
const control = new ControlChannel(new Socket({
  fd: Number(process.env.HALO_CONTROL_FD), readable: true, writable: true
}));
const binary = new Socket({
  fd: Number(process.env.HALO_BULK_FD), readable: true, writable: true
});
let session, bulk, boot, delegate, connection, stream, warm = false, ran = false, ending = false, stalled = false, incoming = 0, outgoing = 0, selected, qualityChanged = false, eos = false, closeReason = null;
const send = (kind, payload, request_id = randomUUID()) => control.send({
  v: 1, kind, boot_id: boot, request_id, payload
});
const pause = () => new Promise(resolve => setTimeout(resolve, 2));
function encode(header, message, nonce) {
  const h = new hap.DataStreamWriter(), m = new hap.DataStreamWriter();
  hap.DataStreamParser.encode(header, h);
  hap.DataStreamParser.encode(message, m);
  const plain = Buffer.concat([Buffer.from([h.length()]), h.getData(), m.getData()]), frame = Buffer.alloc(4), iv = Buffer.alloc(12);
  frame[0] = 1;
  frame.writeUIntBE(plain.length, 1, 3);
  iv.writeBigUInt64LE(BigInt(nonce), 4);
  const cipher = createCipheriv('chacha20-poly1305', key, iv, { authTagLength: 16 });
  cipher.setAAD(frame);
  return Buffer.concat([frame, cipher.update(plain), cipher.final(), cipher.getAuthTag()]);
}
class HdsSocket extends Duplex {
  remoteAddress = 'synthetic-no-network';
  callbacks = [];
  constructor() { super({ highWaterMark: 16 }); }
  _read() { }
  setNoDelay() { return this; }
  setKeepAlive() { return this; }
  _write(frame, encoding, callback) {
    try {
      const iv = Buffer.alloc(12);
      iv.writeBigUInt64LE(BigInt(outgoing++), 4);
      const cipher = createDecipheriv('chacha20-poly1305', key, iv, { authTagLength: 16 });
      cipher.setAAD(frame.subarray(0, 4));
      cipher.setAuthTag(frame.subarray(-16));
      const plain = Buffer.concat([cipher.update(frame.subarray(4, -16)), cipher.final()]);
      const header = hap.DataStreamParser.decode(new hap.DataStreamReader(plain.subarray(1, 1 + plain[0])));
      const value = hap.DataStreamParser.decode(new hap.DataStreamReader(plain.subarray(1 + plain[0])));
      if (header.protocol === 'dataSend' && header.event === 'data') {
        for (const packet of value.packets) {
          const id = packet.metadata.dataSequenceNumber;
          const entry = parts.get(id) ?? [];
          entry.push(Buffer.from(packet.data));
          parts.set(id, entry);
        }
        const fragment = value.packets[0].metadata.dataType === 'mediaFragment';
        if (!fragment && !qualityChanged && (mode === 'video' || mode === 'audio')) {
          qualityChanged = true;
          selected.videoCodec.parameters.bitRate = 3000;
          delegate.updateRecordingConfiguration(selected);
        }
        if ((mode === 'shutdown' || mode === 'privacy') && fragment && !stalled) {
          stalled = true;
          this.callbacks.push(callback);
          void send('status', { state: 'ready', reason: 'stalled' });
          if (mode === 'privacy')
            setImmediate(() => { void (async () => { delegate.updateRecordingActive(false); await delegate.changing; await delegate.fence(); await save(); await send('status', { state: 'ready', reason: 'done' }); })().catch(failed); });
          return;
        }
        if (value.endOfStream) {
          eos = true;
        }
        if (value.endOfStream)
          setImmediate(() => receive('ack', { streamId: 7, endOfStream: true }));
      }
      setImmediate(callback);
    }
    catch (error) {
      callback(error);
    }
  }
  _destroy(error, callback) { for (const done of this.callbacks.splice(0))
    done(error ?? new Error('closed')); callback(error); }
}
let socket;
function receive(event, message) { socket.emit('data', encode({ protocol: 'dataSend', event }, message, ++incoming)); }
async function save() { for (const [id, chunks] of parts)
  await writeFile(`${__OUTPUT__}/packet-${id}.mp4`, Buffer.concat(chunks), { mode: 0o600 }); await writeFile(`${__OUTPUT__}/hds.json`, JSON.stringify({
  packets: parts.size, stalled, encryptedFrames: outgoing, eos, closeReason
}), { mode: 0o600 }); }
async function failed(error) { if (!ending)
  await send('status', { state: 'degraded', reason: String(error?.message ?? error).slice(0, 200) }); }
async function exercise() {
  try {
    while (!session.inventoryComplete)
      await pause();
    const native = session.camera('42');
    const base = {
      synchronized: true, streaming: false, live_audio: false, snapshots: false, recording: false, recording_audio: false, talkback: false, controls: false
    };
    delegate = new HapRecordingDelegate(native, {
      revision: 0, enabled: true, active: false, audio_active: false, configuration: null
    }, {
      active: () => true,
      policy: () => ({ ...base, ...delegate.permissions }), closeTransport: async () => { await connection?.abortAndWait(); }, failure: () => { void failed(new Error('delegate-failed')); }
    });
    const originalClose = delegate.closeRecordingStream.bind(delegate);
    delegate.closeRecordingStream = (id, reason) => { closeReason = reason; originalClose(id, reason); };
    selected = {
      prebufferLength: 4000, eventTriggerTypes: [1], mediaContainerConfiguration: { type: 0, fragmentLength: 4000 },
      videoCodec: {
        type: 0, parameters: {
          profile: 1, level: 2, bitRate: 2000, iFrameInterval: 1000
        }, resolution: [1280, 720, 15]
      },
      audioCodec: {
        type: 0, audioChannels: 1, bitrateMode: 0, samplerate: 2, bitrate: 32
      }
    };
    delegate.updateRecordingConfiguration(selected);
    delegate.updateRecordingActive(true);
    await delegate.setAudioActive(audio);
    await delegate.changing;
    await send('status', { state: 'ready', reason: 'settings-ready' });
    while (!warm)
      await pause();
    socket = new HdsSocket();
    connection = new hap.DataStreamConnection(socket, {
      maximumFrameBytes: 266240, maximumPendingFrames: 2, maximumPendingBytes: 532480, writeTimeoutMs: 2000, closeGraceMs: 5, closeTimeoutMs: 100
    });
    connection.on('identification', (frame, identify) => { if (!connection.decryptHDSFrame(frame, key))
      throw new Error('decrypt'); identify({
      connection: new EventEmitter(), accessoryToControllerEncryptionKey: key, controllerToAccessoryEncryptionKey: key, accessoryKeySalt: Buffer.alloc(32)
    }); });
    socket.emit('data', encode({
      protocol: 'control', request: 'hello', id: new hap.Int64(1)
    }, {}, 0));
    await pause();
    stream = new hap.CameraRecordingStream(connection, delegate, 2, 7);
    const closed = new Promise(resolve => stream.once('closed', resolve));
    stream.startStreaming();
    if (mode === 'shutdown' || mode === 'privacy') {
      await closed;
      if (!stalled && !ending) {
        await save();
        throw new Error('HDS-closed-before-stall-' + closeReason);
      }
      return;
    }
    await closed;
    if (!eos)
      throw new Error('HDS-closed-' + closeReason);
    await delegate.fence();
    await delegate.changing;
    await save();
    await send('status', { state: 'ready', reason: 'done' });
  }
  catch (error) {
    await failed(error);
  }
}
control.on('message', message => {
  void (async () => {
    if (message.kind === 'hello') {
      boot = message.boot_id;
      bulk = new BulkChannel(binary, {
        bootId: boot, role: 'hap', sendControl: send
      });
      bulk.start();
      session = new AdapterSession(boot, { send: m => control.send(m), bulk });
      await control.send({
        ...message, kind: 'hello_ack', payload: {
          role: 'hap', build_id: message.payload.build_id, protocol: 1, features: [...FEATURES], pid: process.pid
        }
      });
      return;
    }
    if (message.kind === 'ping') {
      await control.send({
        ...message, kind: 'pong', payload: {}
      });
      return;
    }
    if (message.kind === 'shutdown') {
      ending = true;
      await delegate?.close();
      await connection?.abortAndWait();
      await save();
      session.close();
      await bulk.close();
      await control.send({
        ...message, kind: 'shutdown_ack', payload: {}
      });
      control.close();
      return;
    }
    if (message.kind === 'status' && message.payload.reason === 'warm-ready') {
      warm = true;
      return;
    }
    await session.receive(message);
    if (message.kind === 'inventory_reset' && !ran) {
      ran = true;
      void exercise();
    }
  })().catch(failed);
});
control.on('closed', () => { session?.close(); binary.destroy(); });
