import { readdir, readFile } from 'node:fs/promises';
import { extname, join, relative } from 'node:path';

const root = process.cwd();
const encodedTerms = [
  'bnNmdw==', 'cG9ybg==', 'cG9ybm1hc3Rlcg==', 'c25vZnM=', 'dGV4dGZ1c2lvbg==',
  'a3JlYS1tdXNl', 'cXdlbi1lZGl0', 'YWR1bHQ=', 'c2V4', 'cHVzc3k=', 'dmFnaW5h',
  'ZGlsZG8=', 'YW51cw==', 'cGVuaXM=', 'YnJlYXN0', 'bmlwcGxl', 'aGVudGFp',
  'ZXJvdGlj', '5oiQ5Lq6', '6Imy5oOF', '6KO45L2T', '6KO46Zyy', '5Lmz5oi/',
];
const terms = encodedTerms.map(value => Buffer.from(value, 'base64').toString('utf8'));
const patterns = terms.map(term => {
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return /^[a-z-]+$/i.test(term)
    ? new RegExp(`(?<![a-z])${escaped}(?![a-z])`, 'i')
    : new RegExp(escaped, 'i');
});
const extensions = new Set([
  '.bat', '.css', '.html', '.js', '.json', '.md', '.mjs', '.ps1', '.py', '.sh',
  '.toml', '.txt', '.yaml', '.yml',
]);
const ignoredDirectories = new Set([
  '.git', '.venv', 'data', 'node_modules', 'playwright-report', 'runtime',
  'static_dist', 'test-results',
]);
const ignoredFiles = new Set(['tools/audit_public_content.mjs']);

async function filesIn(directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.isSymbolicLink()) continue;
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      if (!ignoredDirectories.has(entry.name)) result.push(...await filesIn(path));
    } else if (extensions.has(extname(entry.name).toLowerCase())) {
      result.push(path);
    }
  }
  return result;
}

const findings = [];
for (const path of await filesIn(root)) {
  const name = relative(root, path).replaceAll('\\', '/');
  if (ignoredFiles.has(name)) continue;
  const lines = (await readFile(path, 'utf8')).split(/\r?\n/);
  lines.forEach((line, index) => {
    const lowered = line.toLowerCase();
    if (patterns.some(pattern => pattern.test(lowered))) findings.push(`${name}:${index + 1}`);
  });
}

if (findings.length) {
  console.error('Public-content audit failed:');
  findings.forEach(item => console.error(`  ${item}`));
  process.exitCode = 1;
} else {
  console.log('Public-content audit passed.');
}
