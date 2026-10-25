import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import { load } from './load.mjs';

const RUNTIME = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'runtime');
const SRC = readFileSync(join(RUNTIME, 'demo-starters.js'), 'utf8');
const tower = load(['demo-fleet.js', 'demo-series.js', 'demo-sse.js', 'demo-router.js', 'demo-state.js', 'handlers/alarms.js', 'handlers/tower.js']);

const DASH = '<div id="twBody"><div class="empty"><h3>Ask about your hosts</h3><div class="fu"><button data-sug="x">x</button></div></div></div>'
  + '<textarea id="twInput"></textarea><button id="twSend"></button>';
const PHONE = '<div id="towerBody"><div class="chips sugs"><button class="chip" data-sug="x">x</button></div></div>'
  + '<textarea id="towerInput"></textarea><button id="towerSend"></button>';

async function boot(html) {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`, { url: 'https://demo.example/dashboard', runScripts: 'outside-only' });
  const w = dom.window;
  const tracked = [], sent = [];
  w.DemoTrack = (t, f) => tracked.push([t, f]);
  w.eval(SRC);
  await new Promise((r) => setTimeout(r, 30));
  return { w, tracked, sent };
}
const wait = () => new Promise((r) => setTimeout(r, 150));

test('every starter question maps to a canned answer with the same key', async () => {
  const { w } = await boot('');
  assert.equal(w.DemoStarters.QUESTIONS.length, 10);
  const keys = w.DemoStarters.QUESTIONS.map((q) => q.key);
  for (const k of ['trendtemp', 'trendpower', 'watch', 'slow', 'alarm', 'whatis', 'autotune', 'alarms', 'phone']) assert.ok(keys.includes(k), k);
  for (const q of w.DemoStarters.QUESTIONS) {
    assert.equal(tower.DemoTower.keyFor(q.text), q.key, q.text);
    assert.ok(tower.DemoTower.ANSWERS[q.key], q.key);
  }
});

test('dashboard drawer: panel shows in the empty state and tapping sends the question', async () => {
  const { w, tracked } = await boot(DASH);
  const d = w.document;
  assert.equal(d.querySelectorAll('#twBody .empty .demo-starters button').length, w.DemoStarters.QUESTIONS.length);
  let value = null;
  d.getElementById('twSend').addEventListener('click', () => { value = d.getElementById('twInput').value; });
  d.querySelector('[data-starter="trendpower"]').click();
  assert.equal(value, 'Chart power use over time');
  assert.equal(d.querySelector('.demo-starters'), null);
  assert.equal(JSON.stringify(tracked), JSON.stringify([['click', { target: 'starter-trendpower', href: '' }]]));
});

test('phone Tower: panel shows under the chips and tapping sends', async () => {
  const { w } = await boot(PHONE);
  const d = w.document;
  assert.equal(d.querySelectorAll('#towerBody .demo-starters button').length, w.DemoStarters.QUESTIONS.length);
  let value = null;
  d.getElementById('towerSend').addEventListener('click', () => { value = d.getElementById('towerInput').value; });
  d.querySelector('[data-starter="slow"]').click();
  assert.equal(value, w.DemoStarters.QUESTIONS[0].text);
});

test('panel disappears when messages arrive and returns for a new empty thread', async () => {
  const { w } = await boot(DASH);
  const d = w.document, body = d.getElementById('twBody');
  const empty = body.innerHTML;
  body.innerHTML = '<div class="turn">hello</div>';
  await wait();
  assert.equal(d.querySelector('.demo-starters'), null);
  body.innerHTML = empty;
  await wait();
  assert.equal(d.querySelectorAll('.demo-starters').length, 1);
});

test('no panel when Tower has no model (dashboard empty state without suggestions)', async () => {
  const { w } = await boot('<div id="twBody"><div class="empty"><h3>Nothing to think with</h3><div class="fu"><button>Open</button></div></div></div>');
  assert.equal(w.document.querySelector('.demo-starters'), null);
});
