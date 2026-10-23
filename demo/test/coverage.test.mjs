import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from './load.mjs';
import { scanEndpoints, covers, formatMissing, DEFAULT_DIRS } from '../scan.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const exceptions = JSON.parse(readFileSync(join(HERE, '..', 'coverage-exceptions.json'), 'utf8'));

function routerKeys() {
  const dir = join(HERE, '..', 'runtime', 'handlers');
  let files = [];
  try { files = readdirSync(dir).filter((f) => f.endsWith('.js')).sort(); } catch { files = []; }
  const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', ...files.map((f) => `handlers/${f}`)]);
  return self.DemoRouter.keys();
}

test('every GET and stream endpoint the frontends call has a demo route', () => {
  const keys = routerKeys();
  const seen = new Set();
  const missing = [];
  const writes = new Set();
  const used = new Set();
  for (const e of scanEndpoints(DEFAULT_DIRS)) {
    if (e.pattern === null) continue;
    const id = `${e.method} ${e.pattern}`;
    if (id in exceptions) { used.add(id); continue; }
    if (covers(keys, e.method, e.pattern)) continue;
    if (e.method !== 'GET') { writes.add(id); continue; }
    if (!seen.has(id)) { seen.add(id); missing.push(formatMissing(e)); }
  }
  if (writes.size) console.log(`unrouted writes (information only): ${writes.size}`);
  assert.equal(missing.length, 0, `Endpoints without a demo route:\n${missing.join('\n')}`);
  const stale = Object.keys(exceptions).filter((k) => !used.has(k));
  assert.deepEqual(stale, [], `coverage-exceptions.json entries no scanned call matches:\n${stale.join('\n')}`);
});

test('covers matches :p against :name and literals exactly; failures format as METHOD pattern  (file:line)', () => {
  const keys = ['GET /api/agents/:id/info', 'POST /api/x'];
  assert.equal(covers(keys, 'GET', '/api/agents/:p/info'), true);
  assert.equal(covers(keys, 'GET', '/api/agents/a1/info'), true);
  assert.equal(covers(keys, 'GET', '/api/agents/:p/log'), false);
  assert.equal(covers(keys, 'GET', '/api/x'), false);
  assert.equal(formatMissing({ method: 'GET', pattern: '/api/a/:p', file: 'f.js', line: 7 }), 'GET /api/a/:p  (f.js:7)');
});
