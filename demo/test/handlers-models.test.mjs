import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { load } from './load.mjs';
import { assertCompatible } from './shape.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const self = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'demo-state.js', 'handlers/overall.js', 'handlers/models.js']);
const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);

async function call(path, now = NOW) {
  const url = new URL(path, 'http://demo.example');
  const found = self.DemoRouter.find('GET', url);
  assert.ok(found, `no route for GET ${path}`);
  const ctx = { now, params: found.params, query: url.searchParams, body: null, fleet: self.DemoFleet };
  const out = await found.route.handler(ctx);
  return out instanceof self.Response ? out : JSON.parse(JSON.stringify(out));
}

function keysOf(v, p = '') {
  if (Array.isArray(v)) return v.length ? keysOf(v[0], p + '[]') : [p + '[]'];
  if (v && typeof v === 'object') return Object.keys(v).sort().flatMap((k) => [p + '.' + k, ...keysOf(v[k], p + '.' + k)]);
  return [];
}

const ROUTES = ['/api/gateway/v1/models', '/api/benchmark/results', '/api/benchmark/live/latest', '/api/llm/autotune/status'];

test('captured templates are shape-compatible with their routes', async () => {
  for (const p of ROUTES) {
    const file = join(ROOT, 'templates', `GET_${p.replace(/[^A-Za-z0-9]/g, '_')}.json`);
    assert.ok(existsSync(file), file);
    assertCompatible(await call(p), JSON.parse(readFileSync(file, 'utf8')).body);
  }
});

test('model list, config and profiles agree for every llama machine', async () => {
  for (const [agent, first] of [['atlas', 'Llama-3.1-8B-Instruct-Q8_0'], ['forge', 'Qwen2.5-14B-Instruct-Q5_K_M'], ['quill', 'Phi-3.5-mini-instruct-Q4_K_M']]) {
    const q = `?agent=${agent}`;
    const models = (await call('/api/llm/models' + q)).data;
    const cfg = await call('/api/llm/config' + q);
    const prof = await call('/api/llm/profiles' + q);
    assert.equal(models[0].id, first);
    assert.equal(models[0].status.value, 'loaded');
    assert.deepEqual(Object.keys(cfg), models.map((m) => m.id));
    assert.deepEqual(Object.keys(prof), Object.keys(cfg));
    assert.ok(Object.values(prof).every((p) => p.active in p.profiles));
  }
  assert.equal((await call('/api/llm/models?agent=nope')).data[0].id, 'Llama-3.1-8B-Instruct-Q8_0');
  assert.equal((await call('/api/llm/models?agent=studio')).data[0].id, 'Llama-3.1-8B-Instruct-Q8_0');
  assert.equal((await call('/api/llm/config?agent=quill'))['Phi-3.5-mini-instruct-Q4_K_M']['n-gpu-layers'], '0');
});

test('offline results carry the context switch the model cards compare', async () => {
  for (const agent of ['atlas', 'forge', 'quill']) {
    const cfg = await call('/api/llm/config?agent=' + agent);
    const res = (await call('/api/benchmark/results?agent=' + agent)).results;
    assert.equal(res.length, Object.keys(cfg).length);
    for (const r of res) assert.equal(r.switches.find((s) => s.flag === '-c').value, cfg[r.model_id]['ctx-size']);
  }
  assert.deepEqual((await call('/api/benchmark/results?provider=lms')).results, []);
  const atlas = (await call('/api/benchmark/results')).results;
  const forge = (await call('/api/benchmark/results?agent=forge')).results;
  assert.ok(atlas[0].avg_gen_tps > forge[0].avg_gen_tps);
});

test('live runs list, open and rank; baselines are idle', async () => {
  const runs = (await call('/api/benchmark/live/runs?agent_id=forge')).runs;
  assert.equal(runs.length, 6);
  assert.ok(runs.every((r) => r.agent_id === 'forge' && r.ok && r.provider === 'llama'));
  assert.equal((await call('/api/benchmark/live/runs?model_id=Gemma-2-9B-it-Q6_K&agent_id=forge')).runs.length, 3);
  const one = await call('/api/benchmark/live/runs/' + runs[0].run_id);
  assert.equal(one.ok, true);
  assert.equal(one.run.levels.length, 2);
  assert.ok(one.run.levels.every((l) => l.rows.length === 4 && l.all.pred_tps > 0 && l.all.spread.pred_tps.n === 32));
  assert.equal((await call('/api/benchmark/live/runs/nope')).status, 404);
  assert.equal((await call('/api/benchmark/live/fleet/abc')).status, 404);
  assert.equal((await call('/api/llm/autotune/batch/abc')).status, 404);
  const base = await call('/api/benchmark/live/baselines');
  assert.equal(base.baselines.length, 3);
  assert.ok(base.baselines.every((b) => !b.running && !b.pending && b.last_check.status === 'ok'));
  const hosts = (await call('/api/benchmark/live/hosts?model_id=Gemma-2-9B-it-Q6_K')).hosts;
  assert.deepEqual(hosts.map((h) => h.loaded), [false, false, false]);
});

