/** Keep a real browser cache while replacing the server with a new release. */
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import { browserName, testBrowser } from './browser-engine.mjs';
const url = process.env.HALO_TEST_URL;
const output = process.env.HALO_BROWSER_OUTPUT;
if (!url || !/^https:\/\/127\.0\.0\.1:[0-9]+$/.test(url) || !output?.startsWith('/'))
  throw new Error('Upgrade test requires an isolated loopback URL and output directory');
const browser = await testBrowser.launch({ headless: true });
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();
  const errors = [];
  const missing = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('response', (response) => {
    if (response.status() === 404) missing.push(new URL(response.url()).pathname);
  });
  await page.goto(url);
  await page.getByRole('heading', { name: 'old release', exact: true }).waitFor();
  const ready = createInterface({ input: process.stdin });
  const changed = new Promise((resolve) => ready.once('line', resolve));
  console.log('READY_FOR_UPGRADE');
  await changed;
  ready.close();
  // No fresh context, hard reload, request interception, or cache clearing.
  const response = await page.reload();
  assert.equal(response.status(), 200);
  assert.equal(response.headers()['cache-control'], 'no-store');
  await page.getByRole('heading', { name: 'new release', exact: true }).waitFor();
  assert.deepEqual(errors, []);
  assert.deepEqual(missing, []);
  await page.screenshot({ path: `${output}/upgrade.png` });
  await writeFile(
    `${output}/upgrade-smoke.json`,
    JSON.stringify(
      { passed: true, engine: browserName, errors, missing, ordinary_cached_reload: true },
      null,
      2,
    ),
  );
} finally {
  await browser.close();
}
