import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import { settings } from '../pages/_worker.js';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const HEX = 'a'.repeat(64);
const scriptOf = (html) => /<script\b[^>]*>([\s\S]*?)<\/script[^>]*>/i.exec(html)[1];

// Runs an inline script against a jsdom storage and a stubbed location/history.
function run(code, hash, { throwing = false } = {}) {
  const dom = new JSDOM('<!doctype html><p id="msg"></p>', { url: 'https://example.test/s.html' });
  const w = dom.window, calls = { replace: [], state: [] };
  const location = { hash, pathname: '/s.html', replace: (u) => calls.replace.push(u) };
  const history = { replaceState: (...a) => calls.state.push(a) };
  const localStorage = throwing ? { setItem() { throw new Error('x'); }, removeItem() { throw new Error('x'); } } : w.localStorage;
  new Function('location', 'history', 'localStorage', 'document', code)(location, history, localStorage, w.document);
  return { w, calls, msg: w.document.getElementById('msg').textContent };
}

const demoCode = async () => scriptOf(await settings().text());
const siteCode = () => scriptOf(readFileSync(resolve(ROOT, 'website', 's.html'), 'utf8'));

test('demo page: valid value is stored and forwarded', async () => {
  const r = run(await demoCode(), '#v=' + HEX);
  assert.equal(r.w.localStorage.getItem('lsmPref'), HEX);
  assert.deepEqual(r.calls.replace, ['https://www.llmsyscore.com/s.html#v=' + HEX]);
});

test('demo page: #v=0 removes the value', async () => {
  const code = await demoCode();
  const dom = new JSDOM('', { url: 'https://example.test/' });
  dom.window.localStorage.setItem('lsmPref', HEX);
  const calls = [];
  new Function('location', 'localStorage', code)({ hash: '#v=0', replace: (u) => calls.push(u) }, dom.window.localStorage);
  assert.equal(dom.window.localStorage.getItem('lsmPref'), null);
  assert.deepEqual(calls, ['https://www.llmsyscore.com/s.html#v=0']);
});

test('demo page: uppercase or 65-char hex is rejected', async () => {
  const code = await demoCode();
  for (const h of ['#v=' + 'A'.repeat(64), '#v=' + 'a'.repeat(65)]) {
    const r = run(code, h);
    assert.equal(r.w.localStorage.getItem('lsmPref'), null);
    assert.deepEqual(r.calls.replace, ['https://www.llmsyscore.com/s.html']);
  }
});

test('website page: valid value is stored, saved line shown, fragment cleared', () => {
  const r = run(siteCode(), '#v=' + HEX);
  assert.equal(r.w.localStorage.getItem('lsmPref'), HEX);
  assert.equal(r.msg, 'Saved.');
  assert.deepEqual(r.calls.state, [[null, '', '/s.html']]);
});

test('website page: #v=0 removes the value', () => {
  const code = siteCode();
  const dom = new JSDOM('<!doctype html><p id="msg"></p>', { url: 'https://example.test/' });
  dom.window.localStorage.setItem('lsmPref', HEX);
  new Function('location', 'history', 'localStorage', 'document', code)(
    { hash: '#v=0', pathname: '/s.html' }, { replaceState() {} }, dom.window.localStorage, dom.window.document);
  assert.equal(dom.window.localStorage.getItem('lsmPref'), null);
  assert.match(dom.window.document.getElementById('msg').textContent, /^Cleared\.$/);
});

test('website page: bad hash, no hash or throwing storage shows the fallback', () => {
  for (const [h, opt] of [['#v=' + 'A'.repeat(64), {}], ['#v=' + 'a'.repeat(65), {}], ['', {}], ['#v=' + HEX, { throwing: true }]]) {
    const r = run(siteCode(), h, opt);
    assert.equal(r.msg, 'Not saved.');
    if (!opt.throwing) assert.equal(r.w.localStorage.getItem('lsmPref'), null);
  }
});
