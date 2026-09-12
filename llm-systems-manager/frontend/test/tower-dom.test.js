// DOM contract for the Tower drawer (#924): the real index.html header and
// overlay slices booted against the real js/tower.js + js/lib/tower-view.js.
import { describe, test, expect, afterEach } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile } from './helpers/harness.js';

const indexSrc = srcFile('index.html');
const slice = (a, b) => indexSrc.slice(indexSrc.indexOf(a), indexSrc.indexOf(b, indexSrc.indexOf(a)));
const HEADER = slice('<div class="header-right">', '</h1>');
const DRAWER = slice('<div class="tower-overlay" id="towerOverlay"', '<!-- /tower -->');

const _windows = [];
afterEach(() => { while (_windows.length) _windows.pop().close(); });

const ENABLED = { ok: true, enabled: true, admin: false, model: 'qwen3-14b', provider: 'llama', hosts: ['box'] };

function boot(state, opts = {}) {
  const dom = new JSDOM(`<!doctype html><html><body><h1>${HEADER}</h1><div class="grid"></div>${DRAWER}</body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/', pretendToBeVisual: true });
  const w = dom.window;
  _windows.push(w);
  w.localStorage.clear();
  if (opts.stored) w.localStorage.setItem('lsm.tower', JSON.stringify(opts.stored));
  w.__state = state;
  w.__stateFail = false;
  w.__noThread = !!opts.noThread;
  w.__postError = opts.postError || null;
  w.__threads = opts.threads || [];
  w.__calls = [];
  w.fetch = (url, o) => {
    w.__calls.push((o && o.method ? o.method + ' ' : 'GET ') + url);
    if (url === '/api/tower/state') {
      if (w.__stateFail) return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(w.__state) });
    }
    if (url === '/api/tower/threads' && o && o.method === 'POST') {
      if (w.__noThread) return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({ ok: false }) });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, thread: { id: 't1', title: 'New thread' } }) });
    }
    if (url === '/api/tower/threads') return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, threads: w.__threads }) });
    if (/^\/api\/tower\/threads\/[^/]+$/.test(url)) return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, thread: { id: 't9', title: 'Older' }, messages: [] }) });
    if (/\/messages$/.test(url)) {
      w.__posted = JSON.parse(o.body);
      if (w.__postError) { const e = w.__postError; return Promise.resolve({ ok: false, status: e[1], json: () => Promise.resolve({ ok: false, error: e[0] }) }); }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, run_id: 'r1' }) });
    }
    // The manager answers 404, not {ok:false}, for a run it no longer knows.
    if (/\/stop$/.test(url)) { w.__stopped = url; return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ ok: false, error: 'unknown run' }) }); }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
  };
  w._activeTab = 'overall'; w._getDashSubTab = () => 'llama'; w._me = { admin_access: !!state.admin };
  w.SG = { open: (o) => { w.__sse = o; w.__sseClosed = false; return { close() { w.__sseClosed = true; } }; } };
  const inject = code => { const s = w.document.createElement('script'); s.textContent = code; w.document.body.appendChild(s); };
  inject(srcFile('js/lib/tower-view.js'));
  inject(srcFile('js/tower.js'));
  return w;
}
const flush = () => new Promise(r => setTimeout(r, 0));

async function ready(state, opts) {
  const w = boot(state, opts);
  await w.towerRefreshState(); await flush();
  w.towerOpen(); await flush(); await flush();
  return w;
}
async function ask(w, text) {
  const input = w.document.getElementById('twInput');
  input.value = text;
  input.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter' }));
  await flush(); await flush();
}

describe('Tower drawer', () => {
  test('hidden for operators while off; dashed button + enable card for admins', async () => {
    let w = boot({ ok: true, enabled: false, admin: false });
    await w.towerRefreshState(); await flush();
    expect(w.document.getElementById('towerBtn').hidden).toBe(true);
    w = boot({ ok: true, enabled: false, admin: true });
    await w.towerRefreshState(); await flush();
    const btn = w.document.getElementById('towerBtn');
    expect(btn.hidden).toBe(false);
    expect(btn.classList.contains('off')).toBe(true);
    w.towerOpen(); await flush();
    expect(w.document.querySelector('#twBody .on-card h3').textContent).toBe('Turn on Tower');
  });

  test('opens on the button and Alt+T, closes on Esc unless pinned; state persists', async () => {
    const w = boot(ENABLED);
    await w.towerRefreshState(); await flush();
    w.document.getElementById('towerBtn').click(); await flush();
    expect(w.document.getElementById('towerOverlay').classList.contains('open')).toBe(true);
    expect(w.document.getElementById('twModelChip').textContent).toContain('qwen3-14b');
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
    expect(w.document.getElementById('towerOverlay').classList.contains('open')).toBe(false);
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 't', altKey: true }));
    expect(w.document.getElementById('towerOverlay').classList.contains('open')).toBe(true);
    w.document.getElementById('twPin').click();
    expect(w.document.body.classList.contains('tw-docked')).toBe(true);
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
    expect(w.document.getElementById('towerOverlay').classList.contains('open')).toBe(true);
    expect(JSON.parse(w.localStorage.getItem('lsm.tower'))).toMatchObject({ open: true, pinned: true });
  });

  test('closing hands focus back to the header button', async () => {
    const w = await ready(ENABLED);
    w.towerClose();
    expect(w.document.activeElement.id).toBe('towerBtn');
  });

  test('sending posts text + page context, then renders SSE events into the transcript', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    expect(w.__posted).toMatchObject({ text: 'why is box red?', page: { tab: 'overall', sub: 'llama' } });
    expect(w.__sse.url).toBe('/api/tower/runs/r1/stream');
    // LivePause must not swallow a Tower run's frames.
    expect(w.__sse.bypassPause).toBe(true);
    w.__sse.onEvent({ event: 'tool', name: 'host_detail', ok: true, ms: 84, summary: 'read host detail · box · 84 ms', result: {} });
    w.__sse.onEvent({ event: 'delta', text: 'box is hot' });
    w.__sse.onEvent({ event: 'done', ok: true, calls: 1, elapsed_ms: 500 });
    expect(w.document.querySelector('#twBody .tick').textContent).toContain('read host detail · box');
    expect(w.document.querySelector('#twBody .tick .ms').textContent).toBe('84 ms');
    expect(w.document.querySelector('#twBody .ans').textContent).toBe('box is hot');
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(false);
  });

  test('the model chip truncates instead of bleeding past the drawer edge', async () => {
    const w = await ready({ ok: true, enabled: true, provider: 'llama',
                            model: 'Qwen3-Coder-30B-A3B-Instruct-1M-UD-Q4_K_XL',
                            hosts: ['llm-systems-llama-workstation-01.lan'] });
    const chip = w.document.getElementById('twModelChip');
    expect(chip.title).toBe('Qwen3-Coder-30B-A3B-Instruct-1M-UD-Q4_K_XL · llama.cpp · llm-systems-llama-workstation-01.lan');
    // Name and suffix each get their own ellipsis box, so neither can push the row wide.
    expect(chip.querySelector('b').textContent).toBe('Qwen3-Coder-30B-A3B-Instruct-1M-UD-Q4_K_XL');
    expect(chip.querySelector('.sfx').textContent).toBe('· llama.cpp · llm-systems-llama-workstation-01.lan');
    const sub = w.document.querySelector('#towerAside .tw-sub');
    expect(sub.scrollWidth).toBeLessThanOrEqual(sub.clientWidth);
  });

  test('the context chip carries the exclude glyph', async () => {
    const w = await ready(ENABLED);
    const ctx = w.document.getElementById('twCtxChip');
    expect(ctx.querySelector('.x').textContent).toBe('✕');
    expect(ctx.textContent).toContain('overall · llama');
    ctx.click();
    expect(ctx.classList.contains('off')).toBe(true);
    expect(ctx.getAttribute('aria-pressed')).toBe('false');
  });

  test('the streaming caret sits inside the last paragraph', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    w.__sse.onEvent({ event: 'delta', text: 'box is hot' });
    const p = w.document.querySelector('#twBody .ans p');
    expect(p.querySelector('.caret')).not.toBeNull();
    expect(w.document.querySelector('#twBody .ans > .caret')).toBeNull();
    w.__sse.onEvent({ event: 'done', ok: true });
    expect(w.document.querySelector('#twBody .caret')).toBeNull();
  });

  test('a truncated turn says so', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'dump everything');
    w.__sse.onEvent({ event: 'delta', text: 'partial answer' });
    w.__sse.onEvent({ event: 'truncated' });
    w.__sse.onEvent({ event: 'done', ok: true });
    expect(w.document.querySelector('#twBody .drop').textContent).toContain('some output was dropped');
  });

  test('an expanded tick stays expanded across the next repaint', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    w.__sse.onEvent({ event: 'tool', name: 'host_detail', ok: true, ms: 84, summary: 'read host detail', result: { temp: 91 } });
    const tick = w.document.querySelector('#twBody .tick[data-tk]');
    tick.click();
    expect(tick.getAttribute('aria-expanded')).toBe('true');
    w.__sse.onEvent({ event: 'delta', text: 'hot' });
    const after = w.document.querySelector('#twBody .tick[data-tk]');
    expect(after.classList.contains('open')).toBe(true);
    expect(after.getAttribute('aria-expanded')).toBe('true');
  });

  test('stopping a run the manager no longer knows (404) still frees the composer', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    const send = w.document.getElementById('twSend');
    expect(send.classList.contains('stop')).toBe(true);
    send.click(); await flush(); await flush();
    expect(w.__stopped).toBe('/api/tower/runs/r1/stop');
    expect(send.classList.contains('stop')).toBe(false);
    expect(w.__sseClosed).toBe(true);
  });

  test('a second question while a run is streaming is queued, not posted', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'first question');
    const posts = w.__calls.filter(c => /\/messages$/.test(c)).length;
    await ask(w, 'second question');
    expect(w.document.getElementById('twInput').value).toBe('');
    expect(w.__calls.filter(c => /\/messages$/.test(c)).length).toBe(posts);
    expect(w.document.querySelector('#twBody .u.queued').textContent).toContain('second question');
  });

  test('a question typed with no thread is kept and explained', async () => {
    const w = await ready(ENABLED, { noThread: true });
    const input = w.document.getElementById('twInput');
    input.value = 'why is box red?';
    input.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter' })); await flush(); await flush();
    expect(input.value).toBe('why is box red?');
    expect(w.document.querySelector('#twBody .notice h4').textContent).toContain('Tower could not start a thread');
  });

  test.each([
    ['no_model', 503, 'No chat model is loaded right now.'],
    ['run_active', 409, 'Tower is still answering; wait for it to finish.'],
    ['rate_limited', 429, 'Too many questions in a minute; try again shortly.'],
  ])('a %s refusal is explained in the transcript', async (code, status, message) => {
    const w = await ready(ENABLED, { postError: [code, status] });
    await ask(w, 'why is box red?');
    expect(w.document.querySelector('#twBody .notice h4').textContent).toContain(message);
  });

  test('an off then on round-trip brings the composer back without reopening', async () => {
    const w = await ready(ENABLED);
    expect(w.document.getElementById('twInput').disabled).toBe(false);
    w.__state = { ok: true, enabled: false, admin: true };
    await w.towerRefreshState(); await flush();
    expect(w.document.querySelector('#twBody .on-card h3').textContent).toBe('Turn on Tower');
    expect(w.document.getElementById('twInput').disabled).toBe(true);
    w.__state = ENABLED;
    await w.towerRefreshState(); await flush(); await flush();
    expect(w.document.querySelector('#twBody .on-card')).toBeNull();
    expect(w.document.getElementById('twInput').disabled).toBe(false);
  });

  test('a failing state poll leaves the drawer exactly as it was', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    w.__sse.onEvent({ event: 'delta', text: 'box is hot' });
    w.__sse.onEvent({ event: 'done', ok: true });
    const before = w.document.getElementById('twBody').innerHTML;
    w.__stateFail = true;
    await w.towerRefreshState(); await flush();
    expect(w.document.getElementById('towerBtn').hidden).toBe(false);
    expect(w.document.getElementById('towerOverlay').classList.contains('open')).toBe(true);
    expect(w.document.getElementById('twBody').innerHTML).toBe(before);
    expect(w.document.querySelector('#twBody .on-card')).toBeNull();
  });

  test('starting a new thread stops the live run and clears the transcript', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    w.__sse.onEvent({ event: 'delta', text: 'box is' });
    w.document.getElementById('twNew').click(); await flush(); await flush();
    expect(w.__stopped).toBe('/api/tower/runs/r1/stop');
    expect(w.__sseClosed).toBe(true);
    expect(w.document.querySelector('#twBody .ans')).toBeNull();
    expect(w.document.querySelector('#twBody .empty h3').textContent).toBe('Ask about your hosts');
  });

  test('picking an older thread from history stops the live run first', async () => {
    const w = await ready(ENABLED, { threads: [{ id: 't9', title: 'Older' }] });
    await ask(w, 'why is box red?');
    w.__sse.onEvent({ event: 'delta', text: 'box is' });
    w.document.getElementById('twHist').click(); await flush(); await flush();
    w.document.querySelector('#twBody [data-thread="t9"]').click(); await flush(); await flush();
    expect(w.__stopped).toBe('/api/tower/runs/r1/stop');
    expect(w.__sseClosed).toBe(true);
    expect(w.document.querySelector('#twBody .ans')).toBeNull();
  });

  test('a reply that lands while the drawer is closed pulses the button until it is opened', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    w.towerClose();
    w.__sse.onEvent({ event: 'delta', text: 'hot gpu' });
    w.__sse.onEvent({ event: 'done', ok: true, calls: 0 });
    const btn = w.document.getElementById('towerBtn'), badge = w.document.getElementById('towerBadge');
    expect(btn.classList.contains('unread')).toBe(true);
    expect(badge.hidden).toBe(false); expect(badge.textContent).toBe('1');
    w.towerOpen(); await flush();
    expect(btn.classList.contains('unread')).toBe(false);
    expect(badge.hidden).toBe(true);
  });

  test('a reply that lands while the drawer is open does not mark unread', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    w.__sse.onEvent({ event: 'done', ok: true, calls: 0 });
    expect(w.document.getElementById('towerBtn').classList.contains('unread')).toBe(false);
  });

  test('tab switches repaint the context chip and suggestions while open', async () => {
    const w = await ready(ENABLED);
    expect(w.document.getElementById('twCtxChip').textContent).toContain('overall');
    w._activeTab = 'llm';
    w.document.dispatchEvent(new w.CustomEvent('lsm:tab', { detail: { tab: 'llm' } })); await flush(); await flush();
    expect(w.document.getElementById('twCtxChip').textContent).toContain('llm');
    expect(w.document.querySelector('#twBody .empty p').textContent).toContain('llm');
  });

  test('history rows delete on a second click; deleting the current thread forgets it', async () => {
    const w = await ready(ENABLED, { threads: [{ id: 't1', title: 'New thread' }, { id: 't9', title: 'Older' }] });
    await ask(w, 'why is box red?');
    w.document.getElementById('twHist').click(); await flush(); await flush();
    expect(w.document.querySelectorAll('#twBody .hrow').length).toBe(2);
    const del = w.document.querySelector('#twBody [data-del="t1"]');
    del.click(); await flush();
    expect(del.textContent).toBe('Delete');
    expect(w.__calls.some(c => c.startsWith('DELETE'))).toBe(false);
    del.click(); await flush(); await flush();
    expect(w.__calls).toContain('DELETE /api/tower/threads/t1');
    expect(w.document.querySelectorAll('#twBody .hrow').length).toBe(1);
    expect(w.document.querySelector('#twBody .cnt').textContent).toBe('1');
    expect(w.__stopped).toBe('/api/tower/runs/r1/stop');
    expect(JSON.parse(w.localStorage.getItem('lsm.tower')).thread).toBeNull();
  });

  test('a question typed while Tower is busy waits in the queue and goes out after done', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'first');
    expect(w.__posted.text).toBe('first');
    await ask(w, 'second');
    expect(w.__posted.text).toBe('first');
    const q = w.document.querySelector('#twBody .u.queued');
    expect(q.textContent).toContain('second');
    expect(w.document.getElementById('twInput').value).toBe('');
    expect(w.document.getElementById('twInput').placeholder).toContain('next');
    w.__sse.onEvent({ event: 'delta', text: 'one' });
    w.__sse.onEvent({ event: 'done', ok: true, calls: 0 });
    await flush(); await flush(); await flush();
    expect(w.__posted.text).toBe('second');
    expect(w.document.querySelector('#twBody .u.queued')).toBeNull();
    expect([...w.document.querySelectorAll('#twBody .u')].map(u => u.textContent)).toEqual(['first', 'second']);
  });

  test('a queued question can be removed before it is sent', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'first');
    await ask(w, 'second');
    w.document.querySelector('#twBody [data-qx="0"]').click(); await flush();
    expect(w.document.querySelector('#twBody .u.queued')).toBeNull();
    w.__sse.onEvent({ event: 'done', ok: true, calls: 0 });
    await flush(); await flush();
    expect(w.__posted.text).toBe('first');
  });

  test('sending scrolls the transcript to the bottom even when scrolled up', async () => {
    const w = await ready(ENABLED);
    const body = w.document.getElementById('twBody');
    Object.defineProperty(body, 'scrollHeight', { configurable: true, get: () => 2000 });
    Object.defineProperty(body, 'clientHeight', { configurable: true, get: () => 400 });
    body.scrollTop = 100;
    await ask(w, 'first');
    expect(body.scrollTop).toBe(2000);
    body.scrollTop = 100;
    w.__sse.onEvent({ event: 'delta', text: 'x' });
    expect(body.scrollTop).toBe(100);
    await ask(w, 'second');
    expect(body.scrollTop).toBe(2000);
  });

  test('no model: composer disabled and the empty state explains', async () => {
    const w = await ready({ ok: true, enabled: true, model: null });
    expect(w.document.getElementById('twInput').disabled).toBe(true);
    expect(w.document.querySelector('#twBody .empty h3').textContent).toBe('Nothing to think with');
  });
});
