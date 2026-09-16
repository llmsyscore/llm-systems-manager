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
    const act = url.match(/^\/api\/tower\/actions\/([^/]+)\/(approve|deny|answer)$/);
    if (act) {
      if (w.__actNetFail) return Promise.reject(new Error('offline'));
      if (w.__actFail) {
        const err = { 403: 'not allowed', 409: 'not pending', 410: 'expired' }[w.__actFail] || 'failed';
        return Promise.resolve({ ok: false, status: w.__actFail, json: () => Promise.resolve({ ok: false, error: err }) });
      }
      w.__decideBody = o && o.body ? JSON.parse(o.body) : null;
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, run_id: 'r1', status: { approve: 'approved', deny: 'denied', answer: 'answered' }[act[2]], tool: act[2] === 'answer' ? 'ask_operator' : 'wake_server' }) });
    }
    if (url === '/api/tower/insights') return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, insights: w.__insights || [], new: (w.__insights || []).filter(r => !r.seen_at && (r.status === 'new' || r.status === 'applied')).length }) });
    if (url === '/api/tower/insights/seen') { w.__seen = (w.__seen || 0) + 1; return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, seen: 1 }) }); }
    if (url === '/api/tower/insights/dismiss_all') { w.__dismissedAll = true; return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, dismissed: 1 }) }); }
    const ins = url.match(/^\/api\/tower\/insights\/([^/]+)\/(apply|dismiss)$/);
    if (ins) {
      w.__insCalls = (w.__insCalls || []).concat([ins[2] + ' ' + ins[1]]);
      if (ins[2] === 'apply' && w.__applyFail) return Promise.resolve({ ok: false, status: w.__applyFail, json: () => Promise.resolve({ ok: false, error: { 403: 'not allowed', 409: 'stale' }[w.__applyFail] || 'failed' }) });
      if (ins[2] === 'apply') { w.__insights = (w.__insights || []).map(r => r.id === ins[1] ? { ...r, status: 'applied', applied_by: 'tower via alice', resolved: Date.now() / 1000 } : r); }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, status: 'applied', result: { ok: true, message: 'done', steps: [] } }) });
    }
    if (url === '/api/tower/state') {
      if (w.__stateFail) return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(w.__state) });
    }
    if (url === '/api/tower/threads' && o && o.method === 'POST') {
      w.__threadBody = o.body ? JSON.parse(o.body) : null;
      if (w.__noThread) return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({ ok: false }) });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, thread: { id: 't1', title: (w.__threadBody && w.__threadBody.title) || 'New thread' } }) });
    }
    if (url === '/api/tower/threads') return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, threads: w.__threads, discord: w.__discord || [] }) });
    if (/^\/api\/tower\/threads\/[^/]+$/.test(url) && o && o.method === 'PATCH') {
      w.__renamed = JSON.parse(o.body).title;
      if (w.__renameFail) return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ ok: false, error: 'unknown thread' }) });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, thread: { id: url.split('/').pop(), title: w.__renamed } }) });
    }
    if (/^\/api\/tower\/threads\/[^/]+$/.test(url)) return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, thread: { id: 't9', title: 'Older' }, messages: w.__rows || [], active_run: w.__activeRun || null }) });
    if (/\/messages$/.test(url)) {
      w.__posted = JSON.parse(o.body);
      if (w.__postError) { const e = w.__postError; return Promise.resolve({ ok: false, status: e[1], json: () => Promise.resolve({ ok: false, error: e[0] }) }); }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, run_id: 'r1' }) });
    }
    // The manager answers 404, not {ok:false}, for a run it no longer knows.
    if (/\/park$/.test(url)) { w.__parked = url; return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) }); }
    if (/\/stop$/.test(url)) {
      w.__stopped = url;
      if (w.__stopOk) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
      return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ ok: false, error: 'unknown run' }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
  };
  w._activeTab = 'overall'; w._subTabState = { dashboard: 'energy', llm: 'llama' }; w._me = { admin_access: !!state.admin };
  w.__toasts = []; w.showToast = (...a) => w.__toasts.push(a);
  w.switchTab = t => { w.__tab = t; };
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
async function bootAndAsk(state, text) {
  const w = await ready(state);
  await ask(w, text);
  return w;
}
async function bootWithThread(state, rows) {
  const w = boot(state, { stored: { thread: 't1' } });
  w.__rows = rows;
  await w.towerRefreshState(); await flush();
  w.towerOpen(); await flush(); await flush();
  return w;
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

  test('overlay mode locks the page scroll, pinning insets the drawer instead, closing clears both (#988)', async () => {
    const w = await ready(ENABLED);
    const body = w.document.body, root = w.document.documentElement;
    expect(body.classList.contains('tw-lock')).toBe(true);
    expect(body.classList.contains('tw-docked')).toBe(false);
    expect(root.style.getPropertyValue('--tw-sbw')).toMatch(/^\d+px$/);
    w.document.getElementById('twPin').click(); await flush();
    expect(body.classList.contains('tw-docked')).toBe(true);
    expect(body.classList.contains('tw-lock')).toBe(false);
    w.document.getElementById('twPin').click(); await flush();
    expect(body.classList.contains('tw-lock')).toBe(true);
    w.towerClose(); await flush();
    expect(body.classList.contains('tw-lock')).toBe(false);
    expect(body.classList.contains('tw-docked')).toBe(false);
  });

  test('closing hands focus back to the header button', async () => {
    const w = await ready(ENABLED);
    w.towerClose();
    expect(w.document.activeElement.id).toBe('towerBtn');
  });

  test('sending posts text + page context, then renders SSE events into the transcript', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    expect(w.__posted).toMatchObject({ text: 'why is box red?', page: { tab: 'overall' } });
    expect(w.__posted.page.sub).toBeUndefined();
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
    expect(ctx.textContent).toContain('overall');
    expect(ctx.textContent).not.toContain('llama');
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

  test('starting a new thread parks the live run (it keeps answering) and clears the transcript (#994)', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    w.__sse.onEvent({ event: 'delta', text: 'box is' });
    w.document.getElementById('twNew').click(); await flush(); await flush();
    expect(w.__parked).toBe('/api/tower/runs/r1/park');
    expect(w.__stopped).toBeUndefined();
    expect(w.__sseClosed).toBe(true);
    expect(w.document.querySelector('#twBody .ans')).toBeNull();
    expect(w.document.querySelector('#twBody .empty h3').textContent).toBe('Ask about your hosts');
  });

  test('picking an older thread from history parks the live run instead of stopping it (#994)', async () => {
    const w = await ready(ENABLED, { threads: [{ id: 't9', title: 'Older' }] });
    await ask(w, 'why is box red?');
    w.__sse.onEvent({ event: 'delta', text: 'box is' });
    w.document.getElementById('twHist').click(); await flush(); await flush();
    w.document.querySelector('#twBody [data-thread="t9"]').click(); await flush(); await flush();
    expect(w.__parked).toBe('/api/tower/runs/r1/park');
    expect(w.__stopped).toBeUndefined();
    expect(w.__sseClosed).toBe(true);
    expect(w.document.querySelector('#twBody .ans')).toBeNull();
  });

  test('a stream that dropped events re-reads the thread from the store once the run is done (#994)', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'why is box red?');
    w.__rows = [{ role: 'user', content: 'why is box red?' }, { role: 'assistant', content: 'the full stored answer' }];
    w.__sse.onEvent({ event: 'truncated' });
    w.__sse.onEvent({ event: 'delta', text: 'tail only' });
    w.__sse.onEvent({ event: 'done', ok: true, calls: 0 });
    await flush(); await flush(); await flush();
    expect(w.__calls).toContain('GET /api/tower/threads/t1');
    expect(w.document.querySelector('#twBody .ans').textContent).toContain('the full stored answer');
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

  test('history rows sit under day headers with a time; an emptied day header goes away (#987)', async () => {
    const nowS = Date.now() / 1000;
    const w = await ready(ENABLED, { threads: [{ id: 't1', title: 'Just now', updated: nowS - 60 }, { id: 't9', title: 'Older', updated: nowS - 3 * 86400 }] });
    w.document.getElementById('twHist').click(); await flush(); await flush();
    const days = [...w.document.querySelectorAll('#twBody .hday')].map(e => e.textContent);
    expect(days[0]).toBe('Today');
    expect(days.length).toBe(2);
    expect(w.document.querySelector('#twBody .hrow .ht').textContent).toMatch(/\d/);
    const del = w.document.querySelector('#twBody [data-del="t1"]');
    del.click(); await flush(); del.click(); await flush(); await flush();
    expect([...w.document.querySelectorAll('#twBody .hday')].map(e => e.textContent)).toEqual([days[1]]);
  });

  test('a history row renames inline: Enter saves through PATCH, Esc restores (#987)', async () => {
    const w = await ready(ENABLED, { threads: [{ id: 't9', title: 'Older', updated: Date.now() / 1000 - 60 }] });
    w.document.getElementById('twHist').click(); await flush(); await flush();
    w.document.querySelector('#twBody [data-rn="t9"]').click(); await flush();
    let input = w.document.querySelector('#twBody input.rn-in');
    expect(input.value).toBe('Older');
    input.value = 'GPU heat';
    input.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter' })); await flush(); await flush();
    expect(w.__calls).toContain('PATCH /api/tower/threads/t9');
    expect(w.__renamed).toBe('GPU heat');
    expect(w.document.querySelector('#twBody [data-thread="t9"]').textContent).toBe('GPU heat');
    w.document.querySelector('#twBody [data-rn="t9"]').click(); await flush();
    input = w.document.querySelector('#twBody input.rn-in');
    input.value = 'dropped';
    input.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' })); await flush();
    expect(w.document.querySelector('#twBody [data-thread="t9"]').textContent).toBe('GPU heat');
    expect(w.__renamed).toBe('GPU heat');
  });

  test('the active conversation title is a pill in the tab row; clicking it edits, and History hides it (#987)', async () => {
    const w = await ready(ENABLED, { threads: [{ id: 't9', title: 'Older', updated: Date.now() / 1000 - 60 }] });
    const tabs = w.document.getElementById('twTabs');
    expect(w.document.getElementById('twTitle').hidden).toBe(true);
    await ask(w, 'why is box red?');
    const pill = tabs.querySelector('#twTitle');
    expect(pill.hidden).toBe(false);
    expect(pill.querySelector('.tt').textContent).toBe('why is box red?');
    expect(pill.querySelector('.pen')).not.toBeNull();
    pill.click(); await flush();
    const input = tabs.querySelector('input.rn-in');
    expect(input.value).toBe('why is box red?');
    input.value = 'Red box';
    input.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter' })); await flush(); await flush();
    expect(w.__calls).toContain('PATCH /api/tower/threads/t1');
    expect(tabs.querySelector('#twTitle .tt').textContent).toBe('Red box');
    w.__renameFail = true;
    tabs.querySelector('#twTitle').click(); await flush();
    const again = tabs.querySelector('input.rn-in'); again.value = 'nope';
    again.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter' })); await flush(); await flush();
    expect(tabs.querySelector('#twTitle .tt').textContent).toBe('Red box');
    // History hides the pill, a state poll while History is open keeps it hidden, Back brings it back
    w.document.getElementById('twHist').click(); await flush(); await flush();
    expect(tabs.querySelector('#twTitle').hidden).toBe(true);
    await w.towerRefreshState(); await flush();
    expect(tabs.querySelector('#twTitle').hidden).toBe(true);
    w.document.getElementById('twHistBack').click(); await flush();
    expect(tabs.querySelector('#twTitle').hidden).toBe(false);
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

const CARD = { title: 'Wake llama-server', target: 'box · llama.cpp', does: 'Sends a one-token completion so the server leaves idle sleep.', not: 'No model is loaded or unloaded.' };
const CONFIRM = { event: 'confirm', action_id: 'a1', tool: 'wake_server', args: { host: 'box' }, card: CARD, tier: 'operate', role: 'operator', actor: 'tower via adriel', expires_s: 600 };

const QUESTION = { event: 'question', action_id: 'q1', tool: 'ask_operator', question: 'Which host?', choices: ['box', 'mac'], actor: 'tower via adriel', expires_s: 600 };

describe('question cards (#1028)', () => {
  const pick = (w, val, i = 0) => w.document.querySelector(`#twBody .choice[data-pick="q1"][data-i="${i}"][data-val="${val}"]`);
  const submit = w => w.document.querySelector('#twBody [data-submit="q1"]');

  test('a question renders one row per choice plus Other, Submit disabled until a pick, and closes the stream without stopping the run', async () => {
    const w = await bootAndAsk(ENABLED, 'restart it');
    w.__sse.onEvent(QUESTION);
    const card = w.document.querySelector('#twBody .act.q[data-act="q1"]');
    expect(card).not.toBeNull();
    expect(card.querySelector('.eyebrow').textContent).toBe('Tower asks');
    expect(card.querySelector('h4').textContent).toBe('Which host?');
    expect([...card.querySelectorAll('.choice')].map(e => e.textContent)).toEqual(['box', 'mac', 'Other…']);
    expect(card.querySelector('.qtabs')).toBeNull();
    expect(card.querySelector('[data-other-input]')).toBeNull();
    expect(card.querySelector('.role')).toBeNull();
    expect(submit(w).disabled).toBe(true);
    expect(card.querySelector('[data-dismiss="q1"]')).not.toBeNull();
    expect(w.__sseClosed).toBe(true);
    expect(w.__calls.some(c => c.startsWith('POST /api/tower/runs/r1/stop'))).toBe(false);
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(true);
  });

  test('picking a row selects it; Submit posts the answers, re-attaches, and the answer event collapses the card once', async () => {
    const w = await bootAndAsk(ENABLED, 'restart it');
    w.__sse.onEvent(QUESTION);
    pick(w, 'mac').click(); await flush();
    expect(w.__calls).not.toContain('POST /api/tower/actions/q1/answer');
    expect(pick(w, 'mac').classList.contains('on')).toBe(true);
    expect(pick(w, 'box').classList.contains('on')).toBe(false);
    expect(submit(w).disabled).toBe(false);
    pick(w, 'box').click(); await flush();
    expect(pick(w, 'box').classList.contains('on')).toBe(true);
    submit(w).click(); await flush();
    expect(w.__calls).toContain('POST /api/tower/actions/q1/answer');
    expect(w.__decideBody).toEqual({ answers: ['box'] });
    expect(w.__sse.url).toBe('/api/tower/runs/r1/stream');
    expect(w.__sseClosed).toBe(false);
    expect(w.document.querySelector('#twBody .act[data-act="q1"]')).not.toBeNull();
    w.__sse.onEvent({ event: 'answer', action_id: 'q1', tool: 'ask_operator', status: 'answered', answer: 'box', actor: 'adriel' });
    expect(w.document.querySelector('#twBody .act[data-act]')).toBeNull();
    const tick = w.document.querySelector('#twBody .tick.act.ok');
    expect(tick.textContent).toContain('Which host?');
    expect([...w.document.querySelectorAll('#twBody .u')].map(e => e.textContent)).toEqual(['restart it', 'box']);
    w.__sse.onEvent({ event: 'answer', action_id: 'q1', tool: 'ask_operator', status: 'answered', answer: 'box', actor: 'adriel' });
    expect([...w.document.querySelectorAll('#twBody .u')].map(e => e.textContent)).toEqual(['restart it', 'box']);
    w.__sse.onEvent({ event: 'delta', text: 'restarting box' }); w.__sse.onEvent({ event: 'done', ok: true });
    expect(w.document.querySelector('#twBody .t:last-of-type .ans').textContent).toContain('restarting box');
    expect(w.document.querySelector('#twBody .caret')).toBeNull();
  });

  test('Other opens a text field in the row; typing enables Submit and Enter submits the typed answer', async () => {
    const w = await bootAndAsk(ENABLED, 'restart it');
    w.__sse.onEvent(QUESTION);
    pick(w, '__other__').click(); await flush();
    const inp = w.document.querySelector('#twBody [data-other-input="q1"]');
    expect(inp).not.toBeNull();
    expect(w.document.activeElement).toBe(inp);
    expect(submit(w).disabled).toBe(true);
    inp.value = '  the lab mini  ';
    inp.dispatchEvent(new w.Event('input', { bubbles: true }));
    expect(submit(w).disabled).toBe(false);
    inp.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await flush();
    expect(w.__decideBody).toEqual({ answers: ['the lab mini'] });
  });

  test('several questions render as tabs; answers carry across tabs and Submit sends all of them', async () => {
    const w = await bootAndAsk(ENABLED, 'unload it');
    w.__sse.onEvent({ ...QUESTION, question: 'Which host?', choices: ['box', 'mac'],
                      questions: [{ question: 'Which host?', choices: ['box', 'mac'], label: 'Host' }, { question: 'Which model?', choices: ['qwen3', 'gemma'], label: 'Model' }] });
    const tabs = () => [...w.document.querySelectorAll('#twBody .qtab')];
    expect(tabs().map(e => e.textContent)).toEqual(['Host', 'Model']);
    expect(w.document.querySelector('#twBody .act.q h4').textContent).toBe('Which host?');
    pick(w, 'mac', 0).click(); await flush();
    expect(submit(w).disabled).toBe(true);
    expect(tabs()[0].classList.contains('done')).toBe(true);
    // The pick moves on to the next unanswered tab by itself.
    expect(w.document.querySelector('#twBody .act.q h4').textContent).toBe('Which model?');
    expect(tabs()[1].classList.contains('on')).toBe(true);
    pick(w, '__other__', 1).click(); await flush();
    expect(tabs()[1].classList.contains('on')).toBe(true);
    const inp = w.document.querySelector('#twBody [data-other-input="q1"][data-i="1"]');
    inp.value = 'phi4'; inp.dispatchEvent(new w.Event('input', { bubbles: true }));
    tabs()[0].click(); await flush();
    expect(pick(w, 'mac', 0).classList.contains('on')).toBe(true);
    expect(submit(w).disabled).toBe(false);
    // Re-picking on a tab with everything answered stays put.
    pick(w, 'box', 0).click(); await flush();
    expect(tabs()[0].classList.contains('on')).toBe(true);
    submit(w).click(); await flush();
    expect(w.__decideBody).toEqual({ answers: ['box', 'phi4'] });
    w.__sse.onEvent({ event: 'answer', action_id: 'q1', status: 'answered', answer: 'Which host? box\nWhich model? phi4' });
    expect(w.document.querySelector('#twBody .tick.act.ok').textContent).toContain('Which host? (+1 more)');
  });

  test('Enter in an Other field moves to the next unanswered tab, and submits once all are answered', async () => {
    const w = await bootAndAsk(ENABLED, 'unload it');
    w.__sse.onEvent({ ...QUESTION, questions: [{ question: 'Which host?', choices: ['box'], label: 'Host' }, { question: 'Which model?', choices: ['qwen3'], label: 'Model' }] });
    pick(w, '__other__', 0).click(); await flush();
    const inp = w.document.querySelector('#twBody [data-other-input="q1"][data-i="0"]');
    inp.value = 'mini'; inp.dispatchEvent(new w.Event('input', { bubbles: true }));
    inp.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); await flush();
    expect(w.__calls).not.toContain('POST /api/tower/actions/q1/answer');
    expect(w.document.querySelector('#twBody .act.q h4').textContent).toBe('Which model?');
    pick(w, 'qwen3', 1).click(); await flush();
    expect(submit(w).disabled).toBe(false);
    submit(w).click(); await flush();
    expect(w.__decideBody).toEqual({ answers: ['mini', 'qwen3'] });
  });

  test('Dismiss posts deny and the run reports the dismissal as a crossed tick', async () => {
    const w = await bootAndAsk(ENABLED, 'restart it');
    w.__sse.onEvent(QUESTION);
    w.document.querySelector('#twBody [data-dismiss="q1"]').click(); await flush();
    expect(w.__calls).toContain('POST /api/tower/actions/q1/deny');
    expect(w.__sse.url).toBe('/api/tower/runs/r1/stream');
    w.__sse.onEvent({ event: 'answer', action_id: 'q1', status: 'denied', message: 'dismissed by the operator', actor: 'adriel' });
    expect(w.document.querySelector('#twBody .act[data-act]')).toBeNull();
    expect(w.document.querySelector('#twBody .tick.act.bad').textContent).toContain('dismissed by the operator');
    expect([...w.document.querySelectorAll('#twBody .u')].map(e => e.textContent)).toEqual(['restart it']);
  });

  test('an expired question shows a notice and closes out the turn', async () => {
    const w = await bootAndAsk(ENABLED, 'restart it');
    w.__sse.onEvent(QUESTION);
    w.__actFail = 410;
    pick(w, 'box').click(); await flush();
    submit(w).click(); await flush();
    expect(w.document.querySelector('#twBody .notice').textContent).toContain('That question expired');
    expect(w.document.querySelector('#twBody .act[data-act]')).toBeNull();
    expect(w.document.querySelector('#twBody .tick.act.bad').textContent).toContain('no answer from the operator');
    expect(w.document.querySelector('#twBody .caret')).toBeNull();
  });

  test('a reloaded thread parked on a question keeps the card and its Stop', async () => {
    const rows = [
      { role: 'user', content: 'restart it', ts: 1 },
      { role: 'action', content: JSON.stringify({ action_id: 'q1', run_id: 'r7', tool: 'ask_operator', card: { question: 'Which host?', choices: ['box'], questions: [{ question: 'Which host?', choices: ['box'], label: '' }] }, status: 'pending', actor: 'tower via adriel', expires: Math.floor(Date.now() / 1000) + 500 }), tool_name: 'ask_operator', ts: 2 },
    ];
    const w = await bootWithThread(ENABLED, rows);
    expect(w.document.querySelector('#twBody .act.q[data-act="q1"] .choice[data-val="box"]')).not.toBeNull();
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(true);
  });
});

describe('action cards', () => {
  test('confirm renders a card with the copy and closes the stream without stopping the run', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    const card = w.document.querySelector('#twBody .act[data-act="a1"]');
    expect(card).not.toBeNull();
    expect(card.querySelector('h4').textContent).toBe('Wake llama-server');
    expect(card.querySelector('.tgt').textContent).toBe('box · llama.cpp');
    expect(card.querySelector('p').textContent).toContain('leaves idle sleep');
    expect(card.querySelector('.role').textContent).toContain('answer and act · allowed for operators · audit: tower via adriel');
    expect(card.querySelector('[data-approve]')).not.toBeNull();
    expect(w.__sseClosed).toBe(true);
    expect(w.__calls.some(c => c.startsWith('POST /api/tower/runs/r1/stop'))).toBe(false);
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(true);
  });

  test('a card with option chips sends the picks with the approval (#1002)', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'bench box');
    const card = { title: 'Start a live bench', target: 'box · qwen3', does: 'Runs the benchmark.', not: 'Nothing is loaded.',
                   options: [{ name: 'bench', label: 'Bench set', choices: [{ value: 'qualitative', label: 'qualitative' }, { value: 'throughput_1k', label: 'throughput_1k' }], value: 'qualitative' },
                             { name: 'osl', label: 'Output length', choices: ['256', '1024'], value: '1024' }] };
    w.__sse.onEvent({ ...CONFIRM, action_id: 'b1', tool: 'start_benchmark', args: { kind: 'live', host: 'box', model: 'qwen3' }, card });
    const el = w.document.querySelector('#twBody .act[data-act="b1"]');
    expect([...el.querySelectorAll('.opt .ol')].map(e => e.textContent)).toEqual(['Bench set', 'Output length']);
    expect([...el.querySelectorAll('.chip.on')].map(e => e.dataset.val)).toEqual(['qualitative', '1024']);
    el.querySelector('.chip[data-name="bench"][data-val="throughput_1k"]').click(); await flush();
    const el2 = w.document.querySelector('#twBody .act[data-act="b1"]');
    expect([...el2.querySelectorAll('.chip.on')].map(e => e.dataset.val)).toEqual(['throughput_1k', '1024']);
    el2.querySelector('[data-approve]').click(); await flush();
    expect(w.__calls).toContain('POST /api/tower/actions/b1/approve');
    expect(w.__decideBody).toEqual({ options: { bench: 'throughput_1k', osl: '1024' } });
  });

  test('approve posts the decision and re-attaches to the run; the card collapses to a tick', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    w.document.querySelector('#twBody [data-approve]').click();
    await flush();
    expect(w.__calls).toContain('POST /api/tower/actions/a1/approve');
    expect(w.__sse.url).toBe('/api/tower/runs/r1/stream');
    expect(w.__sseClosed).toBe(false);
    w.__sse.onEvent({ event: 'action', action_id: 'a1', tool: 'wake_server', status: 'done', message: 'done', ms: 1200, actor: 'adriel' });
    expect(w.document.querySelector('#twBody .act[data-act]')).toBeNull();
    const tick = w.document.querySelector('#twBody .tick.act');
    expect(tick.textContent).toContain('Wake llama-server');
    expect(tick.textContent).toContain('1200 ms');
    w.__sse.onEvent({ event: 'delta', text: 'awake' }); w.__sse.onEvent({ event: 'done', ok: true });
    expect(w.document.querySelector('#twBody .ans').textContent).toContain('awake');
  });

  test('deny posts deny and a denied action shows as a crossed tick', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    w.document.querySelector('#twBody [data-deny]').click();
    await flush();
    expect(w.__calls).toContain('POST /api/tower/actions/a1/deny');
    w.__sse.onEvent({ event: 'action', action_id: 'a1', tool: 'wake_server', status: 'denied', message: 'denied by the operator', ms: 0, actor: 'adriel' });
    expect(w.document.querySelector('#twBody .tick.act.bad').textContent).toContain('denied');
  });

  test('a running action shows the card without buttons', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    w.document.querySelector('#twBody [data-approve]').click();
    await flush();
    w.__sse.onEvent({ event: 'action', action_id: 'a1', tool: 'wake_server', status: 'running', message: null, ms: null });
    const card = w.document.querySelector('#twBody .act[data-act="a1"]');
    expect(card).not.toBeNull();
    expect(card.querySelector('.eyebrow').textContent).toBe('Running…');
    expect(card.querySelector('[data-approve]')).toBeNull();
    expect(card.querySelector('[data-deny]')).toBeNull();
  });

  test('an expired approval shows a notice instead of re-attaching', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    w.__actFail = 410;
    w.document.querySelector('#twBody [data-approve]').click();
    await flush();
    expect(w.document.querySelector('#twBody .notice').textContent).toContain('That approval expired');
    expect(w.document.querySelector('#twBody .act[data-act]')).toBeNull();
  });

  test('an expired approval closes out the turn: no caret, one clean "approval expired" tick', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    w.__actFail = 410;
    w.document.querySelector('#twBody [data-approve]').click();
    await flush();
    expect(w.document.querySelector('#twBody .caret')).toBeNull();
    const tick = w.document.querySelector('#twBody .tick.act');
    expect(tick.textContent).toContain('approval expired');
    expect(tick.textContent).not.toContain('expired expired');
  });

  test('Stop while awaiting posts stop for the parked run and listens again for the denial', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    w.__stopOk = true;
    w.document.getElementById('twSend').click();
    await flush();
    expect(w.__calls).toContain('POST /api/tower/runs/r1/stop');
    expect(w.__sse.url).toBe('/api/tower/runs/r1/stream');
    expect(w.__sseClosed).toBe(false);
    w.__sse.onEvent({ event: 'action', action_id: 'a1', tool: 'wake_server', status: 'denied', message: 'Stopped.', ms: 0 });
    w.__sse.onEvent({ event: 'error', message: 'Stopped.' });
    expect(w.document.querySelector('#twBody .tick.act.bad').textContent).toContain('Stopped.');
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(false);
  });

  test('a refused approval (403) explains and leaves the card and the run alone', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    w.__actFail = 403;
    w.document.querySelector('#twBody [data-approve]').click();
    await flush();
    expect(w.document.querySelector('#twBody .notice').textContent).toContain('does not allow this action');
    expect(w.document.querySelector('#twBody .act[data-act="a1"] [data-approve]')).not.toBeNull();
    expect(w.__sseClosed).toBe(true);
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(true);
    w.document.getElementById('twSend').click();
    await flush();
    expect(w.__calls).toContain('POST /api/tower/runs/r1/stop');
  });

  test('an already-decided action (409) explains and listens again for the real outcome', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    w.__actFail = 409;
    w.document.querySelector('#twBody [data-approve]').click();
    await flush();
    expect(w.document.querySelector('#twBody .notice').textContent).toContain('already decided');
    expect(w.__sse.url).toBe('/api/tower/runs/r1/stream');
    expect(w.__sseClosed).toBe(false);
    w.__sse.onEvent({ event: 'action', action_id: 'a1', tool: 'wake_server', status: 'done', message: 'done', ms: 30 });
    expect(w.document.querySelector('#twBody .act[data-act]')).toBeNull();
  });

  test('a network failure leaves the decision retryable', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    w.__actNetFail = true;
    w.document.querySelector('#twBody [data-approve]').click();
    await flush();
    expect(w.document.querySelector('#twBody .notice').textContent).toContain('could not record the decision');
    expect(w.document.querySelector('#twBody .act[data-act="a1"] [data-approve]')).not.toBeNull();
    expect(w.__sseClosed).toBe(true);
  });

  test('an admin-only action is read-only for an operator and decidable for an admin', async () => {
    const ADMIN_CONFIRM = { ...CONFIRM, tier: 'admin', role: 'admin' };
    let w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(ADMIN_CONFIRM);
    let card = w.document.querySelector('#twBody .act[data-act="a1"]');
    expect(card.querySelector('[data-approve]')).toBeNull();
    expect(card.querySelector('[data-deny]')).toBeNull();
    expect(card.querySelector('.eyebrow').textContent).toBe("Needs an admin's approval");
    expect(card.querySelector('.role').textContent).toContain('admins only');
    w = await bootAndAsk({ ...ENABLED, admin: true, capabilities: 'admin' }, 'wake box');
    w.__sse.onEvent(ADMIN_CONFIRM);
    card = w.document.querySelector('#twBody .act[data-act="a1"]');
    expect(card.querySelector('[data-approve]')).not.toBeNull();
    expect(card.querySelector('.eyebrow').textContent).toBe('Needs your approval');
  });

  test('a stored pending card past its expiry renders without buttons', async () => {
    const rows = [{ role: 'user', content: 'wake box' },
                  { role: 'action', content: JSON.stringify({ action_id: 'a9', tool: 'wake_server', args: { host: 'box' }, card: CARD, status: 'pending', expires: 1, message: null }), tool_name: 'wake_server' }];
    const w = await bootWithThread({ ...ENABLED, capabilities: 'operate' }, rows);
    const card = w.document.querySelector('#twBody .act[data-act="a9"]');
    expect(card).not.toBeNull();
    expect(card.querySelector('[data-approve]')).toBeNull();
    expect(card.querySelector('.eyebrow').textContent).toBe('Approval expired');
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(false);
  });
  test('a reloaded thread parked on a pending card takes its run back: Stop posts to it and listens for the denial (#956)', async () => {
    const rows = [{ role: 'user', content: 'wake box' },
                  { role: 'action', content: JSON.stringify({ action_id: 'a9', run_id: 'r7', tool: 'wake_server', args: { host: 'box' }, card: CARD, status: 'pending', expires: Math.floor(Date.now() / 1000) + 500, message: null }), tool_name: 'wake_server' }];
    const w = await bootWithThread({ ...ENABLED, capabilities: 'operate' }, rows);
    const send = w.document.getElementById('twSend');
    expect(send.classList.contains('stop')).toBe(true);
    expect(w.document.querySelector('#twBody .act[data-act="a9"] [data-approve]')).not.toBeNull();
    w.__stopOk = true;
    send.click(); await flush();
    expect(w.__calls).toContain('POST /api/tower/runs/r7/stop');
    expect(w.__sse.url).toBe('/api/tower/runs/r7/stream');
    w.__sse.onEvent({ event: 'action', action_id: 'a9', tool: 'wake_server', status: 'denied', message: 'Stopped.', ms: 0 });
    w.__sse.onEvent({ event: 'error', message: 'Stopped.' });
    expect(w.document.querySelector('#twBody .tick.act.bad').textContent).toContain('Stopped.');
    expect(send.classList.contains('stop')).toBe(false);
  });
  test('a reloaded thread with a run still answering re-attaches and receives the reply', async () => {
    const w = boot({ ...ENABLED }, { stored: { thread: 't1' } });
    w.__rows = [{ role: 'user', content: 'wake the llama model' }];
    w.__activeRun = 'r5';
    await w.towerRefreshState(); await flush();
    w.towerOpen(); await flush(); await flush();
    expect(w.__sse.url).toBe('/api/tower/runs/r5/stream');
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(true);
    w.__sse.onEvent({ event: 'delta', text: 'Woke it.' });
    w.__sse.onEvent({ event: 'done', ok: true });
    expect(w.document.querySelector('#twBody .ans').textContent).toBe('Woke it.');
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(false);
  });
  test('a reloaded thread whose action is running re-attaches to the run stream (#956)', async () => {
    const rows = [{ role: 'user', content: 'wake box' },
                  { role: 'action', content: JSON.stringify({ action_id: 'a9', run_id: 'r8', tool: 'wake_server', args: { host: 'box' }, card: CARD, status: 'running', expires: Math.floor(Date.now() / 1000) + 500, message: null }), tool_name: 'wake_server' }];
    const w = await bootWithThread({ ...ENABLED, capabilities: 'operate' }, rows);
    expect(w.__sse.url).toBe('/api/tower/runs/r8/stream');
    expect(w.document.getElementById('twSend').classList.contains('stop')).toBe(true);
    w.__sse.onEvent({ event: 'action', action_id: 'a9', tool: 'wake_server', status: 'done', ms: 12 });
    w.__sse.onEvent({ event: 'delta', text: 'Woke box.' });
    w.__sse.onEvent({ event: 'done', ok: true });
    expect(w.document.querySelector('#twBody .tick.act.ok')).not.toBeNull();
    expect(w.document.querySelector('#twBody .ans').textContent).toBe('Woke box.');
  });
});

