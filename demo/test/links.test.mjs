import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const RUNTIME = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'runtime');
const SRC = readFileSync(join(RUNTIME, 'demo-links.js'), 'utf8');

// Runs demo-links.js in a jsdom window and records window.open calls.
async function boot(html) {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`, { url: 'https://demo.example/dashboard', runScripts: 'outside-only' });
  const w = dom.window;
  const opened = [];
  w.open = (...a) => { opened.push(a); return null; };
  w.eval(SRC);
  await new Promise((r) => setTimeout(r, 30));
  return { w, opened };
}
const click = (w, el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));

test('tag adds ref, keeps query and hash, leaves existing ref alone', async () => {
  const { w } = await boot('');
  const t = w.DemoLinks.tag;
  assert.equal(t('https://example.org/'), 'https://example.org/?ref=demo');
  assert.equal(t('https://example.org/a?x=1#top'), 'https://example.org/a?x=1&ref=demo#top');
  assert.equal(t('https://example.org/?ref=hn'), 'https://example.org/?ref=hn');
  assert.equal(t('https://get.llmsyscore.com/?ref=demo'), 'https://get.llmsyscore.com/?ref=demo');
});

test('tag ignores same-origin, mailto, tel and javascript', async () => {
  const { w } = await boot('');
  const t = w.DemoLinks.tag;
  for (const u of ['https://demo.example/x', '/local', 'mailto:a@b.co', 'tel:123', 'javascript:void(0)']) assert.equal(t(u), u);
});

test('clicking an external link sets target, rel and ref', async () => {
  const { w } = await boot('<a id="a" href="https://example.org/p" rel="author"><span id="s">x</span></a>');
  click(w, w.document.getElementById('s'));
  const a = w.document.getElementById('a');
  assert.equal(a.getAttribute('target'), '_blank');
  assert.match(a.getAttribute('rel'), /noopener/);
  assert.match(a.getAttribute('rel'), /author/);
  assert.equal(a.getAttribute('href'), 'https://example.org/p?ref=demo');
});

test('install link keeps a single ref=demo', async () => {
  const { w } = await boot('<a id="a" href="https://get.llmsyscore.com/?ref=demo">i</a>');
  const a = w.document.getElementById('a');
  click(w, a);
  assert.equal(a.getAttribute('href'), 'https://get.llmsyscore.com/?ref=demo');
});

test('same-origin, mailto and download links are untouched', async () => {
  const { w } = await boot('<a id="a" href="/x">a</a><a id="b" href="mailto:a@b.co">b</a><a id="c" href="https://example.org/f.zip" download>c</a>');
  for (const id of ['a', 'b', 'c']) click(w, w.document.getElementById(id));
  for (const id of ['a', 'b', 'c']) assert.equal(w.document.getElementById(id).hasAttribute('target'), false);
  assert.equal(w.document.getElementById('c').getAttribute('href'), 'https://example.org/f.zip');
});

test('hrefs are rewritten on load and for links added later', async () => {
  const { w } = await boot('<a id="a" href="https://example.org/">a</a>');
  assert.equal(w.document.getElementById('a').getAttribute('href'), 'https://example.org/?ref=demo');
  const b = w.document.createElement('a');
  b.id = 'b'; b.setAttribute('href', 'https://example.net/');
  w.document.body.appendChild(b);
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(b.getAttribute('href'), 'https://example.net/?ref=demo');
});

test('window.open to another site is tagged and opens a new tab', async () => {
  const { w, opened } = await boot('');
  w.open('https://example.org/x', '_self');
  w.open('/local', '_self');
  assert.deepEqual(opened[0].slice(0, 2), ['https://example.org/x?ref=demo', '_blank']);
  assert.deepEqual(opened[1].slice(0, 2), ['/local', '_self']);
});
