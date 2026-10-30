import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const SRC = readFileSync(join(resolve(dirname(fileURLToPath(import.meta.url)), '..', 'runtime'), 'demo-install.js'), 'utf8');
const CMD = 'bash <(curl -fsSL https://get.llmsysmanager.com/install.sh)';
const SHARE = 'https://get.llmsysmanager.com/?ref=demo-share';

// Loads demo-install.js into a jsdom window, with optional navigator stubs.
function boot(nav = {}) {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only' });
  const w = dom.window;
  for (const [k, v] of Object.entries(nav)) Object.defineProperty(w.navigator, k, { value: v, configurable: true });
  w.eval(SRC);
  return w;
}

test('panel shows the command, copy, share, GitHub and directions', () => {
  const w = boot();
  const p = w.DemoInstall.panel('tour');
  assert.equal(p.querySelector('code.demo-install-cmd').textContent, CMD);
  const copyB = p.querySelector('button.demo-install-copy');
  const shareB = p.querySelector('button.demo-install-share');
  const star = p.querySelector('a.demo-install-star');
  const dir = p.querySelector('a.demo-install-directions');
  assert.deepEqual([copyB.textContent, shareB.textContent, star.textContent, dir.textContent],
    ['Copy', 'Share', 'Star on GitHub', 'Installation directions']);
  assert.equal(star.href, 'https://github.com/llmsyscore/llm-systems-manager');
  assert.equal(dir.href, 'https://get.llmsysmanager.com/?ref=demo');
  for (const a of [star, dir]) { assert.equal(a.target, '_blank'); assert.equal(a.rel, 'noopener'); }
  assert.deepEqual([copyB, shareB, star, dir].map((n) => n.getAttribute('data-demo-track')),
    ['copy-tour', 'share-tour', 'star-tour', 'directions-tour']);
  assert.equal(p.querySelector('p.demo-install-note').getAttribute('aria-live'), 'polite');
});

test('copy uses the clipboard and says Copied', async () => {
  const got = [];
  const w = boot({ clipboard: { writeText: (t) => { got.push(t); return Promise.resolve(); } } });
  const p = w.DemoInstall.panel('banner');
  w.document.body.appendChild(p);
  p.querySelector('.demo-install-copy').click();
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(got, [CMD]);
  assert.equal(p.querySelector('.demo-install-note').textContent, 'Copied');
});

test('copy falls back to selecting the command', async () => {
  const w = boot();
  const p = w.DemoInstall.panel('banner');
  w.document.body.appendChild(p);
  p.querySelector('.demo-install-copy').click();
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(w.getSelection().toString(), CMD);
  assert.equal(p.querySelector('.demo-install-note').textContent, 'Press Ctrl+C / ⌘C to copy');
});

test('share uses the system sheet', async () => {
  const got = [];
  const w = boot({ share: (d) => { got.push(d); return Promise.resolve(); } });
  await w.DemoInstall.share(null);
  assert.deepEqual(JSON.parse(JSON.stringify(got)), [{ title: 'LLM Systems Manager', text: 'Install LLM Systems Manager on your own machines', url: SHARE }]);
});

test('share falls back to copying the link', async () => {
  const got = [];
  const w = boot({ clipboard: { writeText: (t) => { got.push(t); return Promise.resolve(); } } });
  const note = w.document.createElement('p');
  await w.DemoInstall.share(note);
  assert.deepEqual(got, [SHARE]);
  assert.equal(note.textContent, 'Link copied');
});

test('share with no sheet and no clipboard shows the link', async () => {
  const w = boot();
  const note = w.document.createElement('p');
  await w.DemoInstall.share(note);
  assert.equal(note.textContent, SHARE);
});

test('cancelled share is quiet', async () => {
  const w = boot({ share: () => Promise.reject(Object.assign(new Error('x'), { name: 'AbortError' })) });
  const note = w.document.createElement('p');
  await w.DemoInstall.share(note);
  assert.equal(note.textContent, '');
});
