import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const RUNTIME = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'runtime');
const SRC = readFileSync(join(RUNTIME, 'demo-tour-ask.js'), 'utf8');

const DESK = '<div id="towerOverlay" class="open"><div id="towerAside"><button id="twNew"></button><div id="twBody"></div>'
  + '<textarea id="twInput"></textarea><button id="twSend" class="send"></button></div></div>';
const PHONE = '<button id="towerNew"></button><div id="towerBody"></div><textarea id="towerInput"></textarea>'
  + '<button id="towerSend"></button><button id="towerStop" hidden></button>';

// Fake clock: timers run only when advance() moves time, with microtasks flushed between steps.
function clock() {
  let now = 0, id = 0;
  const timers = new Map();
  return {
    setTimeout(fn, ms) { timers.set(++id, [now + (ms || 0), fn]); return id; },
    clearTimeout(i) { timers.delete(i); },
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        for (let i = 0; i < 20; i++) await Promise.resolve();
        const due = [...timers].filter(([, [t]]) => t <= end).sort((a, b) => a[1][0] - b[1][0])[0];
        if (!due) break;
        timers.delete(due[0]);
        now = Math.max(now, due[1][0]);
        due[1][1]();
      }
      now = end;
      for (let i = 0; i < 20; i++) await Promise.resolve();
    }
  };
}

