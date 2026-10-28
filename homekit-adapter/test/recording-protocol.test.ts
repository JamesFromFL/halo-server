import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { FEATURES, ProtocolError, validateDirection, validateEnvelope, validateReply, type Envelope } from '../src/protocol.js';
const fixtures = JSON.parse(readFileSync(process.env.HALO_PROTOCOL_FIXTURES ?? new URL('../../../src/halo_server/homekit/protocol/v1/fixtures.json', import.meta.url), 'utf8')) as {
  valid: Envelope[];
};
const fixture = (kind: string) => structuredClone(fixtures.valid.find(item => item.kind === kind)!);
test('recording replies own exact boot, job, scope, request and sequence', () => {
  for (const kind of ['recording_settings_update', 'recording_open', 'recording_next', 'recording_stop']) {
    const request = fixture(kind), reply = fixture(kind.replace('settings_update', 'settings') + '_result');
    assert.equal(validateDirection(request, 'hap'), request);
    assert.equal(validateDirection(reply, 'python'), reply);
    assert.equal(validateReply(request, reply), reply);
    assert.throws(() => validateDirection(request, 'python'), ProtocolError);
    assert.throws(() => validateDirection(reply, 'hap'), ProtocolError);
    const changed = structuredClone(reply);
    if (kind === 'recording_next')
      changed.payload.sequence = 1;
    else if (kind === 'recording_settings_update')
      changed.payload.revision = 4;
    else
      changed.payload.job_id = '00000000-0000-0000-0000-000000000009';
    assert.throws(() => validateReply(request, changed), ProtocolError);
  }
});
test('recording selected limits preserve native level and GOP constraints', () => {
  for (const change of ['geometry', 'level', 'rate', 'audio', 'gop']) {
    const item = fixture('recording_open'), config = item.payload.configuration as {
      video: Record<string, unknown>;
      audio: Record<string, unknown>;
      i_frame_interval_ms: number;
    };
    if (change === 'geometry')
      config.video.width = 1279;
    if (change === 'level')
      Object.assign(config.video, {
        width: 1920, height: 1080, level: '3.1'
      });
    if (change === 'rate')
      config.video.max_bit_rate = 14001;
    if (change === 'audio')
      config.audio.bitrate_mode = 'cbr';
    if (change === 'gop')
      config.i_frame_interval_ms = 66;
    assert.throws(() => validateEnvelope(item), ProtocolError);
  }
});
test('recording handshake rejects missing or reordered extension marker', () => {
  const item = fixture('hello_ack');
  assert.deepEqual(item.payload.features, FEATURES);
  for (const features of [FEATURES.slice(0, -1), [...FEATURES].reverse()]) {
    const changed = structuredClone(item);
    changed.payload.features = features;
    assert.throws(() => validateEnvelope(changed), ProtocolError);
  }
});