test('preflight answers for the tuner and the live bench; slots follow the machine load', async () => {
  const pre = await call('/api/llm/autotune/preflight?agent=forge');
  assert.equal(pre.ok, true);
  assert.deepEqual(Object.keys(pre.sizes), ['Qwen2.5-14B-Instruct-Q5_K_M', 'Gemma-2-9B-it-Q6_K']);
  assert.equal(pre.vram_total_mb, 24576);
  const live = await call('/api/benchmark/live/preflight?agent=atlas');
  assert.equal(live.server.up, true);
  assert.match(live.server.url, /192\.0\.2\./);
  assert.ok(Object.keys(live.datasets).length === live.benches.length);
  assert.ok((await call('/api/llm/autotune/preflight?provider=lms')).models.length === 2);
});

test('report card has a current card and weekly history in unix seconds', async () => {
  const latest = await call('/api/reportcard/latest?agent=atlas&provider=llama');
  assert.equal(latest.card.mode, 'standard');
  assert.ok(latest.card.ts < 4e9 && latest.card.result.gen_tps > 100);
  const hist = (await call('/api/reportcard/history?agent=forge&provider=llama&model=x')).cards;
  assert.equal(hist.length, 4);
  assert.ok(hist.every((c, i) => i === 0 || c.ts < hist[i - 1].ts));
  assert.equal((await call('/api/reportcard/latest?agent=atlas&provider=lms')).card, null);
  assert.equal((await call('/api/reportcard/preset')).models.length, 2);
  const recent = await call('/api/reportcard/recent?limit=3');
  assert.equal(recent.ok, true);
  assert.equal(recent.cards.length, 3);
  assert.ok(recent.cards.every((c) => c.agent_id && c.result.model && c.result.gen_tps > 0));
  assert.ok(recent.cards[0].ts >= recent.cards[2].ts);
  assert.deepEqual((await call('/api/reportcard/models?agent=studio&provider=lms')).models.length, 2);
});

test('gateway lists every provider without duplicates', async () => {
  const d = (await call('/api/gateway/v1/models')).data;
  assert.equal(new Set(d.map((m) => m.id)).size, d.length);
  assert.deepEqual([...new Set(d.map((m) => m.provider))].sort(), ['llama', 'lms', 'vllm']);
});

test('a second call 60 s later keeps the same keys', async () => {
  for (const p of ['/api/llm/models', '/api/llm/autotune/status', '/api/benchmark/live/latest', '/api/benchmark/live/preflight',
    '/api/benchmark/live/baselines', '/api/reportcard/latest?agent=atlas&provider=llama', '/api/llm/hf-trending?limit=5']) {
    assert.deepEqual(keysOf(await call(p)), keysOf(await call(p, NOW + 60000)), p);
  }
});

test('trending honours the limit and the quiet streams send keepalives', async () => {
  assert.equal((await call('/api/llm/hf-trending?limit=3')).data.length, 3);
  assert.ok((await call('/api/llm/server/log/tail')).lines.length >= 8);
  for (const [p, key] of [['/api/llm/download/stream', 'type'], ['/api/llm/build/stream', 'type'], ['/api/llm/server/log/stream', 'keepalive']]) {
    const res = await call(p);
    assert.equal(res.headers.get('Content-Type'), 'text/event-stream');
    const reader = res.body.getReader();
    const { value } = await reader.read();
    await reader.cancel();
    assert.ok(key in JSON.parse(new TextDecoder().decode(value).slice(6)), p);
  }
});

test('LM Studio report cards name a model from the LM Studio list; llama models carry their server command line', async () => {
  const list = (await call('/api/reportcard/models?agent=studio&provider=lms')).models;
  for (const q of ['', '&model=' + encodeURIComponent(list[1]), '&model=not-an-lms-model']) {
    const card = (await call('/api/reportcard/latest?agent=studio&provider=lms' + q)).card;
    assert.ok(list.includes(card.result.model), card.result.model);
    for (const c of (await call('/api/reportcard/history?agent=studio&provider=lms' + q)).cards) {
      assert.ok(list.includes(c.result.model));
    }
  }
  assert.equal((await call('/api/reportcard/latest?agent=studio&provider=lms&model=' + encodeURIComponent(list[1]))).card.result.model, list[1]);
  const models = (await call('/api/llm/models?agent=atlas')).data;
  for (const m of models) {
    const a = m.status.args;
    assert.ok(Array.isArray(a) && a.every((x) => typeof x === 'string'));
    assert.equal(a[0], 'llama-server');
    assert.equal(a[a.indexOf('--alias') + 1], m.id);
    assert.ok(a.includes('--model') && a.includes('--ctx-size') && a.includes('--n-gpu-layers'));
  }
});
