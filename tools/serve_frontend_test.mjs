import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';

const root = resolve(import.meta.dirname, '..', 'mobile_server', 'static_dist');
const types = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
};

createServer(async (request, response) => {
  if (request.url === '/healthz') {
    response.end('ok');
    return;
  }
  const url = new URL(request.url || '/', 'http://127.0.0.1');
  let relative = url.pathname.startsWith('/static/') ? url.pathname.slice(8) : 'index.html';
  if (url.pathname === '/login' || url.pathname === '/register') relative = 'auth.html';
  const path = resolve(root, relative);
  if (path !== root && !path.startsWith(`${root}${sep}`)) {
    response.writeHead(403).end();
    return;
  }
  try {
    if (!(await stat(path)).isFile()) throw new Error('not a file');
    const body = await readFile(path);
    response.writeHead(200, { 'Content-Type': types[extname(path)] || 'application/octet-stream' });
    response.end(body);
  } catch {
    response.writeHead(404).end();
  }
}).listen(43178, '127.0.0.1');