describe('insights', () => {
  // Timestamps are taken per test: vitest loads the file long before a late test runs.
  const nowS = () => Date.now() / 1000;
  const mkIns = (over = {}) => ({ id: 'i1', alert_id: 'a1', rule: 'llama-server asleep', host: 'box', severity: 'warning', summary: 'asleep since 21:25',
                                  detail: 'Idle sleep.', suggested_action: 'Wake it', status: 'new', playbook_id: 'wake_llama', playbook_title: 'Wake llama-server',
                                  playbook_safe: true, created: nowS() - 600, checks: [{ name: 'host_detail', summary: 'read host detail · box · 8 ms', ok: true }], ...over });
  const mkState = (created, over = {}) => ({ ...ENABLED, capabilities: 'operate', insights_new: 1, insights_rev: created,
                                             latest_insight: { id: 'i1', rule: 'llama-server asleep', host: 'box', severity: 'warning', summary: 'asleep since 21:25', created }, ...over });
  const QUIET = { ...ENABLED, capabilities: 'operate', insights_new: 0, insights_rev: 1 };
  async function openWith(state, insights, opts) {
    const w = boot(state, opts); w.__insights = insights;
    await w.towerRefreshState(); await flush(); w.towerOpen(); await flush(); await flush(); await flush();
    return w;
  }
  const $ = (w, id) => w.document.getElementById(id);
  const onIns = w => !$(w, 'twInsView').hidden && $(w, 'twBody').hidden;

  test('opening with unseen insights lands on the Insights tab: compact cards with Details, Apply and the applied line', async () => {
    const INS = mkIns();
    const w = await openWith(mkState(INS.created), [INS]);
    const view = $(w, 'twInsView');
    expect($(w, 'twTabs').hidden).toBe(false);
    expect(onIns(w)).toBe(true);
    expect($(w, 'twTabIns').getAttribute('aria-selected')).toBe('true');
    expect(w.__seen).toBe(1);
    expect($(w, 'twInsCount').hidden).toBe(true);                                        // seen once on screen
    expect(view.querySelector('.ins-h .cnt').textContent).toBe('1 new');
    expect(view.querySelector('.ins .rule').textContent).toBe('llama-server asleep');
    expect(view.querySelector('.ins .age').textContent).toBe('10 min');
    expect(view.querySelector('.ins .sum').textContent).toBe('asleep since 21:25');
    expect(view.querySelector('[data-ins-apply]').textContent).toBe('Wake llama-server');
    expect(view.querySelector('[data-ins-open]').textContent).toBe('Alert');
    expect(view.querySelector('.detb')).toBeNull();
    view.querySelector('[data-ins-det]').click(); await flush();
    expect(view.querySelector('.detb').textContent).toContain('read host detail · box');
    expect(view.querySelector('.detb').textContent).toContain('Wake it');
    view.querySelector('[data-ins-apply]').click();
    expect(view.querySelector('[data-ins="i1"] .run').textContent).toBe('Running Wake llama-server…');   // before the apply answers
    await flush(); await flush(); await flush();
    expect(w.__insCalls).toEqual(['apply i1']);
    expect(view.querySelector('.ins.done .ok').textContent).toBe('✓ Wake llama-server · alice');
    expect(view.querySelector('.ins.done .detb').textContent).toContain('Audit: tower via alice');
    expect(view.querySelector('.ins.done [data-ins-dismiss]')).not.toBeNull();
  });

  test('a card shows its metric snapshot; Troubleshoot opens a titled thread and asks about the alert (#980)', async () => {
    const INS = mkIns({ snapshot: { metric: 'system/cpu_total', unit: '%', minutes: 60, points: [[0, 10], [60, 90]], threshold: 80 } });
    const w = await openWith(mkState(INS.created), [INS]);
    const view = $(w, 'twInsView');
    expect(view.querySelector('.ins .snap svg path').getAttribute('d')).toMatch(/^M2\.0 34\.0 L238\.0 2\.0$/);
    expect(view.querySelector('.ins .snap .thr')).not.toBeNull();
    expect(view.querySelector('.ins .snapc').textContent).toBe('system/cpu_total · last 60 min · 10%–90% · threshold 80%');
    expect(view.querySelector('[data-ins-chat]').textContent).toBe('Troubleshoot');
    view.querySelector('[data-ins-chat]').click();
    await flush(); await flush(); await flush(); await flush(); await flush();
    expect(onIns(w)).toBe(false);
    expect(w.__threadBody.title).toBe('Troubleshoot: llama-server asleep · box');
    expect(w.__posted.text).toMatch(/^Troubleshoot the alert "llama-server asleep" on box \(alert id a1\)\. /);
    expect(w.__posted.text).toContain("Tower's earlier read: asleep since 21:25");
    expect(w.__posted.page.alert_id).toBe('a1'); expect(w.__posted.page.tab).toBe('events');
    expect($(w, 'twTitle').textContent).toContain('Troubleshoot: llama-server asleep');
    const plain = await openWith(mkState(INS.created), [mkIns()]);
    expect($(plain, 'twInsView').querySelector('.snap')).toBeNull();
  });

  test('opening with nothing unseen stays on the conversation; the tabs switch views', async () => {
    const w = await openWith(QUIET, [mkIns({ status: 'seen' })]);
    expect(onIns(w)).toBe(false);
    expect($(w, 'twTabConv').classList.contains('on')).toBe(true);
    expect($(w, 'twInsNote').hidden).toBe(true);
    $(w, 'twTabIns').click(); await flush();
    expect(onIns(w)).toBe(true);
    expect($(w, 'twInsView').querySelectorAll('.ins')).toHaveLength(1);
    $(w, 'twTabConv').click(); await flush();
    expect(onIns(w)).toBe(false);
  });

  test('a new insight during a conversation shows a one-line notice; View opens the tab and highlights the card', async () => {
    const w = await openWith(QUIET, []);
    await ask(w, 'why is box red?');
    w.__sse.onEvent({ event: 'delta', text: 'box is hot' });
    w.__sse.onEvent({ event: 'done', ok: true, calls: 0 });
    w.__insights = [mkIns()];
    w.__state = mkState(nowS(), { insights_rev: 2 });
    await w.towerRefreshState(); await flush(); await flush();
    const note = $(w, 'twInsNote');
    expect(onIns(w)).toBe(false);
    expect(note.hidden).toBe(false);
    expect(note.querySelector('.txt').textContent).toBe('New insight: llama-server asleep');
    expect($(w, 'twInsCount').textContent).toBe('1');
    expect(w.__seen || 0).toBe(0);                                                      // not seen while the conversation is on screen
    note.querySelector('[data-ins-view]').click(); await flush();
    expect(onIns(w)).toBe(true);
    expect($(w, 'twInsView').querySelector('[data-ins="i1"]').classList.contains('flash')).toBe(true);
    expect(w.__seen).toBe(1);
    expect(note.hidden).toBe(true);
    expect($(w, 'twInsCount').hidden).toBe(true);
    $(w, 'twTabConv').click(); await flush();
    expect($(w, 'twBody').querySelector('.ans').textContent).toBe('box is hot');
    w.__state = mkState(nowS(), { insights_new: 2, insights_rev: 3 });
    await w.towerRefreshState(); await flush(); await flush();
    expect(note.querySelector('.txt').textContent).toBe('2 new insights · latest: llama-server asleep');
  });

  test('asking a question from the Insights tab switches back to the conversation', async () => {
    const w = await openWith(mkState(nowS() - 600), [mkIns()]);
    expect(onIns(w)).toBe(true);
    await ask(w, 'why is box red?');
    expect(onIns(w)).toBe(false);
    expect(w.__posted.text).toBe('why is box red?');
  });

  test('read tier hides Apply; dismiss removes a card; Alert switches to Events; Dismiss all leaves the empty state', async () => {
    const INS = mkIns();
    const w = await openWith({ ...mkState(INS.created), capabilities: 'read' },
                             [INS, mkIns({ id: 'i2', alert_id: 'a2' }), mkIns({ id: 'i3', alert_id: 'a3', status: 'applied', applied_by: 'tower via alarm a3' })]);
    const view = $(w, 'twInsView');
    expect(view.querySelector('[data-ins-apply]')).toBeNull();
    expect(view.querySelectorAll('.ins')).toHaveLength(3);
    expect(view.querySelector('[data-ins="i3"] .ok').textContent).toBe('✓ Wake llama-server · auto');
    view.querySelector('[data-ins-dismiss="i1"]').click(); await flush(); await flush();
    expect(w.__insCalls).toEqual(['dismiss i1']);
    expect(view.querySelectorAll('.ins')).toHaveLength(2);
    view.querySelector('[data-ins-open]').click(); await flush();
    expect(w.__tab).toBe('events');
    expect($(w, 'towerOverlay').classList.contains('open')).toBe(false);
    w.towerOpen(); await flush(); await flush(); await flush();
    $(w, 'twTabIns').click(); await flush();
    $(w, 'twInsDismissAll').click(); await flush(); await flush();
    expect(w.__dismissedAll).toBe(true);
    expect(view.querySelector('.ins')).toBeNull();
    expect(view.querySelector('.empty h3').textContent).toBe('No insights');
  });
  test('the Alert link hands the alert id to focusAlarmAlert when the page provides it (#962)', async () => {
    const INS = mkIns({ alert_id: 'al-42' });
    const w = await openWith(mkState(INS.created), [INS]);
    w.focusAlarmAlert = id => { w.__focused = id; };
    $(w, 'twInsView').querySelector('[data-ins-open]').click(); await flush();
    expect(w.__focused).toBe('al-42');
    expect(w.__tab).toBeUndefined();
  });

  test('a refused apply explains on the tab and leaves the card; an applying card shows Running with no actions', async () => {
    const INS = mkIns();
    const w = await openWith(mkState(INS.created), [INS, mkIns({ id: 'i2', alert_id: 'a2', status: 'applying' })]);
    w.__applyFail = 403;
    const view = $(w, 'twInsView');
    expect(view.querySelector('[data-ins="i2"] .run').textContent).toBe('Running Wake llama-server…');
    expect(view.querySelector('[data-ins="i2"] [data-ins-dismiss]')).toBeNull();
    view.querySelector('[data-ins-apply]').click(); await flush(); await flush(); await flush();
    expect(view.querySelector('.notice').textContent).toContain('does not allow this playbook');
    expect(view.querySelector('[data-ins-apply]')).not.toBeNull();
    w.__applyFail = 409;
    view.querySelector('[data-ins-apply]').click(); await flush(); await flush(); await flush();
    expect(view.querySelector('.notice').textContent).toContain('alert changed');
  });

  test('with no chat model loaded the Insights tab still works; Tower off hides the tabs', async () => {
    const w = await openWith({ ...mkState(nowS() - 600), model: null }, [mkIns()]);
    expect(onIns(w)).toBe(true);
    expect($(w, 'twInsView').querySelector('[data-ins-apply]')).not.toBeNull();
    expect(w.__seen).toBe(1);
    $(w, 'twTabConv').click(); await flush();
    expect($(w, 'twBody').querySelector('.empty h3').textContent).toBe('Nothing to think with');
    const off = await openWith({ ok: true, enabled: false, admin: true }, [mkIns()]);
    expect($(off, 'twTabs').hidden).toBe(true);
    expect($(off, 'twInsView').hidden).toBe(true);
    expect($(off, 'twBody').querySelector('.on-card h3').textContent).toBe('Turn on Tower');
  });

  test('new insights count on the badge while closed; opening marks them seen; each fresh insight toasts once', async () => {
    const w = boot(ENABLED);
    await w.towerRefreshState(); await flush();
    expect($(w, 'towerBadge').hidden).toBe(true);
    w.__state = mkState(nowS() + 60);
    await w.towerRefreshState(); await flush();
    const badge = $(w, 'towerBadge');
    expect(badge.hidden).toBe(false); expect(badge.textContent).toBe('1');
    expect(w.__toasts).toHaveLength(1);
    expect(w.__toasts[0][0]).toBe('Tower · llama-server asleep'); expect(w.__toasts[0][1]).toBe('asleep since 21:25'); expect(w.__toasts[0][2]).toBe('warning');
    await w.towerRefreshState(); await flush();
    expect(w.__toasts).toHaveLength(1);                    // same insight, no second toast
    w.__state = mkState(nowS() + 60, { latest_insight: { id: 'i2', rule: 'GPU hot', host: 'box', severity: 'critical', summary: 'hot', created: nowS() + 60 } });
    await w.towerRefreshState(); await flush();
    expect(w.__toasts).toHaveLength(2);                    // count unchanged, but a different newest insight
    w.__insights = [mkIns()];
    w.towerOpen(); await flush(); await flush(); await flush();
    expect(w.__seen).toBe(1);
    expect(badge.hidden).toBe(true);
  });

  test('an insight that predates the page load does not toast; a reply badge and insight badge add up', async () => {
    const w = boot(ENABLED);
    await w.towerRefreshState(); await flush();
    w.__state = mkState(nowS() - 600);                     // older than this page load
    await w.towerRefreshState(); await flush();
    expect(w.__toasts).toHaveLength(0);
    expect($(w, 'towerBadge').textContent).toBe('1');
    await ask(w, 'why?');                                  // sending works with the drawer closed
    w.__sse.onEvent({ event: 'done', ok: true, calls: 0 });
    expect($(w, 'towerBadge').textContent).toBe('2');
  });

  test('History shows Discord conversations for admins under their own group and opens them (#996)', async () => {
    const w = await ready({ ...ENABLED, admin: true }, { threads: [{ id: 't9', title: 'Older', updated: Date.now() / 1000 }] });
    w.__discord = [{ id: 'd1', user: 'discord:111', title: 'why is box red?', updated: Date.now() / 1000 }];
    w.document.getElementById('twHist').click(); await flush(); await flush();
    const body = w.document.getElementById('twBody');
    expect([...body.querySelectorAll('.hday')].map(e => e.textContent)).toEqual(['Today', 'Discord · 111']);
    expect(body.querySelector('.cnt').textContent).toBe('2');
    body.querySelector('[data-thread="d1"]').click(); await flush(); await flush();
    expect(w.__calls).toContain('GET /api/tower/threads/d1');
  });

  test('History returns to the conversation tab; a poll with a new insight shows the notice and leaves History alone', async () => {
    const w = await openWith(mkState(nowS() - 600, { insights_new: 0, insights_rev: 1 }), [mkIns({ status: 'seen' })]);
    $(w, 'twTabIns').click(); await flush();
    $(w, 'twHist').click(); await flush(); await flush();
    expect(onIns(w)).toBe(false);
    const body = $(w, 'twBody');
    expect(body.querySelector('.ins-h h3').textContent).toBe('History');
    w.__insights = [mkIns({ id: 'i2', alert_id: 'a2' })]; w.__state = mkState(nowS() - 600, { insights_rev: 5 });
    await w.towerRefreshState(); await flush(); await flush();
    expect(body.querySelector('.ins-h h3').textContent).toBe('History');
    expect($(w, 'twInsNote').hidden).toBe(false);
  });

  test('the Insights tab reloads when insights_rev moves even with nothing unseen (auto-applied)', async () => {
    const w = await openWith({ ...QUIET, insights_rev: 100 }, []);
    $(w, 'twTabIns').click(); await flush();
    expect($(w, 'twInsView').querySelector('.empty h3').textContent).toBe('No insights');
    w.__insights = [mkIns({ status: 'applied', applied_by: 'tower via alarm a1', resolved: nowS() })];
    w.__state = { ...QUIET, insights_rev: 200 };
    await w.towerRefreshState(); await flush(); await flush();
    expect($(w, 'twInsView').querySelector('.ins.done .ok').textContent).toBe('✓ Wake llama-server · auto');
    const loads = w.__calls.filter(c => c === 'GET /api/tower/insights').length;
    await w.towerRefreshState(); await flush(); await flush();
    expect(w.__calls.filter(c => c === 'GET /api/tower/insights').length).toBe(loads);   // same rev, no reload
  });

  test('no toast while on the Events tab; it shows on the next poll after leaving', async () => {
    const w = boot(ENABLED);
    await w.towerRefreshState(); await flush();
    w._activeTab = 'events';
    w.__state = mkState(nowS() + 60);
    await w.towerRefreshState(); await flush();
    expect(w.__toasts).toHaveLength(0);
    w._activeTab = 'overall';
    await w.towerRefreshState(); await flush();
    expect(w.__toasts).toHaveLength(1);
    expect(w.__toasts[0][0]).toBe('Tower · llama-server asleep');
  });
});

