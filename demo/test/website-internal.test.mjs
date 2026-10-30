import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import { internal } from '../pages/_worker.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const HEX = 'a'.repeat(64);
const scriptOf = (html) => /<script>([\s\S]*?)<\/script>/.exec(html)[1];

// Runs an inline script against a jsdom storage and a stubbed location/history.
function run(code, hash, { throwing = false } = {}) {
  const dom = new JSDOM('<!doctype html><p id="msg"></p>', { url: 'https://example.test/internal.html' });
  const w = dom.window, calls = { replace: [], state: [] };
  const location = { hash, pathname: '/internal.html', replace: (u) => calls.replace.push(u) };
  const history = { replaceState: (...a) => calls.state.push(a) };
  const localStorage = throwing ? { setItem() { throw new Error('x'); }, removeItem() { throw new Error('x'); } } : w.localStorage;
  new Function('location', 'history', 'localStorage', 'document', code)(location, history, localStorage, w.document);
  return { w, calls, msg: w.document.getElementById('msg').textContent };
}

const demoCode = async () => scriptOf(await internal(new URL('https://demo.test/internal')).text());
const siteCode = () => scriptOf(readFileSync(resolve(ROOT, 'website', 'internal.html'), 'utf8'));

test('demo page: valid tag is stored and forwarded', async () => {
  const r = run(await demoCode(), '#t=' + HEX);
  assert.equal(r.w.localStorage.getItem('lsmTag'), HEX);
  assert.deepEqual(r.calls.replace, ['https://www.llmsyscore.com/internal.html#t=' + HEX]);
});

test('demo page: #r=1 removes the tag', async () => {
  const code = await demoCode();
  const dom = new JSDOM('', { url: 'https://example.test/' });
  dom.window.localStorage.setItem('lsmTag', HEX);
  const calls = [];
  new Function('location', 'localStorage', code)({ hash: '#r=1', replace: (u) => calls.push(u) }, dom.window.localStorage);
  assert.equal(dom.window.localStorage.getItem('lsmTag'), null);
  assert.deepEqual(calls, ['https://www.llmsyscore.com/internal.html#r=1']);
});

test('demo page: uppercase or 65-char hex is rejected', async () => {
  const code = await demoCode();
  for (const h of ['#t=' + 'A'.repeat(64), '#t=' + 'a'.repeat(65)]) {
    const r = run(code, h);
    assert.equal(r.w.localStorage.getItem('lsmTag'), null);
    assert.deepEqual(r.calls.replace, ['https://www.llmsyscore.com/internal.html']);
  }
});

test('website page: valid tag is stored, marked line shown, fragment cleared', () => {
  const r = run(siteCode(), '#t=' + HEX);
  assert.equal(r.w.localStorage.getItem('lsmTag'), HEX);
  assert.match(r.msg, /is marked as yours/);
  assert.deepEqual(r.calls.state, [[null, '', '/internal.html']]);
});

test('website page: #r=1 removes the tag', () => {
  const code = siteCode();
  const dom = new JSDOM('<!doctype html><p id="msg"></p>', { url: 'https://example.test/' });
  dom.window.localStorage.setItem('lsmTag', HEX);
  new Function('location', 'history', 'localStorage', 'document', code)(
    { hash: '#r=1', pathname: '/internal.html' }, { replaceState() {} }, dom.window.localStorage, dom.window.document);
  assert.equal(dom.window.localStorage.getItem('lsmTag'), null);
  assert.match(dom.window.document.getElementById('msg').textContent, /no longer marked/);
});

test('website page: bad hash, no hash or throwing storage shows the fallback', () => {
  for (const [h, opt] of [['#t=' + 'A'.repeat(64), {}], ['#t=' + 'a'.repeat(65), {}], ['', {}], ['#t=' + HEX, { throwing: true }]]) {
    const r = run(siteCode(), h, opt);
    assert.equal(r.msg, 'This browser could not be marked.');
    if (!opt.throwing) assert.equal(r.w.localStorage.getItem('lsmTag'), null);
  }
});
