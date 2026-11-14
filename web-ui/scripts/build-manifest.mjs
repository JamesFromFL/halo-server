/** Deterministic source and static asset manifest; never includes build timestamps. */
import { createHash } from 'node:crypto';
import { readFile, readdir, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const output = resolve(root, '../src/halo_server/web/static');
const hash = (data) => createHash('sha256').update(data).digest('hex');
async function files(dir) {
  const result = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (['node_modules', '.git'].includes(entry.name)) continue;
    const full = join(dir, entry.name);
    if (entry.isSymbolicLink())
      throw new Error('Symlinks are not allowed in frontend build inputs');
    if (entry.isDirectory()) result.push(...(await files(full)));
    else if (entry.isFile()) result.push(full);
  }
  return result.sort();
}
const sources = [];
for (const path of await files(root))
  sources.push({
    path: relative(root, path).split('\\').join('/'),
    sha256: hash(await readFile(path)),
  });
const assets = [];
for (const path of await files(output))
  if (relative(output, path) !== 'build-manifest.json')
    assets.push({
      path: relative(output, path).split('\\').join('/'),
      sha256: hash(await readFile(path)),
    });
const manifest = {
  schema: 1,
  source_hash: hash(Buffer.from(JSON.stringify(sources))),
  source_files: sources,
  assets,
};
await writeFile(join(output, 'build-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