describe('Tower drawer: sub-views, waiting and re-attach (#1014, #1016)', () => {
  test('the page context carries the active tab\'s own sub-view, none for tabs without one', async () => {
    const w = await ready(ENABLED);
    w._activeTab = 'llm';
    await ask(w, 'what is loaded?');
    expect(w.__posted.page).toMatchObject({ tab: 'llm', sub: 'llama' });
    w.__sse.onEvent({ event: 'done', ok: true });
    w._activeTab = 'events';
    await ask(w, 'alarms?');
    expect(w.__posted.page.tab).toBe('events');
    expect(w.__posted.page.sub).toBeUndefined();
  });

  test('a waiting status shows one wait line until the next event, and reattach reopens the stream', async () => {
    const w = await ready(ENABLED);
    await ask(w, 'wake box and tell me when it is up');
    const first = w.__sse;
    w.__sse.onEvent({ event: 'status', state: 'waiting', name: 'host awake · box', elapsed_s: 45, timeout_s: 300 });
    const body = w.document.getElementById('twBody');
    expect(body.querySelector('.wait').textContent).toBe('waiting for host awake · box · 45 s of 300');
    expect(body.querySelectorAll('.wait').length).toBe(1);
    w.__sse.onEvent({ event: 'reattach' });
    expect(w.__sseClosed).toBe(false);
    expect(w.__sse).not.toBe(first);
    expect(w.__sse.url).toBe('/api/tower/runs/r1/stream');
    expect(body.querySelector('.wait')).not.toBeNull();
    w.__sse.onEvent({ event: 'tool', name: 'wait_until', ok: true, ms: 45000, summary: 'read wait until · host awake · box · ready after 45 s · 45000 ms', result: { ok: true } });
    expect(body.querySelector('.wait')).toBeNull();
    w.__sse.onEvent({ event: 'delta', text: 'box is awake.' });
    w.__sse.onEvent({ event: 'done', ok: true });
    expect(body.textContent).toContain('box is awake.');
  });
});

