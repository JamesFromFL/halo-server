// Executed only as a bounded child. No account, discovery, ICE gathering or STUN.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import dgram from 'node:dgram';
import { performance } from 'node:perf_hooks';

const request = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
if (![20, 22, 24].includes(Number(process.versions.node.split('.')[0]))) {
  throw new Error('use a released Ring supported Node major: 20, 22, or 24');
}
const result = { arrivals: [], forwarded: [], assembled: [], egress: [], sink: [],
  forwarded_unit: 'rtp_packet', code_executed: [] };
const hash = data => createHash('sha256').update(data).digest('hex');
const paths = request.reference.paths;
const requireRef = createRequire(path.join(paths.fork || paths.root, 'package.json'));
const load = async file => {
  const loaded = await import(pathToFileURL(file));
  return loaded.default && !loaded.RTCPeerConnection ? loaded.default : loaded;
};
const quiet = { log() {}, error() {}, warn() {}, debug() {} };
let pc, wrapper, werift, extracted = [];
let sendPacket;

if (request.kind === 'homebridge') {
  const mod = await load(paths.peer);
  werift = requireRef('werift');
  wrapper = new mod.WeriftPeerConnection();
  pc = wrapper.pc; // TypeScript private field in the exact released wrapper.
  wrapper.onVideoRtp.subscribe(packet => sendPacket(packet));
  result.code_executed.push('released Ring WeriftPeerConnection constructor and onVideoRtp callback',
    'released Werift 0.22.4 RTCRtpReceiver.handleRtpBySsrc/handleRTP');
} else {
  werift = await load(path.join(paths.fork, 'packages/webrtc/src/index.ts'));
  const ts = requireRef('typescript');
  const source = fs.readFileSync(paths.peer, 'utf8');
  const parsed = ts.createSourceFile(paths.peer, source, ts.ScriptTarget.Latest, true);
  function declaration(name) {
    const matches = [];
    function walk(node) {
      if (ts.isVariableDeclaration(node) && node.name.getText(parsed) === name
          && (name !== 'ret' || (ts.isNewExpression(node.initializer)
            && node.initializer.expression.getText(parsed) === 'RTCPeerConnection'))) matches.push(node);
      ts.forEachChild(node, walk);
    }
    walk(parsed);
    if (matches.length !== 1) throw new Error(`source shape changed: ${name}`);
    const node = matches[0];
    const text = `const ${node.getText(parsed)};`;
    extracted.push({ name, start_line: parsed.getLineAndCharacterOfPosition(node.getStart()).line + 1,
      sha256: hash(text) });
    return text;
  }
  const compiledCodecs = ts.transpileModule(fs.readFileSync(paths.codecs, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const codecModule = { exports: {} };
  new Function('require', 'module', 'exports', compiledCodecs)(name => {
    if (name !== './werift') throw new Error(`unexpected codec import: ${name}`);
    return werift;
  }, codecModule, codecModule.exports);
  // Execute unmodified, hash-verified source declarations, with only host services
  // supplied by the harness. The source callback still serializes RTP into sendTrack.
  const body = [declaration('ret'), 'const pc = ret;', declaration('setupAudioTranscevier'),
    declaration('setupVideoTransceiver'),
    'setupAudioTranscevier(pc.addTransceiver("audio", { direction: "sendrecv" }));',
    'setupVideoTransceiver(pc.addTransceiver("video", { direction: "recvonly" }));',
    'return pc;'].join('\n');
  const compiled = ts.transpileModule(body, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const waitClosed = peer => new Promise(resolve => peer.connectionStateChange.subscribe(state => {
    if (state === 'closed') resolve();
  }));
  const build = new Function('RTCPeerConnection', 'requiredAudioCodecs', 'requiredVideoCodec',
    'setup', 'getWeriftIceServers', 'console', 'rtspServer', 'waitClosed',
    'FullIntraRequest', 'RtcpPayloadSpecificFeedback', 'getNaluTypesInNalu',
    `let audioTransceiver, gotAudio = false, gotVideo = false;
     const timeStart = Date.now(), audioTrack = 'audio', videoTrack = 'video';
     const timeout = setTimeout(() => {}, 20000); timeout.unref();\n${compiled}`);
  pc = build(werift.RTCPeerConnection, codecModule.exports.requiredAudioCodecs,
    codecModule.exports.requiredVideoCodec, { configuration: { iceServers: [] } }, () => [],
    quiet, { sendTrack(_track, wire, rtcp) {
      if (!rtcp) sendPacket(werift.RtpPacket.deSerialize(wire));
    } }, waitClosed, werift.FullIntraRequest, werift.RtcpPayloadSpecificFeedback, () => new Set());
  result.extracted_declarations = extracted;
  result.code_executed.push('Scrypted wrtc-to-rtsp.ts ret constructor, setupAudioTranscevier, setupVideoTransceiver declarations',
    'Scrypted webrtc-required-codecs.ts actual exports',
    'pinned koush Werift RTCRtpReceiver.handleRtpBySsrc/handleRTP');
  result.host_substitutions = ['RTSP sendTrack sink, console/debug NAL logging, closed-state wait',
    'empty ICE servers: no ICE gathering or transport establishment is invoked'];
}

const sink = dgram.createSocket('udp4');
const sender = dgram.createSocket('udp4');
await new Promise(resolve => sink.bind(0, '127.0.0.1', resolve));
// Measurement collection must accommodate one >128-packet IDR while this same
// event loop is injecting its burst. This is a diagnostic socket, not a playout buffer.
sink.setRecvBufferSize(1024 * 1024);
result.diagnostic_socket_receive_buffer_bytes = sink.getRecvBufferSize();
let start = performance.now();
const elapsed = () => request.instrument ? performance.now() - start : 0;
const outputHash = createHash('sha256');
const aus = new Map(request.trace.aus.map(a => [a.timestamp, { ...a, received: new Map(), done: false }]));
const rtpBySequence = new Map(request.trace.packets.map(p => [p.sequence, p]));
let pendingSends = 0;
sink.on('message', wire => result.sink.push({ id: wire.readUInt32BE(), ms: elapsed() }));

function reassemble(au) {
  const buffers = [];
  let fragmented = false;
  for (const sequence of au.sequences) {
    const payload = au.received.get(sequence);
    if (!payload) return;
    const kind = payload[0] & 31;
    if (kind === 28) {
      if (payload.length < 3) throw new Error('invalid FU-A');
      if (payload[1] & 128) {
        if (fragmented) throw new Error('overlapping FU-A');
        fragmented = true;
        buffers.push(Buffer.from([0, 0, 0, 1, (payload[0] & 0xe0) | (payload[1] & 31)]));
      } else if (!fragmented) throw new Error('FU-A missing start');
      buffers.push(payload.subarray(2));
      if (payload[1] & 64) fragmented = false;
    } else {
      if (fragmented) throw new Error('incomplete FU-A');
      buffers.push(Buffer.from([0, 0, 0, 1]), payload);
    }
  }
  if (fragmented) return;
  const data = Buffer.concat(buffers);
  if (hash(data) !== au.annexb_sha256) throw new Error('independent AU payload check failed');
  au.done = true;
  result.assembled.push({ au: au.index, ms: elapsed(), bytes: data.length, sha256: hash(data) });
}

sendPacket = packet => {
  const item = rtpBySequence.get(packet.header.sequenceNumber);
  if (!item) throw new Error('receiver emitted an unexpected RTP packet');
  const wire = packet.serialize();
  outputHash.update(wire);
  result.forwarded.push({ id: item.id, au: item.au, ms: elapsed(), bytes: wire.length });
  const au = aus.get(packet.header.timestamp);
  au.received.set(packet.header.sequenceNumber, packet.payload);
  if (!au.done && au.received.size === au.packets) reassemble(au);
  // The same metadata-only UDP sink boundary is used by Halo. This is diagnostic
  // egress, not encryption, RTSP framing, or the HomeKit application sender.
  const token = Buffer.alloc(4);
  token.writeUInt32BE(item.id);
  pendingSends++;
  sender.send(token, sink.address().port, '127.0.0.1', error => {
    pendingSends--;
    if (error) throw error;
    result.egress.push({ id: item.id, ms: elapsed() });
  });
};

try {
  const offer = await pc.createOffer(); // serialization only; never setLocalDescription
  result.offer = offer.sdp;
  result.profile = { bundlePolicy: pc.config.bundlePolicy,
    transceivers: pc.getTransceivers().map(t => ({ kind: t.kind, direction: t.direction, mid: t.mid })),
    codecs: pc.config.codecs };
  const video = pc.getTransceivers().find(t => t.kind === 'video');
  const codec = pc.config.codecs.video.find(c => c.mimeType.toLowerCase() === 'video/h264');
  // All traces use the same synthetic negotiated payload type. Offer parameters
  // are retained; transport-cc was not selected by the synthetic sender.
  const selected = new werift.RTCRtpCodecParameters({ ...codec, payloadType: 96,
    rtcpFeedback: codec.rtcpFeedback.filter(f => f.type !== 'transport-cc') });
  const receiver = video.receiver;
  receiver.prepareReceive({ codecs: [selected], encodings: [{ ssrc: 0x12345678 }], headerExtensions: [] });
  const track = new werift.MediaStreamTrack({ kind: 'video', ssrc: 0x12345678, remote: true });
  receiver.addTrack(track);
  receiver.dtlsTransport.sendRtcp = async () => {
    (result.rtcp_feedback_ms ||= []).push(elapsed());
  };
  video.onTrack.execute(track);
  start = performance.now();
  for (const item of request.trace.packets) {
    const remaining = start + item.due_ms - performance.now();
    if (remaining > 0) await new Promise(resolve => setTimeout(resolve, remaining));
    result.arrivals.push({ id: item.id, ms: elapsed() });
    receiver.handleRtpBySsrc(werift.RtpPacket.deSerialize(Buffer.from(item.wire, 'base64')), {});
  }
  await new Promise(resolve => setTimeout(resolve, 35));
  if (pendingSends) throw new Error('UDP sends remain pending');
} finally {
  wrapper?.close();
  await pc.close();
  await Promise.all([new Promise(resolve => sender.close(resolve)), new Promise(resolve => sink.close(resolve))]);
}
result.output_digest = outputHash.digest('hex');
result.cleanup = { peer_closed: pc.connectionState === 'closed', sockets_closed: true, pending_sends: pendingSends };
result.runtime = { node: process.version, werift_package: request.kind === 'homebridge' ? '0.22.4' : 'fork 0.22.2 at pinned commit' };
fs.writeFileSync(process.argv[3], JSON.stringify(result));
// Natural child exit is required; the parent enforces a wall-clock deadline and
// kills the entire process group if a reference leaks timers or sockets.
