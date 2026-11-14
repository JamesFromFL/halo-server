/** Include locked npm dependency notices with the offline static artifact. */
import { readFile, readdir, writeFile } from 'node:fs/promises';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const lock = JSON.parse(await readFile(join(root, 'package-lock.json'), 'utf8'));
let output = await readFile(join(root, 'public/THIRD_PARTY_NOTICES.txt'), 'utf8');
for (const name of Object.keys(lock.packages)
  .filter((name) => name.startsWith('node_modules/'))
  .sort()) {
  const path = join(root, name);
  let metadata;
  try {
    metadata = JSON.parse(await readFile(join(path, 'package.json'), 'utf8'));
  } catch {
    continue;
  }
  output += `\n\n--- ${metadata.name} ${metadata.version} (${metadata.license ?? 'see notice'}) ---\n`;
  const notices = (await readdir(path))
    .filter((name) => /^(licen[sc]e|copying|notice)(\.|$)/i.test(name))
    .sort();
  for (const notice of notices) {
    try {
      output += await readFile(join(path, notice), 'utf8');
      output += '\n';
    } catch {
      /* Directory notices are not text files. */
    }
  }
}
await writeFile(resolve(root, '../src/halo_server/web/static/THIRD_PARTY_NOTICES.txt'), output);
