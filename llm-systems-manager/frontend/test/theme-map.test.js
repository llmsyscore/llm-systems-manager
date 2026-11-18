// #1432: a saved legacy theme name maps to Dark or Light and is written back once on load.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const foundationSrc = readFileSync(join(here, '..', 'js', 'foundation.js'), 'utf8');
const drawerSrc = readFileSync(join(here, '..', 'js', 'lib', 'settingsdrawer.js'), 'utf8');

// Loads foundation.js with GET /api/layout answering `saved`; POST bodies land in `posts`.
function harness(saved) {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const w = dom.window;
  const posts = [];
  w.fetch = async (url, opts) => {
    if (opts && opts.method === 'POST') { if (url === '/api/layout') posts.push(JSON.parse(opts.body)); return { ok: true, json: async () => ({}) }; }
    if (url === '/api/layout') return { ok: true, json: async () => JSON.parse(JSON.stringify(saved)) };
    return { ok: true, json: async () => ({}) };
  };
  const inject = (code) => { const s = w.document.createElement('script'); s.textContent = code; w.document.head.appendChild(s); };
  inject(drawerSrc);
  inject(foundationSrc);
  return { w, posts };
}

// Waits up to `max` macrotask ticks for `done()` to hold.
async function waitFor(done, max = 50) {
  for (let i = 0; i < max && !done(); i++) await new Promise(r => setTimeout(r, 0));
}

describe('theme map', () => {
  test('loadLayout saves a mapped theme once', async () => {
    const legacy = harness({ theme: 'graphite' });
    await legacy.w.loadLayout();
    await waitFor(() => legacy.posts.length >= 1);
    expect(legacy.posts.length).toBe(1);
    expect(legacy.posts[0].theme).toBe('dark');
    expect(legacy.w.document.documentElement.getAttribute('data-theme')).toBe('dark');

    const current = harness({ theme: 'dark' });
    await current.w.loadLayout();
    await waitFor(() => false, 5);
    expect(current.posts.length).toBe(0);
  });
});
