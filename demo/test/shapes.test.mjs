import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from './load.mjs';
import { shapeOf, assertCompatible } from './shape.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

test('shapeOf describes nested types, arrays by first element', () => {
  assert.deepEqual(shapeOf({ a: 1, b: [{ c: 's' }], d: null, e: [] }), { a: 'number', b: [{ c: 'string' }], d: 'null', e: [] });
});

test('assertCompatible fails on a missing key or a type change, allows extra keys', () => {
  assertCompatible({ a: 2, x: 1 }, { a: 1 });
  assert.throws(() => assertCompatible({}, { a: 1 }));
  assert.throws(() => assertCompatible({ a: 'x' }, { a: 1 }));
  assert.throws(() => assertCompatible({ a: [{ b: 1 }] }, { a: [{ b: 1, c: 2 }] }));
  assertCompatible({ a: [] }, { a: [{ b: 1 }] });
});

function loadRouter() {
  let files = [];
  try { files = readdirSync(join(ROOT, 'runtime', 'handlers')).filter((f) => f.endsWith('.js')).sort(); } catch { files = []; }
  return load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', ...files.map((f) => `handlers/${f}`)]);
}

test('every template with a handler route matches the handler output shape', async () => {
  const dir = join(ROOT, 'templates');
  const files = existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith('.json') && !f.startsWith('ws_')) : [];
  const self = loadRouter();
  const now = Date.UTC(2026, 0, 1, 12, 0, 0);
  for (const f of files) {
    const t = JSON.parse(readFileSync(join(dir, f), 'utf8'));
    if (!t.key || t.status >= 400) continue;
    const sp = t.key.indexOf(' ');
    const path = t.key.slice(sp + 1).replace(/:[A-Za-z_]+/g, 'atlas');
    const found = self.DemoRouter.find(t.key.slice(0, sp), new URL(path, 'http://demo.example'));
    if (!found) continue;
    const ctx = { now, params: found.params, query: new URLSearchParams(), body: null, fleet: self.DemoFleet };
    const out = await found.route.handler(ctx);
    if (out instanceof self.Response || out === undefined) continue;
    assertCompatible(JSON.parse(JSON.stringify(out)), t.body);
  }
});
