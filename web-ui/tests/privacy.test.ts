import assert from 'node:assert/strict';
import test from 'node:test';
import {
  cameraPrivacy,
  privacyAccess,
  privacyBody,
  privacyReason,
  validPrivacyPage,
} from '../src/lib/privacy.ts';
import type { CameraPrivacyPage, Device } from '../src/lib/types.ts';

const device = {
  camera_id: 'camera-a',
  scope: { profile_id: 'profile-a', account_generation: 3, device_generation: 4 },
} as Device;
const page: CameraPrivacyPage = {
  profile_id: 'profile-a',
  account_generation: 3,
  available: true,
  cameras: [
    {
      camera_id: 'camera-a',
      revision: 7,
      mode: 'ring',
      ring_live_allowed: true,
      ring_recording_allowed: false,
      ring_snapshot_allowed: false,
      ring_audio_allowed: false,
      source_fresh: true,
      effective_live_allowed: true,
      effective_recording_allowed: false,
      effective_snapshot_allowed: false,
      effective_audio_allowed: false,
      reason: 'ring_motion_disabled',
    },
  ],
};

test('privacy writes use the exact camera revision and do not include effective permissions', () => {
  assert.equal(validPrivacyPage(page), true);
  assert.deepEqual(privacyBody({ ...page, revision: 99 } as CameraPrivacyPage, device, 'homekit'), {
    profile_id: 'profile-a',
    account_generation: 3,
    camera_id: 'camera-a',
    expected_revision: 7,
    mode: 'homekit',
  });
  assert.equal(cameraPrivacy(page, device)?.mode, 'ring');
});

test('stale source observations do not prevent saving a current scoped preference', () => {
  const stale = {
    ...page,
    cameras: [
      { ...page.cameras[0], source_fresh: false, ring_live_allowed: null, reason: 'source_stale' },
    ],
  };
  assert.equal(privacyBody(stale, device, 'ring').expected_revision, 7);
  assert.equal(privacyBody(stale, device, 'homekit').mode, 'homekit');
  assert.equal(privacyAccess(false, true), 'Unknown');
  assert.equal(privacyAccess(true, null), 'Unknown');
  assert.equal(privacyAccess(true, false), 'Blocked');
  assert.equal(privacyAccess(true, true), 'Allowed');
});

test('missing, malformed, duplicate, and cross-account privacy data never grants a write', () => {
  const unavailable = { profile_id: null, account_generation: null, available: false, cameras: [] };
  assert.equal(validPrivacyPage(unavailable), true);
  assert.equal(validPrivacyPage({ ...unavailable, available: true }), false);
  assert.throws(() => privacyBody(unavailable, device, 'homekit'));
  for (const change of [
    { mode: 'other' },
    { revision: -1 },
    { revision: 1.1 },
    { effective_live_allowed: null },
    { ring_live_allowed: 'true' },
    { ring_audio_allowed: 'false' },
    { effective_audio_allowed: null },
    { source_fresh: undefined },
  ])
    assert.equal(
      validPrivacyPage({ ...page, cameras: [{ ...page.cameras[0], ...change }] }),
      false,
    );
  assert.equal(validPrivacyPage({ ...page, cameras: [page.cameras[0], page.cameras[0]] }), false);
  for (const change of [
    { profile_id: 'other' },
    { account_generation: 4 },
    { available: false },
    { cameras: [] },
  ])
    assert.throws(() => privacyBody({ ...page, ...change }, device, 'homekit'));
  assert.throws(() => privacyBody(page, { ...device, camera_id: 'other' }, 'ring'));
});

test('privacy reason text is allowlisted and describes source limits rather than active recording', () => {
  assert.match(privacyReason('ring_motion_disabled'), /manual Live View is checked separately/);
  assert.match(privacyReason('ring_audio_disabled'), /Live View and recording/);
  assert.match(privacyReason('ring_audio_disabled'), /turn off Record Audio in Apple Home/);
  assert.equal(privacyReason(null), '');
  assert.equal(
    privacyReason('<untrusted Ring message>'),
    'Current privacy information is unavailable. Refresh this camera to check again.',
  );
  assert.match(privacyReason('source_stale'), /out of date/);
});
