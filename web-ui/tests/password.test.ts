import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { OWNER_USERNAME } from '../src/lib/auth.ts';
import { PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH } from '../src/lib/password.ts';

test('web password form limits match the authoritative backend validator', () => {
  const backend = readFileSync(
    new URL('../../src/halo_server/web/auth.py', import.meta.url),
    'utf8',
  );
  const policy = backend.match(
    /def _password\(value\):\s+return text\(value, minimum=(\d+), maximum=(\d+)\)/,
  );
  assert.ok(policy, 'Update the parity check if the authoritative password validator changes');
  assert.equal(PASSWORD_MIN_LENGTH, Number(policy[1]));
  assert.equal(PASSWORD_MAX_LENGTH, Number(policy[2]));
});

test('web owner identity matches the backend single-owner account', () => {
  const defaults = readFileSync(
    new URL('../../src/halo_server/web/defaults.py', import.meta.url),
    'utf8',
  );
  const owner = defaults.match(/OWNER_USERNAME = ["']([^"']+)["']/);
  assert.ok(owner, 'Update the parity check if the authoritative owner identity changes');
  assert.equal(OWNER_USERNAME, owner[1]);
});