describe('Tower drawer: running cards and help suggestions (#1017, #1018)', () => {
  test('approve tints the card and drops its buttons before the action result arrives', async () => {
    const w = await bootAndAsk({ ...ENABLED, capabilities: 'operate' }, 'wake box');
    w.__sse.onEvent(CONFIRM);
    const before = w.document.querySelector('#twBody .act');
    expect(before.classList.contains('running')).toBe(false);
    w.document.querySelector('#twBody [data-approve]').click();
    await flush();
    const card = w.document.querySelector('#twBody .act');
    expect(card.classList.contains('running')).toBe(true);
    expect(card.querySelector('[data-approve]')).toBeNull();
    expect(card.querySelector('.eyebrow').textContent).toBe('Running…');
    w.__sse.onEvent({ event: 'action', action_id: 'a1', tool: 'wake_server', status: 'done', message: 'done', ms: 1200, actor: 'adriel' });
    expect(w.document.querySelector('#twBody .act.running')).toBeNull();
    expect(w.document.querySelector('#twBody .tick.act.ok')).not.toBeNull();
  });

  test('the developer and help suggestions render highlighted on the Overall tab', async () => {
    const w = await ready(ENABLED);
    const hi = [...w.document.querySelectorAll('#twBody .sug.hi')].map(b => b.textContent);
    expect(hi).toEqual(['Who develops LLM Systems Manager?', 'How do I get help?']);
    expect(w.document.querySelector('#twBody .sug:not(.hi)')).not.toBeNull();
  });
});
