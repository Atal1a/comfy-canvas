import { constants as zlibConstants, brotliCompressSync, gzipSync } from 'node:zlib';
import { createHash } from 'node:crypto';
import { cp, mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { minify } from 'terser';

const root = resolve(import.meta.dirname, '..');
const sourceRoot = resolve(root, 'mobile_server', 'static');
const outputRoot = resolve(root, 'mobile_server', 'static_dist');
const manifest = JSON.parse(await readFile(resolve(outputRoot, '.vite', 'manifest.json'), 'utf8'));

function manifestAsset(sourceName) {
  const suffix = `/mobile_server/static/${sourceName}`.replaceAll('\\', '/');
  const match = Object.entries(manifest).find(([key]) => `/${key.replaceAll('\\', '/')}`.endsWith(suffix));
  if (!match?.[1]?.file) throw new Error(`Vite manifest is missing ${sourceName}`);
  return `/static/${match[1].file}`;
}

const cssFiles = ['style.css', 'chat-spacing.css', 'mobile-media-guard.css', 'chat-redesign.css', 'interface.css'];
const scriptFiles = ['icons.js', 'mobile-media-guard.js', 'request.js', 'chat.js', 'app.js', 'interface.js', 'auth.js'];
const scriptAssets = new Map();

await mkdir(resolve(outputRoot, 'assets'), { recursive: true });
for (const name of scriptFiles) {
  const source = await readFile(resolve(sourceRoot, name), 'utf8');
  const result = await minify(source, {
    module: false,
    toplevel: false,
    ecma: 2020,
    compress: { toplevel: false },
    mangle: { toplevel: false },
    format: { comments: /^!/ },
  });
  if (!result.code) throw new Error(`Terser did not produce ${name}`);
  const hash = createHash('sha256').update(result.code).digest('base64url').slice(0, 10);
  const outputName = `${name.slice(0, -3)}-${hash}.js`;
  await writeFile(resolve(outputRoot, 'assets', outputName), result.code, 'utf8');
  scriptAssets.set(name, `/static/assets/${outputName}`);
}

function packageHtml(html) {
  for (const name of cssFiles) {
    const pattern = new RegExp(`href="/static/${name.replace('.', '\\.')}(?:\\?v=\\d+)?"`, 'g');
    html = html.replace(pattern, `href="${manifestAsset(name)}"`);
  }
  for (const name of scriptFiles) {
    const pattern = new RegExp(`<script src="/static/${name.replace('.', '\\.')}(?:\\?v=\\d+)?"></script>`, 'g');
    // These legacy entry points intentionally share browser globals. Keep them
    // as ordered classic scripts until each feature boundary has explicit imports.
    html = html.replace(pattern, `<script defer src="${scriptAssets.get(name)}"></script>`);
  }
  return html;
}

for (const name of ['index.html', 'auth.html']) {
  const html = packageHtml(await readFile(resolve(sourceRoot, name), 'utf8'));
  await writeFile(resolve(outputRoot, name), html, 'utf8');
}

for (const name of ['icons', 'brand-icon.svg', 'favicon.png', 'manifest-fraunces-v1.webmanifest', 'manifest.webmanifest', 'LUCIDE-LICENSE.txt']) {
  await cp(resolve(sourceRoot, name), resolve(outputRoot, name), { recursive: true, force: true });
}

const compressible = new Set(['.css', '.html', '.js', '.json', '.svg', '.txt', '.webmanifest']);
async function compressTree(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.name === '.vite' || entry.name.endsWith('.br') || entry.name.endsWith('.gz')) continue;
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      await compressTree(path);
      continue;
    }
    const extension = entry.name.slice(entry.name.lastIndexOf('.'));
    const info = await stat(path);
    if (!compressible.has(extension) || info.size < 1024) continue;
    const body = await readFile(path);
    await writeFile(`${path}.gz`, gzipSync(body, { level: 6 }));
    await writeFile(`${path}.br`, brotliCompressSync(body, {
      params: { [zlibConstants.BROTLI_PARAM_QUALITY]: 6 }
    }));
  }
}

await mkdir(outputRoot, { recursive: true });
await compressTree(outputRoot);
console.log(`Packaged hashed and precompressed frontend in ${outputRoot}`);
