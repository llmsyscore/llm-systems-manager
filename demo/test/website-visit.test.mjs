import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const SRC = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'website', 'js', 'visit.js'), 'utf8');

const BODY = `
<a id="inst" href="https://get.llmsyscore.com/">i</a>
<button id="copy" data-copy="#x">c</button>
<a id="demo" href="https://demo.llmsyscore.com/">d</a>
<a id="gh" href="https://github.com/llmsyscore/x">g</a>
<a id="docs" href="/docs/api.html">docs</a>
<a id="docs2" href="docs.html">docs</a>
<button id="shot" data-full="a.webp">s</button>
<a id="contact" href="contact.html">c</a>
<a id="mail" href="mailto:a@b.c">m</a>
<div class="term-tabs"><button id="tab">t</button></div>
<a id="page" href="/about.html">p</a>
<a id="ext" href="https://example.org/">e</a>
<span id="plain">x</span>
<div class="tour-video"><video id="vid"></video></div>`;

// Loads visit.js into a jsdom page and returns the page plus the recorded beacons.
function open({ url = 'https://llmsyscore.com/', referrer, store = {}, clock } = {}) {
  const dom = new JSDOM(`<!doctype html><body>${BODY}</body>`, { url, referrer, runScripts: 'outside-only' });
  const w = dom.window;
  Object.entries(store).forEach(([k, v]) => w.sessionStorage.setItem(k, v));
  const sent = [];
  w.navigator.sendBeacon = (u, b) => { sent.push(JSON.parse(b)); return true; };
  if (clock) w.Date.now = () => clock.now;
  let state = 'visible';
  Object.defineProperty(w.document, 'visibilityState', { get: () => state });
  w.eval(SRC);
  w.document.addEventListener('click', (e) => e.preventDefault());
  const hide = () => { state = 'hidden'; w.document.dispatchEvent(new w.Event('visibilitychange')); };
  const show = () => { state = 'visible'; w.document.dispatchEvent(new w.Event('visibilitychange')); };
  const click = (id) => w.document.getElementById(id).dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  return { w, sent, hide, show, click };
}

test('first page carries referrer and campaign, n=1', () => {
  const p = open({ url: 'https://llmsyscore.com/?ref=hn&utm_source=s&utm_medium=m&utm_campaign=c', referrer: 'https://news.example/x' });
  p.hide();
  assert.equal(p.sent.length, 1);
  const b = p.sent[0];
  assert.equal(b.v, 1);
  assert.equal(b.n, 1);
  assert.match(b.visit, /^[0-9a-f]{16}$/);
  assert.match(b.page, /^[0-9a-f]{16}$/);
  assert.equal(b.path, '/');
  assert.equal(b.referrer, 'https://news.example/x');
  assert.equal(b.ref, 'hn');
  assert.equal(b.utm_source, 's');
  assert.equal(b.utm_medium, 'm');
  assert.equal(b.utm_campaign, 'c');
});

test('second page in the same tab has n=2, same visit, no referrer', () => {
  const a = open({ referrer: 'https://news.example/x' });
  a.hide();
  const store = { lsmSiteVisit: a.w.sessionStorage.getItem('lsmSiteVisit'), lsmSitePages: a.w.sessionStorage.getItem('lsmSitePages') };
  const b = open({ url: 'https://llmsyscore.com/about.html?ref=hn', referrer: 'https://llmsyscore.com/', store });
  b.hide();
  assert.equal(b.sent[0].n, 2);
  assert.equal(b.sent[0].visit, a.sent[0].visit);
  assert.notEqual(b.sent[0].page, a.sent[0].page);
  assert.equal(b.sent[0].referrer, undefined);
  assert.equal(b.sent[0].ref, undefined);
});

test('clicks are classified by target', () => {
  const p = open();
  ['inst', 'copy', 'demo', 'gh', 'docs', 'docs2', 'shot', 'contact', 'mail', 'tab', 'page', 'ext', 'plain'].forEach(p.click);
  p.hide();
  assert.deepEqual(p.sent[0].clicks, [
    { t: 'install', h: 'get.llmsyscore.com' }, { t: 'install', h: '' },
    { t: 'demo', h: 'demo.llmsyscore.com' }, { t: 'github', h: 'github.com' },
    { t: 'docs', h: '' }, { t: 'docs', h: '' }, { t: 'screenshot', h: '' },
    { t: 'contact', h: '' }, { t: 'contact', h: '' }, { t: 'tab', h: '' },
    { t: 'page', h: '' }, { t: 'external', h: 'example.org' },
  ]);
});

test('tour video play is a click and reports watched time', () => {
  const p = open();
  const v = p.w.document.getElementById('vid');
  Object.defineProperty(v, 'duration', { value: 100 });
  v.currentTime = 0;
  v.dispatchEvent(new p.w.Event('play'));
  v.currentTime = 1;
  v.dispatchEvent(new p.w.Event('timeupdate'));
  v.currentTime = 40;
  v.dispatchEvent(new p.w.Event('pause'));
  p.hide();
  assert.deepEqual(p.sent[0].clicks, [{ t: 'video', h: '' }]);
  assert.deepEqual(p.sent[0].video, { ms: 1000, pct: 40 });
});

test('clicks are capped at 30', () => {
  const p = open();
  for (let i = 0; i < 40; i++) p.click('gh');
  p.hide();
  assert.equal(p.sent[0].clicks.length, 30);
});

test('scroll keeps the maximum depth', () => {
  const p = open();
  const d = p.w.document.documentElement;
  Object.defineProperty(d, 'scrollHeight', { value: 2000, configurable: true });
  Object.defineProperty(p.w, 'innerHeight', { value: 500, configurable: true });
  p.w.scrollY = 1000;
  p.w.dispatchEvent(new p.w.Event('scroll'));
  p.w.scrollY = 0;
  p.w.dispatchEvent(new p.w.Event('scroll'));
  p.hide();
  assert.equal(p.sent[0].scroll, 75);
});

test('page that fits the window reports 100', () => {
  const p = open();
  p.hide();
  assert.equal(p.sent[0].scroll, 100);
});

test('no resend when nothing changed, resend after a new click', () => {
  const p = open();
  p.hide();
  p.w.dispatchEvent(new p.w.Event('pagehide'));
  p.hide();
  assert.equal(p.sent.length, 1);
  p.click('gh');
  p.hide();
  assert.equal(p.sent.length, 2);
  assert.equal(p.sent[1].page, p.sent[0].page);
  assert.equal(p.sent[1].clicks.length, 1);
});

test('visible time counts only while shown and resends only after 5 s more of it', () => {
  const clock = { now: 1000 };
  const p = open({ clock });
  clock.now += 3000;
  p.hide();
  clock.now += 60000;
  p.show();
  clock.now += 2000;
  p.hide();
  assert.equal(p.sent.length, 1, 'under 5 s more visible time, no resend');
  assert.equal(p.sent[0].ms, 3000);
  p.show();
  clock.now += 4000;
  p.hide();
  assert.equal(p.sent.length, 2);
  assert.equal(p.sent[1].ms, 9000, 'hidden minute not counted');
});

test('nothing is written to localStorage or cookies', () => {
  const p = open();
  p.click('gh');
  p.hide();
  assert.equal(p.w.localStorage.length, 0);
  assert.equal(p.w.document.cookie, '');
  assert.deepEqual(Object.keys(p.w.sessionStorage).sort(), ['lsmSitePages', 'lsmSiteVisit']);
});