// Stub Send: on click it records the text, goes busy, then adds an answer after delay ms.
const OLD = '<div class="t"><div class="ans">old</div></div>';
function boot(html, phone, delay = 1000, newDelay = 100, early = false) {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`, { url: 'https://demo.example/dashboard', runScripts: 'outside-only' });
  const w = dom.window, c = clock(), d = w.document;
  w.setTimeout = c.setTimeout; w.clearTimeout = c.clearTimeout;
  const tracked = [], texts = [];
  w.DemoTrack = (t, f) => tracked.push([t, f.target]);
  const input = d.getElementById(phone ? 'towerInput' : 'twInput');
  const send = d.getElementById(phone ? 'towerSend' : 'twSend');
  const body = d.getElementById(phone ? 'towerBody' : 'twBody');
  const busy = (on) => {
    if (phone) d.getElementById('towerStop').hidden = !on;
    else { send.classList.toggle('stop', on); d.getElementById('towerAside').classList.toggle('streaming', on); }
  };
  d.getElementById(phone ? 'towerNew' : 'twNew').addEventListener('click', () => {
    c.setTimeout(() => { body.insertAdjacentHTML('afterbegin', '<div class="empty"></div>'); }, newDelay);
  });
  send.addEventListener('click', () => {
    texts.push(input.value);
    busy(true);
    if (early) body.insertAdjacentHTML('beforeend', '<div class="t"><div class="ans">a</div></div>');
    c.setTimeout(() => { if (!early) body.insertAdjacentHTML('beforeend', '<div class="t"><div class="ans">a</div></div>'); busy(false); }, delay);
  });
  w.eval(SRC);
  return { w, c, d, tracked, texts, busy, body };
}

test('asks both questions in order, waiting for each answer', async () => {
  const { w, c, tracked, texts } = boot(DESK, false);
  const p = w.DemoTourAsk.run('desk', () => true);
  await c.advance(10000);
  assert.equal(await p, 2);
  assert.deepEqual([...texts], [...w.DemoTourAsk.QUESTIONS]);
  assert.deepEqual(tracked.map((t) => t[1]), ['tour-ask-1', 'tour-ask-2']);
  assert.equal(w.DemoTourAsking, false);
});

test('a busy drawer delays the next tour question', async () => {
  const { w, c, d, texts, busy, body } = boot(DESK, false);
  body.insertAdjacentHTML('beforeend', '<div class="t"><div class="ans">visitor</div></div>');
  busy(true);
  const p = w.DemoTourAsk.run('desk', () => true);
  await c.advance(2000);
  assert.deepEqual(texts, []);
  busy(false);
  await c.advance(10000);
  assert.equal(await p, 2);
  assert.equal(texts[0], w.DemoTourAsk.QUESTIONS[0]);
  assert.equal(d.querySelector('#twBody .ans').textContent, 'visitor');
});

test('leaving the step stops the next question', async () => {
  const { w, c, texts, body } = boot(DESK, false);
  body.insertAdjacentHTML('beforeend', OLD);
  let live = true;
  const p = w.DemoTourAsk.run('desk', () => live);
  await c.advance(1000);
  live = false;
  await c.advance(10000);
  assert.equal(await p, 1);
  assert.equal(texts.length, 1);
});

test('phone screen uses the phone controls', async () => {
  const { w, c, tracked, texts } = boot(PHONE, true);
  const p = w.DemoTourAsk.run('phone', () => true);
  await c.advance(10000);
  assert.equal(await p, 2);
  assert.deepEqual([...texts], [...w.DemoTourAsk.QUESTIONS]);
  assert.deepEqual(tracked.map((t) => t[1]), ['tour-ask-1', 'tour-ask-2']);
});

test('the phone waits for Tower to be ready before asking', async () => {
  const { w, c, d, texts } = boot(PHONE, true);
  const input = d.getElementById('towerInput');
  input.disabled = true;
  c.setTimeout(() => { input.disabled = false; }, 800);
  const p = w.DemoTourAsk.run('phone', () => true);
  await c.advance(10000);
  assert.equal(await p, 2);
  assert.deepEqual([...texts], [...w.DemoTourAsk.QUESTIONS]);
});

test('a drawer that never opens sends nothing', async () => {
  const { w, c, d, texts } = boot(DESK, false);
  d.getElementById('towerOverlay').classList.remove('open');
  const p = w.DemoTourAsk.run('desk', () => true);
  await c.advance(3500);
  assert.equal(await p, 0);
  assert.deepEqual(texts, []);
});

test('an answer that never finishes is not tracked and stops the run', async () => {
  const { w, c, tracked, texts } = boot(DESK, false, 60000);
  const p = w.DemoTourAsk.run('desk', () => true);
  await c.advance(20000);
  assert.equal(await p, 1);
  assert.deepEqual(tracked, []);
  assert.equal(texts.length, 1);
  assert.equal(w.DemoTourAsking, false);
});

test('a slow new thread delays the first question until the drawer repaints', async () => {
  const { w, c, texts, body } = boot(DESK, false, 1000, 2000);
  body.insertAdjacentHTML('beforeend', OLD);
  const p = w.DemoTourAsk.run('desk', () => true);
  await c.advance(1500);
  assert.equal(texts.length, 0);
  await c.advance(1000);
  assert.equal(texts.length, 1);
  await c.advance(10000);
  assert.equal(await p, 2);
});

test('a drawer that never repaints after New sends nothing', async () => {
  const { w, c, texts, body } = boot(DESK, false, 1000, 9000);
  body.insertAdjacentHTML('beforeend', OLD);
  const p = w.DemoTourAsk.run('desk', () => true);
  await c.advance(5000);
  assert.equal(await p, 0);
  assert.deepEqual(texts, []);
});

test('a step change during a wait sends nothing more', async () => {
  const { w, c, texts, busy } = boot(DESK, false);
  busy(true);
  let live = true;
  const p = w.DemoTourAsk.run('desk', () => live);
  await c.advance(1000);
  live = false;
  busy(false);
  await c.advance(10000);
  assert.equal(await p, 0);
  assert.deepEqual(texts, []);
});

test('an answer row that appears before streaming ends does not end the wait early', async () => {
  const { w, c, texts, tracked } = boot(DESK, false, 3000, 100, true);
  const p = w.DemoTourAsk.run('desk', () => true);
  await c.advance(2000);
  assert.equal(texts.length, 1);
  assert.deepEqual(tracked, []);
  await c.advance(20000);
  assert.equal(await p, 2);
});

for (const phone of [false, true]) {
  test(`an empty conversation is reused without New (${phone ? 'phone' : 'desk'})`, async () => {
    const { w, c, d, texts } = boot(phone ? PHONE : DESK, phone);
    d.getElementById(phone ? 'towerNew' : 'twNew').replaceWith(d.createElement('span'));
    const p = w.DemoTourAsk.run(phone ? 'phone' : 'desk', () => true);
    await c.advance(10000);
    assert.equal(await p, 2);
    assert.deepEqual([...texts], [...w.DemoTourAsk.QUESTIONS]);
  });
}

test('earlier turns still start a new thread and wait for the repaint', async () => {
  const { w, c, texts, body } = boot(DESK, false, 1000, 2000);
  body.insertAdjacentHTML('beforeend', '<div class="t"><div class="ans">old</div></div>');
  const p = w.DemoTourAsk.run('desk', () => true);
  await c.advance(1500);
  assert.equal(texts.length, 0);
  await c.advance(1000);
  assert.equal(texts.length, 1);
  await c.advance(10000);
  assert.equal(await p, 2);
});
