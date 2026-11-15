import assert from 'node:assert/strict';
import test from 'node:test';
import {
  recordingEligibility,
  recordingReadiness,
  recordingSources,
} from '../src/lib/recording.ts';

test('recording eligibility depends on video support, not power, model, or source count', () => {
  for (const mains_confirmed of [true, false, undefined]) {
    assert.equal(
      recordingEligibility(true, true, true, {
        recording_supported: true,
        mains_confirmed,
        model: 'Unknown',
      }).eligible,
      true,
    );
  }
  for (const recording_supported of [false, undefined, 'true']) {
    assert.equal(recordingEligibility(true, true, true, { recording_supported }).eligible, false);
  }
  assert.equal(
    recordingEligibility(false, true, true, { recording_supported: true }).eligible,
    false,
  );
  assert.equal(
    recordingEligibility(true, false, true, { recording_supported: true }).eligible,
    false,
  );
  assert.equal(
    recordingEligibility(true, true, false, { recording_supported: true }).eligible,
    false,
  );
  assert.equal(recordingEligibility(true, true, true, undefined).eligible, false);
});

test('source usage reports an absent camera cap without claiming unlimited resources', () => {
  assert.equal(
    recordingSources(true, { capacity: null, reserved: 7 }),
    '7 sources reserved · No configured camera cap',
  );
  assert.equal(recordingSources(true, { capacity: 2, reserved: 2 }), '2 of 2 sources reserved');
  for (const value of [
    null,
    {},
    { capacity: null, reserved: -1 },
    { capacity: null, reserved: '7' },
  ])
    assert.equal(recordingSources(true, value), 'Unavailable');
  assert.equal(recordingSources(false, { capacity: null, reserved: 7 }), 'Unavailable');
});

test('source readiness reflects known observed states and fails closed for stale or unknown values', () => {
  assert.equal(recordingReadiness(true, 'ready'), 'Pre-event buffer ready');
  assert.equal(recordingReadiness(true, 'disabled'), 'Not buffering');
  assert.equal(recordingReadiness(true, 'warming'), 'Preparing pre-event buffer');
  assert.equal(recordingReadiness(true, 'cleanup_pending'), 'Waiting for source cleanup');
  assert.equal(recordingReadiness(false, 'ready'), 'Unknown');
  assert.equal(recordingReadiness(true, '<untrusted status>'), 'Unknown');
});
