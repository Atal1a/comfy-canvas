import { access, readFile, readdir, stat } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..', 'mobile_server', 'static_dist');
const index = await readFile(resolve(root, 'index.html'), 'utf8');
const auth = await readFile(resolve(root, 'auth.html'), 'utf8');
if (index.includes('?v=') || auth.includes('?v=')) throw new Error('Runtime HTML still contains manual static versions');
if (!index.includes('<script defer ') || !auth.includes('<script defer ')) throw new Error('Runtime scripts are not deferred');

const references = [...index.matchAll(/(?:src|href)="\/static\/([^"]+)/g), ...auth.matchAll(/(?:src|href)="\/static\/([^"]+)/g)];
for (const match of references) await access(resolve(root, match[1]));

let rawBytes = 0;
let compressedBytes = 0;
for (const entry of await readdir(resolve(root, 'assets'))) {
  const path = resolve(root, 'assets', entry);
  if (entry.endsWith('.br')) compressedBytes += (await stat(path)).size;
  else if (!entry.endsWith('.gz')) rawBytes += (await stat(path)).size;
}
if (!compressedBytes) throw new Error('No precompressed Brotli assets were generated');
console.log(`Frontend build verified: ${(rawBytes / 1024).toFixed(1)} KB raw assets, ${(compressedBytes / 1024).toFixed(1)} KB Brotli variants`);
