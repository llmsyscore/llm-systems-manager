// DOM contract for the companion Tower screen (#964): the real companion.html
// Tower slice booted against js/companion-tower.js + js/lib/tower-view.js.
import { describe, test, expect, afterEach } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile } from './helpers/harness.js';

const html = srcFile('companion.html');
const slice = (a, b) => html.slice(html.indexOf(a), html.indexOf(b, html.indexOf(a)));
const SCREEN = slice('<section class="screen tower" id="scr-tower"', '<section class="screen" id="scr-energy"');
const TAB = slice('<button class="tab" data-tab="tower"', '<button class="tab" data-tab="energy"');

const _windows = [];
afterEach(() => { while (_windows.length) _windows.pop().close(); });

const ON = { ok: true, enabled: true, admin: false, model: 'qwen3-14b', provider: 'llama', hosts: ['box'], capabilities: 'operate', insights_new: 0 };
const OFF = { ok: true, enabled: false, admin: false };

function boot(state, opts = {}) {
  const dom = new JSDOM(`<!doctype html><html><body><main class="screens">${SCREEN}</main><nav class="tabbar" id="tabbar">${TAB}</nav></body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/companion', pretendToBeVisual: true });
  const w = dom.window;
  _windows.push(w);
  w.localStorage.clear();
  if (opts.stored) w.localStorage.setItem('companionTowerThread', opts.stored);
  w.__state = state; w.__calls = []; w.__rows = opts.rows || []; w.__activeRun = opts.activeRun || null;
  w.__insights = opts.insights || []; w.__threads = opts.threads || [];
  w.fetch = (url, o) => {
    const method = (o && o.method) || 'GET';
    w.__calls.push(method + ' ' + url);
    const json = (body, status = 200) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
    if (url === '/api/tower/state') return json(w.__state);
    if (url === '/api/tower/threads' && method === 'POST') { w.__threadBody = JSON.parse(o.body); return json({ ok: true, thread: { id: 't1', title: 'New thread' } }); }
    if (url === '/api/tower/threads') return json({ ok: true, threads: w.__threads });
    if (/^\/api\/tower\/threads\/[^/]+$/.test(url)) {
      if (w.__threadGone) return json({ ok: false, error: 'unknown thread' }, 404);
      return json({ ok: true, thread: { id: url.split('/').pop(), title: 'Older' }, messages: w.__rows, active_run: w.__activeRun });
    }
    if (/\/messages$/.test(url)) { w.__posted = JSON.parse(o.body); return w.__postError ? json({ ok: false, error: w.__postError }, 409) : json({ ok: true, run_id: 'r1' }); }
    const act = url.match(/^\/api\/tower\/actions\/([^/]+)\/(approve|deny|answer)$/);
    if (act) { w.__decision = { aid: act[1], verb: act[2], body: o && o.body ? JSON.parse(o.body) : null }; return json({ ok: true, run_id: 'r2', status: 'x', tool: 'wake_server', thread_id: 't1' }); }
    if (url === '/api/tower/insights') return json({ ok: true, insights: w.__insights, new: w.__insights.filter(r => r.status === 'new' && !r.seen_at).length });
    if (url === '/api/tower/insights/seen') { w.__seen = (w.__seen || 0) + 1; return json({ ok: true, seen: 1 }); }
    const ins = url.match(/^\/api\/tower\/insights\/([^/]+)\/(apply|dismiss)$/);
    if (ins) {
      w.__insCalls = (w.__insCalls || []).concat([ins[2] + ' ' + ins[1]]);
      if (ins[2] === 'apply') w.__insights = w.__insights.map(r => r.id === ins[1] ? { ...r, status: 'applied', applied_by: 'tower via alice', resolved: Date.now() / 1000 } : r);
      return json({ ok: true });
    }
    if (/\/(park|stop)$/.test(url)) { w.__parked = (w.__parked || []).concat([url]); return json({ ok: true }); }
    return json({ ok: true });
  };
  w.SG = { open: (o) => { w.__sse = o; w.__sseClosed = false; return { close() { w.__sseClosed = true; } }; } };
  w.__sheet = []; w.__alert = null; w.__badge = [];
  w.__visible = true;
  const inject = code => { const s = w.document.createElement('script'); s.textContent = code; w.document.body.appendChild(s); };
  inject(srcFile('js/lib/tower-view.js'));
  inject(srcFile('js/companion-tower.js'));
  w.eval(`window.__ctrl = window.CTower.create({
    $: (id) => document.getElementById(id),
    sheet: { open(t, b, fn) { window.__sheet.push({ title: t, body: b, fn }); }, close() { window.__sheetClosed = true; },
             confirm(t, d, l, danger, fn) { window.__sheet.push({ title: t, detail: d, label: l, fn }); } },
    openAlert: (id) => { window.__alert = id; },
    badge: (n) => { window.__badge.push(n); },
    visible: () => window.__visible,
  });`);
  return w;
}
const flush = () => new Promise(r => setTimeout(r, 0));
const $ = (w, id) => w.document.getElementById(id);
async function ready(state, opts) {
  const w = boot(state, opts);
  await w.__ctrl.init(); await w.__ctrl.refresh(); await flush(); await flush();
  return w;
}
async function ask(w, text) {
  $(w, 'towerInput').value = text;
  $(w, 'towerInput').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter' }));
  await flush(); await flush();
}
const emit = (w, ev) => { w.__sse.onEvent(ev); };
const click = (w, sel) => { const el = w.document.querySelector(sel); expect(el, sel).toBeTruthy(); el.click(); };

describe('companion Tower screen', () => {
  test('off: init reports disabled, the ask bar hides and the body says so', async () => {
    const w = await ready(OFF);
    expect(w.__ctrl.enabled).toBe(false);
    expect($(w, 'towerAsk').hidden).toBe(true);
    expect($(w, 'towerBody').textContent).toMatch(/Tower is off/);
    expect(w.__calls.some(c => c.startsWith('POST /api/tower/threads'))).toBe(false);
  });

  test('on: refresh creates a thread tagged as the companion and shows suggestions', async () => {
    const w = await ready(ON);
    expect(w.__ctrl.enabled).toBe(true);
    expect(w.__threadBody).toEqual({ page: { tab: 'companion' } });
    expect(w.localStorage.getItem('companionTowerThread')).toBe('t1');
    expect(w.document.querySelectorAll('#towerBody [data-sug]').length).toBeGreaterThan(2);
    expect($(w, 'towerAsk').hidden).toBe(false);
  });

  test('ask: Enter posts the message, streams the answer and clears Stop on done', async () => {
    const w = await ready(ON);
    await ask(w, 'Is anything red?');
    expect(w.__posted).toEqual({ text: 'Is anything red?', page: { tab: 'companion' } });
    expect($(w, 'towerInput').value).toBe('');
    expect(w.__sse.url).toBe('/api/tower/runs/r1/stream');
    expect($(w, 'towerBody').querySelector('.u').textContent).toBe('Is anything red?');
    emit(w, { event: 'status', state: 'thinking' });
    expect($(w, 'towerStop').hidden).toBe(false);
    emit(w, { event: 'tool', name: 'host_detail', ok: true, ms: 12, summary: 'read host detail · box · 12 ms', result: { a: 1 } });
    emit(w, { event: 'delta', text: 'All **green**.' });
    emit(w, { event: 'done', ok: true });
    expect($(w, 'towerBody').querySelector('.ans').innerHTML).toMatch(/<b>green<\/b>|<strong>green<\/strong>/);
    expect($(w, 'towerBody').querySelector('.tick').textContent).toMatch(/read host detail · box/);
    expect($(w, 'towerStop').hidden).toBe(true);
    // Expanding a tick shows the raw result.
    click(w, '#towerBody [data-tk]');
    expect($(w, 'towerBody').querySelector('.raw').textContent).toMatch(/"a": 1/);
  });

  test('a rejected send surfaces the mapped error in the turn', async () => {
    const w = await ready(ON);
    w.__postError = 'rate_limited';
    await ask(w, 'again');
    expect($(w, 'towerBody').textContent).toMatch(/Too many questions in a minute/);
  });

  test('approval card: Approve confirms in the sheet, then posts and re-attaches', async () => {
    const w = await ready(ON);
    await ask(w, 'wake it');
    emit(w, { event: 'confirm', action_id: 'a1', tool: 'wake_server', args: {}, tier: 'operate', role: 'operator', actor: 'tower via alice',
              card: { title: 'Wake llama-server', target: 'box', does: 'Sends a wake request.', not: 'Does not load a model.' }, expires_s: 600 });
    expect(w.__sseClosed).toBe(true);
    expect($(w, 'towerBody').querySelector('.act h4').textContent).toBe('Wake llama-server');
    click(w, '#towerBody [data-approve="a1"]');
    expect(w.__sheet.length).toBe(1);
    expect(w.__sheet[0].label).toBe('Approve');
    expect(w.__sheet[0].detail).toMatch(/box · Sends a wake request\. · Does not load a model\./);
    expect(w.__decision).toBeUndefined();
    w.__sheet[0].fn(); await flush(); await flush();
    expect(w.__decision).toEqual({ aid: 'a1', verb: 'approve', body: null });
    expect(w.__sse.url).toBe('/api/tower/runs/r2/stream');
  });

  test('deny posts straight away without the sheet', async () => {
    const w = await ready(ON);
    await ask(w, 'wake it');
    emit(w, { event: 'confirm', action_id: 'a1', tool: 'wake_server', args: {}, tier: 'operate', role: 'operator', card: { title: 'Wake' } });
    click(w, '#towerBody [data-deny="a1"]'); await flush(); await flush();
    expect(w.__sheet.length).toBe(0);
    expect(w.__decision.verb).toBe('deny');
  });

  test('an admin-only card shows no buttons to an operator', async () => {
    const w = await ready(ON);
    await ask(w, 'restart it');
    emit(w, { event: 'confirm', action_id: 'a2', tool: 'restart_server', args: {}, tier: 'admin', role: 'admin', card: { title: 'Restart' } });
    expect($(w, 'towerBody').textContent).toMatch(/Needs an admin's approval/);
    expect(w.document.querySelector('#towerBody [data-approve]')).toBeNull();
  });

  test('question card: a pick enables Submit, Other takes text, Submit posts the answers', async () => {
    const w = await ready(ON);
    await ask(w, 'which host?');
    emit(w, { event: 'question', action_id: 'q1', tool: 'ask_operator', question: 'Which host?', choices: ['box', 'lab'], expires_s: 600 });
    expect($(w, 'towerBody').querySelector('[data-submit="q1"]').disabled).toBe(true);
    click(w, '#towerBody [data-pick="q1"][data-val="__other__"]');
    const inp = $(w, 'towerBody').querySelector('[data-other-input="q1"]');
    inp.value = 'garage'; inp.dispatchEvent(new w.Event('input', { bubbles: true }));
    expect($(w, 'towerBody').querySelector('[data-submit="q1"]').disabled).toBe(false);
    click(w, '#towerBody [data-submit="q1"]'); await flush(); await flush();
    expect(w.__decision).toEqual({ aid: 'q1', verb: 'answer', body: { answers: ['garage'] } });
    expect(w.__sse.url).toBe('/api/tower/runs/r2/stream');
  });

  test('a stored thread reloads its turns and re-attaches to a run still answering', async () => {
    const rows = [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'hello' }, { role: 'user', content: 'more?' }];
    const w = await ready(ON, { stored: 't7', rows, activeRun: 'r9' });
    expect(w.__calls).toContain('GET /api/tower/threads/t7');
    expect(w.__calls.some(c => c.startsWith('POST /api/tower/threads'))).toBe(false);
    expect(w.document.querySelectorAll('#towerBody .u').length).toBe(2);
    expect(w.__sse.url).toBe('/api/tower/runs/r9/stream');
  });

  test('a stored thread the manager no longer knows falls back to a new one', async () => {
    const w = boot(ON, { stored: 'gone' });
    w.__threadGone = true;
    await w.__ctrl.init(); await w.__ctrl.refresh(); await flush();
    expect(w.__threadBody).toBeTruthy();
    expect(w.localStorage.getItem('companionTowerThread')).toBe('t1');
  });

  test('the conversations sheet lists threads and picking one loads it', async () => {
    const w = await ready(ON, { threads: [{ id: 't1', title: 'New thread', updated: Date.now() / 1000 }, { id: 't5', title: 'Energy last week', updated: Date.now() / 1000 - 90000 }] });
    $(w, 'towerHistory').click(); await flush(); await flush();
    expect(w.__sheet.length).toBe(1);
    expect(w.__sheet[0].title).toBe('Conversations');
    expect(w.__sheet[0].body).toMatch(/Energy last week/);
    expect(w.__sheet[0].body).toMatch(/data-thread="t1" disabled/);
    const dom = new JSDOM(w.__sheet[0].body);
    const target = dom.window.document.querySelector('[data-thread="t5"]');
    w.__sheet[0].fn({ target });
    await flush(); await flush();
    expect(w.__sheetClosed).toBe(true);
    expect(w.__calls).toContain('GET /api/tower/threads/t5');
    expect(w.localStorage.getItem('companionTowerThread')).toBe('t5');
  });

  test('New parks a running answer and starts a fresh thread', async () => {
    const w = await ready(ON);
    await ask(w, 'slow one');
    emit(w, { event: 'status', state: 'thinking' });
    $(w, 'towerNew').click(); await flush(); await flush();
    expect(w.__parked).toEqual(['/api/tower/runs/r1/park']);
    expect(w.document.querySelectorAll('#towerBody .u').length).toBe(0);
  });

  test('insights: count chip, Dismiss, the Alert link and Apply through the sheet', async () => {
    const insights = [
      { id: 'i1', status: 'new', rule: 'host_down', host: 'lab', severity: 'critical', summary: 'lab stopped reporting.', alert_id: 'al-1', created: Date.now() / 1000 - 120,
        playbook_id: 'pb.wake', playbook_title: 'Wake the host', playbook_safe: true, steps: [['wake_server', {}]] },
      { id: 'i2', status: 'seen', rule: 'ram_high', host: 'box', summary: 'RAM at 93 %.', alert_id: 'al-2', created: Date.now() / 1000 - 3600, seen_at: 1 },
    ];
    const w = await ready(ON, { insights });
    expect($(w, 'towerInsCount').hidden).toBe(false);
    expect($(w, 'towerInsCount').textContent).toBe('1');
    expect(w.__badge[w.__badge.length - 1]).toBe(1);
    expect($(w, 'towerIns').hidden).toBe(true);
    click(w, '#towerChips [data-tview="ins"]'); await flush();
    expect($(w, 'towerIns').hidden).toBe(false);
    expect($(w, 'towerConv').hidden).toBe(true);
    expect($(w, 'towerAsk').hidden).toBe(true);
    expect(w.__seen).toBe(1);
    expect($(w, 'towerInsCount').hidden).toBe(true);
    expect(w.document.querySelectorAll('#towerInsList .ins').length).toBe(2);
    expect(w.document.querySelector('#towerInsList .ins[data-ins="i1"]').classList.contains('crit')).toBe(true);
    // Operator on the operate tier may apply a safe playbook; it confirms first.
    click(w, '#towerInsList [data-ins-apply="i1"]');
    expect(w.__sheet[0].label).toBe('Apply');
    expect(w.__sheet[0].title).toBe('Wake the host');
    expect(w.__insCalls).toBeUndefined();
    w.__sheet[0].fn(); await flush(); await flush(); await flush();
    expect(w.__insCalls).toEqual(['apply i1']);
    expect(w.document.querySelector('#towerInsList .ins[data-ins="i1"] .ok').textContent).toMatch(/Wake the host · alice/);
    click(w, '#towerInsList [data-ins-open="al-2"]');
    expect(w.__alert).toBe('al-2');
    click(w, '#towerInsList [data-ins-dismiss="i2"]'); await flush(); await flush();
    expect(w.__insCalls).toEqual(['apply i1', 'dismiss i2']);
    expect(w.document.querySelectorAll('#towerInsList .ins').length).toBe(1);
  });

  test('a reply that lands while the screen is away counts on the tab badge until the next visible refresh', async () => {
    const w = await ready(ON);
    await ask(w, 'later');
    w.__visible = false;
    emit(w, { event: 'delta', text: 'done now' });
    emit(w, { event: 'done', ok: true });
    expect(w.__badge[w.__badge.length - 1]).toBe(1);
    w.__visible = true;
    await w.__ctrl.refresh(); await flush();
    expect(w.__badge[w.__badge.length - 1]).toBe(0);
  });

  test('the read tier hides Apply and marks admin-only playbooks', async () => {
    const insights = [{ id: 'i3', status: 'new', rule: 'r', summary: 's', created: Date.now() / 1000, playbook_id: 'pb.restart', playbook_title: 'Restart', playbook_safe: false, steps: [['restart', {}]] }];
    const w = await ready({ ...ON, capabilities: 'read' }, { insights });
    click(w, '#towerChips [data-tview="ins"]'); await flush();
    expect(w.document.querySelector('#towerInsList [data-ins-apply]')).toBeNull();
    expect($(w, 'towerInsList').textContent).toMatch(/Admin only/);
  });

  test('no chat model: the thread is not created and the input is disabled', async () => {
    const w = await ready({ ...ON, model: null, hosts: [] });
    expect(w.__threadBody).toBeUndefined();
    expect($(w, 'towerInput').disabled).toBe(true);
    expect($(w, 'towerBody').textContent).toMatch(/No chat model is loaded/);
  });
});
