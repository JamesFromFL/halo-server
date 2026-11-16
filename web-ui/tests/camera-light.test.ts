import assert from 'node:assert/strict';
import test from 'node:test';
import {
  cameraControl,
  cameraLightingView,
  lightingReason,
  motionDuration,
  validCameraLighting,
} from '../src/lib/camera-light.ts';
import type { CameraLighting, CameraControlPage, Device } from '../src/lib/types.ts';

const lighting: CameraLighting = {
  fresh: true,
  observed_age_seconds: 3.5,
  light: false,
  motion_duration_seconds: 60,
  manual_timer_status: 'unverified',
  activation_source: null,
  reason: null,
};

test('camera lighting retains independently known values from a partial fresh receipt', () => {
  for (const partial of [{ light: null }, { motion_duration_seconds: null }]) {
    const value = { ...lighting, ...partial, reason: 'source_unknown' as const };
    assert.equal(validCameraLighting(value), true);
    const view = cameraLightingView(value, true, true);
    assert.equal(view.light, value.light);
    assert.equal(view.motion_duration_seconds, value.motion_duration_seconds);
  }
  assert.equal(motionDuration(60), '1 minute');
  assert.equal(motionDuration(900), '15 minutes');
  assert.equal(motionDuration(30), '30 seconds');
  assert.equal(motionDuration(null), 'Unknown');
});

test('stale or superseded camera lighting cannot use legacy light state as a fallback', () => {
  for (const [value, fresh] of [
    [{ ...lighting, fresh: false, reason: 'source_stale' }, true],
    [{ ...lighting, fresh: false, reason: 'lighting_changed' }, true],
    [lighting, false],
  ] as const) {
    const view = cameraLightingView(value, fresh, true);
    assert.equal(view.light, null);
    assert.equal(view.motion_duration_seconds, null);
    assert.equal(view.observed_age_seconds, 3.5);
  }
  assert.equal(cameraLightingView(undefined, true, true).light, true);
  assert.equal(cameraLightingView(undefined, false, true).light, null);
});

test('an open lighting page ages out the receipt without a new server response', () => {
  assert.equal(cameraLightingView(lighting, true, true, 86.5).light, false);
  const expired = cameraLightingView(lighting, true, true, 86.501);
  assert.equal(expired.light, null);
  assert.equal(expired.motion_duration_seconds, null);
  assert.equal(expired.reason, 'source_stale');
  assert.equal(
    cameraLightingView({ ...lighting, observed_age_seconds: null }, true, true).light,
    null,
  );
});

test('malformed lighting data cannot imply zero, an indefinite timer, or a reported activation cause', () => {
  for (const fields of [
    { light: 'on' },
    { observed_age_seconds: -1 },
    { observed_age_seconds: Infinity },
    { motion_duration_seconds: 0 },
    { motion_duration_seconds: 60.1 },
    { motion_duration_seconds: 901 },
    { motion_duration_seconds: '60' },
    { manual_timer_status: 'until_off' },
    { activation_source: 'motion' },
    { reason: 'private native text' },
  ]) {
    const value = { ...lighting, ...fields };
    assert.equal(validCameraLighting(value), false);
    assert.equal(cameraLightingView(value, true, true).light, null);
    assert.equal(cameraLightingView(value, true, true).motion_duration_seconds, null);
  }
  assert.equal(
    lightingReason('private native text'),
    'Ring has not provided all lighting details. Missing values remain unknown.',
  );
});

test('camera controls remain bound to a unique current account and device generation', () => {
  const device = {
    camera_id: '1',
    scope: { profile_id: 'account-a', account_generation: 2, device_generation: 3 },
  } as Device;
  const row = {
    camera_id: '1',
    device_generation: 3,
    has_light: true,
    has_siren: false,
    light: false,
    siren: null,
    lighting,
  };
  const page: CameraControlPage = {
    profile_id: 'account-a',
    account_generation: 2,
    cameras: [row],
  };
  assert.equal(cameraControl(page, device), row);
  for (const changed of [
    { ...page, profile_id: 'other' },
    { ...page, account_generation: 3 },
    { ...page, cameras: [row, row] },
    { ...page, cameras: [{ ...row, device_generation: 4 }] },
  ])
    assert.equal(cameraControl(changed, device), undefined);
});
