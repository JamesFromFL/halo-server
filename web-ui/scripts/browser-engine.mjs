/** Keep the full console acceptance flow runnable in both supported test engines. */
import { chromium, firefox } from '@playwright/test';
const name = process.env.HALO_BROWSER ?? 'chromium';
if (!['chromium', 'firefox'].includes(name))
  throw new Error('HALO_BROWSER must be chromium or firefox');
export const browserName = name;
export const testBrowser = name === 'firefox' ? firefox : chromium;
