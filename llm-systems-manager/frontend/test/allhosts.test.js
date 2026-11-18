// #1432 phase 4c-1: Dashboards · All hosts — Right now table, Compare over time charts, minute poll and range.
// Runs kit.js, js/lib/overall-view.js, js/lib/hosts-view.js and js/allhosts.js on the real #dash-all markup with fetch stubbed.
import { describe, test, expect, afterEach, vi } from 'vitest';
import { JSDOM } from 'jsdom';
import OV from '../js/lib/overall-view.js';
import HV from '../js/lib/hosts-view.js';
import { srcFile, fnSrc, constAsWindowProp, evalGlobal, flush } from './helpers/harness.js';

const indexDoc = new JSDOM(srcFile('index.html')).window.document;
const pageEl = indexDoc.getElementById('dash-all');
const pageHtml = pageEl ? pageEl.outerHTML : '';
const allhosts = srcFile('js/allhosts.js');
const foundation = srcFile('js/foundation.js');
const boot = srcFile('js/boot.js');
evalGlobal(srcFile('kit/kit.js'));
window.OV = OV;
window.HV = HV;

const ID_LLAMA = '056ba13c-689e-411e-b865-74a83b9086cd';
const ID_LMS = '42cf47d9-2849-47c3-99b9-bca2a9955fd1';
const ID_SYS = '7f0c2a51-0d7e-4c8e-9a43-1b2f3e4d5c6a';
const THREE = [
  { agent_id: ID_LLAMA, hostname: 'llm-systems-agent-llama', providers: ['llama'], online: true, age_s: 2.1 },
  { agent_id: ID_LMS, hostname: 'llm-systems-lmstudio.local', providers: ['lms'], online: true, age_s: 0.8 },
  { agent_id: ID_SYS, hostname: 'llm-systems-agent-bare', providers: [], online: false, age_s: 900 },
];

// One row a minute for the last hour, newest first; level sets every metric of the host.
function rowsFor(level, now) {
  const out = [];
  for (let i = 0; i < 60; i++) {
    out.push({ ts: new Date(now - i * 60000).toISOString(), cpu_total: level, ram_percent: 40 + level / 10,
      disk_root_pct: 50 + level / 10, net_recv: 1000 * level, io_write: 500 * level });
  }
  return out;
}

// Answers /api/agents/hosts with hosts (status hostsDown.n times 500 first) and /api/history/hosts with rows of each
// host's level keyed by hostname; ids in missing are left out of the history answer and names in failed.list are
// reported as failed fields.
function stubFetch(hosts, levels, missing = [], hostsDown = { n: 0 }, failed = { list: [] }) {
  const calls = [];
  window.fetch = vi.fn(async (url) => {
    calls.push(url);
    if (url.startsWith('/api/agents/hosts')) {
      if (hostsDown.n > 0) { hostsDown.n--; return { ok: false, status: 500, json: async () => ({}) }; }
      return { ok: true, json: async () => ({ hosts: JSON.parse(JSON.stringify(hosts)) }) };
    }
    if (url.startsWith('/api/history/hosts')) {
      const out = {};
      hosts.filter(h => !missing.includes(h.agent_id)).forEach(h => { out[h.hostname] = rowsFor(levels[h.agent_id] || 0, Date.now()); });
      return { ok: true, json: async () => (failed.list.length ? { hosts: out, failed: failed.list.slice() } : { hosts: out }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  return calls;
}

// A store like the shell's: string values per key.
function makeStore() {
  const m = {};
  return { getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null), setItem: (k, v) => { m[k] = String(v); } };
}

// A fresh #dash-all and a fresh allhosts.js.
function load() {
  if (typeof window.ahLeave === 'function') window.ahLeave();
  document.body.innerHTML = pageHtml;
  delete window._activeTab;
  delete window.ahState;
  window.mcShellStore = makeStore();
  window.LivePause = { on: false, every: (fn, ms) => setInterval(fn, ms) };
  window._selectAgent = vi.fn();
  window.switchSubTab = vi.fn();
  Object.keys(window.KIT_SERIES_OFF).forEach(k => { delete window.KIT_SERIES_OFF[k]; });
  evalGlobal(allhosts);
}

async function settle() { for (let i = 0; i < 5; i++) await flush(); }

const nowRows = () => [...document.querySelectorAll('#ah-b-now tbody tr')];
const rowOf = (name) => nowRows().find(r => r.textContent.includes(name));
const historyCalls = (calls) => calls.filter(u => u.startsWith('/api/history'));

afterEach(() => {
  if (typeof window.ahLeave === 'function') window.ahLeave();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('All hosts page (#1432)', () => {
  test('All hosts is the first Dashboards item and the default Dashboards page', () => {
    const subs = [...indexDoc.querySelectorAll('#side .nav.sub[data-tab="dashboard"]')];
    expect(subs[0].id).toBe('subTabBtnDashAll');
    expect(subs[0].dataset.sub).toBe('all');
    expect(subs[0].querySelector('.side-lab').textContent).toBe('All hosts');
    expect(indexDoc.querySelector('#side .side-sub').nextElementSibling.id).toBe('subTabBtnDashAll');
    expect(pageEl.parentElement.id).toBe('dashboardTab');
    expect(pageEl.parentElement.firstElementChild.id).toBe('dash-all');

    document.body.innerHTML = indexDoc.getElementById('dashboardTab').outerHTML;
    evalGlobal(fnSrc(foundation, '_getDashSubTab') + '\nwindow._getDashSubTab = _getDashSubTab;');
    expect(window._getDashSubTab()).toBe('all');
    document.querySelectorAll('.sub-tab-panel').forEach(p => p.classList.remove('active'));
    expect(window._getDashSubTab()).toBe('all');
    document.getElementById('dash-llamacpp').classList.add('active');
    expect(window._getDashSubTab()).toBe('llamacpp');
    document.getElementById('dash-llamacpp').classList.remove('active');
    document.getElementById('dash-forecast').classList.add('active');
    expect(window._getDashSubTab()).toBe('forecast');

    evalGlobal([constAsWindowProp(boot, '_subTabState'), constAsWindowProp(boot, '_SUB_TAB_MAP')].join('\n'));
    expect(window._subTabState.dashboard).toBe('all');
    expect(window._SUB_TAB_MAP.dashboard.subs[0]).toBe('all');
  });

  test('Right now lists each host; a host with an LLM server opens its page', async () => {
    load();
    stubFetch(THREE, { [ID_LLAMA]: 90, [ID_LMS]: 20, [ID_SYS]: 5 });
    await window.ahEnter();
    await settle();
    expect(nowRows()).toHaveLength(3);
    const llama = rowOf('llm-systems-agent-llama');
    expect(llama.textContent).toContain('90%');
    expect(llama.querySelector('.bad')).not.toBeNull();
    expect(rowOf('llm-systems-lmstudio').querySelector('.bad')).toBeNull();
    llama.querySelector('button').click();
    expect(window._selectAgent).toHaveBeenCalledWith('llama', ID_LLAMA);
    expect(window.switchSubTab).toHaveBeenCalledWith('dashboard', 'llamacpp');
    rowOf('llm-systems-lmstudio').querySelector('button').click();
    expect(window._selectAgent).toHaveBeenLastCalledWith('lms', ID_LMS);
    expect(window.switchSubTab).toHaveBeenLastCalledWith('dashboard', 'lmstudio');
    const bare = rowOf('llm-systems-agent-bare');
    expect(bare.querySelector('button.ah-go')).toBeNull();
    expect(bare.textContent).toContain('offline');
    bare.click();
    expect(window.switchSubTab).toHaveBeenCalledTimes(2);
    // A host with no LLM server opens the host page with that host picked.
    const open = bare.querySelector('button[data-ah-host]');
    expect(open.textContent).toBe('No LLM server · open host');
    window.hpSetHost = vi.fn();
    open.click();
    expect(window.hpSetHost).toHaveBeenCalledWith('host', ID_SYS);
    expect(window.switchSubTab).toHaveBeenLastCalledWith('dashboard', 'host');
    expect(window.hpSetHost.mock.invocationCallOrder[0]).toBeLessThan(window.switchSubTab.mock.invocationCallOrder[2]);
    expect(window._selectAgent).toHaveBeenCalledTimes(2);
    delete window.hpSetHost;
    expect(document.getElementById('dash-all').getAttribute('data-kit-page')).toBe('allhosts');
    expect(document.querySelector('[data-card="ah-now"] .p-note').textContent).toBe('click a host to open it');
    expect(document.querySelector('[data-card="ah-now"] .p-note').title).toBe('click a host to open it');
    const shown = document.getElementById('dash-all').textContent;
    for (const bad of ['NaN', 'undefined', 'null', 'Infinity']) expect(shown, bad).not.toContain(bad);
  });

  test('one history request for every host; a host missing from it shows dashes', async () => {
    load();
    const calls = stubFetch(THREE, { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 }, [ID_LMS]);
    await window.ahEnter();
    await settle();
    expect(historyCalls(calls)).toEqual(['/api/history/hosts?since_minutes=1440&max_rows=864', '/api/history/hosts?since_minutes=5']);
    expect(nowRows()).toHaveLength(3);
    expect(rowOf('llm-systems-agent-llama').textContent).toContain('30%');
    expect(rowOf('llm-systems-agent-bare').textContent).toContain('5.0%');
    const failed = [...rowOf('llm-systems-lmstudio').querySelectorAll('td')].slice(1).map(td => td.textContent.trim());
    expect(failed).toEqual(['—', '—', '—']);
    for (const m of HV.METRICS) {
      const chips = document.querySelectorAll(`#ah-b-${m.card.slice(3)} .legend [data-kit-series]`);
      expect(chips.length, m.card).toBe(2);
    }
  });

  test('seven hosts: each chart lists seven chips with the two quietest hidden', async () => {
    load();
    const hosts = [], levels = {};
    for (let i = 1; i <= 7; i++) {
      const id = 'host-' + i;
      hosts.push({ agent_id: id, hostname: 'h' + i, providers: [], online: true, age_s: 1 });
      levels[id] = i * 10;
    }
    stubFetch(hosts, levels);
    await window.ahEnter();
    await settle();
    for (const m of HV.METRICS) {
      const chips = [...document.querySelectorAll(`#ah-b-${m.card.slice(3)} .legend [data-kit-series]`)];
      expect(chips.length, m.card).toBe(7);
      const off = chips.filter(c => c.classList.contains('off')).map(c => c.getAttribute('data-kit-series'));
      expect(off, m.card).toEqual(['ah-' + m.key + '|h1', 'ah-' + m.key + '|h2']);
      expect(document.querySelector(`[data-card="${m.card}"] .p-note`).textContent, m.card).toBe("Some hosts hidden · click a host's chip to show it");
      expect(document.querySelector(`[data-card="${m.card}"] .p-note`).title, m.card).toBe("Some hosts hidden · click a host's chip to show it");
    }
    expect(document.querySelector('#ah-b-cpu .ts[data-kit-ts]')).not.toBeNull();
  });

  test('no hosts: the page says so', async () => {
    load();
    stubFetch([], {});
    await window.ahEnter();
    await settle();
    expect(document.getElementById('ah-b-now').textContent).toBe('No hosts reported yet.');
    for (const m of HV.METRICS) {
      expect(document.querySelector(`#ah-b-${m.card.slice(3)} .ov-empty`), m.card).not.toBeNull();
    }
  });

  test('leaving stops the minute poll; returning reuses the cached rows', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = stubFetch(THREE, { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 });
    await window.ahEnter();
    await vi.advanceTimersByTimeAsync(0);
    const seeded = calls.length;
    expect(historyCalls(calls)).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(60000);
    expect(calls.length).toBeGreaterThan(seeded);
    expect(historyCalls(calls).slice(2)).toEqual(['/api/history/hosts?since_minutes=5']);
    window.ahLeave();
    const left = calls.length;
    await vi.advanceTimersByTimeAsync(180000);
    expect(calls.length).toBe(left);
    await window.ahEnter();
    await vi.advanceTimersByTimeAsync(0);
    const back = historyCalls(calls.slice(left));
    expect(back).toHaveLength(1);
    expect(back[0]).not.toContain('since_minutes=1440');
    expect(nowRows()).toHaveLength(3);
  });

  test('range 6 saves the choice and refetches six hours', async () => {
    load();
    const calls = stubFetch(THREE, { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 });
    await window.ahEnter();
    await settle();
    const before = calls.length;
    await window.ahSetRange(6);
    await settle();
    expect(window.mcShellStore.getItem('mgr.ahRange')).toBe('6');
    const again = historyCalls(calls.slice(before));
    expect(again).toEqual(['/api/history/hosts?since_minutes=360&max_rows=1080', '/api/history/hosts?since_minutes=5']);
    expect(window.kitTsData(document.querySelector('#ah-b-cpu .ts[data-kit-ts]')).labels).toHaveLength(360);
  });

  test('compare charts break a line where a host has no data instead of dropping it to 0', async () => {
    load();
    stubFetch(THREE, { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 });
    await window.ahEnter();
    await settle();
    const ts = document.querySelector('#ah-b-cpu .ts[data-kit-ts]');
    expect(window.kitTsData(ts).gaps).toBe('break');
    // Most of the day has no rows: no line point sits on the 0 gridline.
    const zeroY = ts.querySelector('svg line').getAttribute('y1');
    const ys = [...ts.querySelectorAll('path[fill="none"]')].flatMap(p => [...p.getAttribute('d').matchAll(/[ML][\d.]+ ([\d.]+)/g)].map(m => m[1]));
    expect(ys.length).toBeGreaterThan(0);
    expect(ys).not.toContain(zeroY);
  });

  test('a quiet host first seen later in the visit starts hidden too', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const hosts = [], levels = {};
    for (let i = 2; i <= 7; i++) {
      hosts.push({ agent_id: 'host-' + i, hostname: 'h' + i, providers: [], online: true, age_s: 1 });
      levels['host-' + i] = i * 10;
    }
    stubFetch(hosts, levels);
    await window.ahEnter();
    await vi.advanceTimersByTimeAsync(0);
    const off = (m) => [...document.querySelectorAll(`#ah-b-${m.card.slice(3)} .legend [data-kit-series]`)]
      .filter(c => c.classList.contains('off')).map(c => c.getAttribute('data-kit-series'));
    for (const m of HV.METRICS) expect(off(m), m.card).toEqual(['ah-' + m.key + '|h2']);
    levels['host-1'] = 10;
    stubFetch([{ agent_id: 'host-1', hostname: 'h1', providers: [], online: true, age_s: 1 }].concat(hosts), levels);
    await vi.advanceTimersByTimeAsync(60000);
    for (const m of HV.METRICS) {
      expect(document.querySelectorAll(`#ah-b-${m.card.slice(3)} .legend [data-kit-series]`).length, m.card).toBe(7);
      expect(off(m), m.card).toEqual(['ah-' + m.key + '|h1', 'ah-' + m.key + '|h2']);
    }
  });

  test('hosts with the same shown name get apart labels and chips', async () => {
    load();
    const hosts = [
      { agent_id: 'aaaaaa11-0000', hostname: 'dup', providers: [], online: true, age_s: 1 },
      { agent_id: 'bbbbbb22-0000', hostname: 'dup.local', providers: [], online: true, age_s: 1 },
    ];
    stubFetch(hosts, { 'aaaaaa11-0000': 10, 'bbbbbb22-0000': 20 });
    await window.ahEnter();
    await settle();
    const names = nowRows().map(r => r.querySelector('.ah-name').textContent);
    expect(names).toEqual(['dup (aaaaaa)', 'dup (bbbbbb)']);
    const keys = [...document.querySelectorAll('#ah-b-cpu .legend [data-kit-series]')].map(c => c.getAttribute('data-kit-series'));
    expect(keys).toEqual(['ah-cpu_total|dup (aaaaaa)', 'ah-cpu_total|dup (bbbbbb)']);
  });

  test('Reset this page resets All hosts only; Forecast has nothing to reset', async () => {
    load();
    stubFetch(THREE, { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 });
    await window.ahEnter();
    await settle();
    evalGlobal([fnSrc(foundation, '_kitRows'), fnSrc(foundation, '_applyRowsHidden'), fnSrc(foundation, '_ahResetLayout'),
      fnSrc(foundation, 'resetCurrentTabLayout'),
      'window._applyRowsHidden = _applyRowsHidden; window.resetCurrentTabLayout = resetCurrentTabLayout;'].join('\n'));
    window._activeTab = 'dashboard';
    window._themedConfirm = vi.fn(async () => true);
    window.saveLayout = vi.fn();
    window.renderSettingsPanel = vi.fn();
    const reset = vi.spyOn(window, 'kitLayoutReset');
    window.layout = { hidden: ['gpu'], order: ['gpu'], cols: 3, cardSizes: { gpu: '2x1' }, allHostsRowsHidden: ['ah-cmp'] };
    window._applyRowsHidden();
    expect(document.querySelector('#dash-all section.row[data-row="ah-cmp"]').hidden).toBe(true);
    // A chip the operator turned on stays on until the reset.
    const k = 'ah-cpu_total|llm-systems-agent-bare';
    window.KIT_SERIES_OFF[k] = true;
    window.fetch = vi.fn();

    window._getDashSubTab = () => 'forecast';
    await window.resetCurrentTabLayout();
    expect(window._themedConfirm).not.toHaveBeenCalled();
    expect(window.layout.allHostsRowsHidden).toEqual(['ah-cmp']);

    window._getDashSubTab = () => 'all';
    await window.resetCurrentTabLayout();
    expect(window._themedConfirm).toHaveBeenCalledTimes(1);
    expect(window.layout.allHostsRowsHidden).toEqual([]);
    expect(document.querySelector('#dash-all section.row[data-row="ah-cmp"]').hidden).toBe(false);
    expect(reset).toHaveBeenCalledWith('allhosts', document.getElementById('dash-all'), window.mcShellStore);
    expect(window.KIT_SERIES_OFF[k]).toBeUndefined();
    expect(window.layout).toMatchObject({ hidden: ['gpu'], order: ['gpu'], cols: 3, cardSizes: { gpu: '2x1' } });
    expect(window.saveLayout).toHaveBeenCalled();
    expect(window.fetch).not.toHaveBeenCalled();
  });

  test('a failed host list says so, and the next minute retries', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = stubFetch(THREE, { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 }, [], { n: 1 });
    await window.ahEnter();
    await vi.advanceTimersByTimeAsync(0);
    expect(document.getElementById('ah-b-now').textContent).toBe('Could not load hosts.');
    expect(document.getElementById('ah-b-now').textContent).not.toContain('No hosts reported yet.');
    expect(historyCalls(calls)).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(60000);
    expect(nowRows()).toHaveLength(3);
    expect(historyCalls(calls)[0]).toBe('/api/history/hosts?since_minutes=1440&max_rows=864');
  });

  test('the minute poll skips while the browser tab is hidden; ahRefresh fetches the gap', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = stubFetch(THREE, { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 });
    await window.ahEnter();
    await vi.advanceTimersByTimeAsync(0);
    const seeded = calls.length;
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    await vi.advanceTimersByTimeAsync(10 * 60000);
    expect(calls.length).toBe(seeded);
    hidden.mockReturnValue(false);
    await window.ahRefresh();
    expect(historyCalls(calls).slice(2)).toEqual(['/api/history/hosts?since_minutes=1440&max_rows=864', '/api/history/hosts?since_minutes=5']);
    window.ahLeave();
    const left = calls.length;
    await window.ahRefresh();
    expect(calls.length).toBe(left);
  });

  test('resume backfill on All hosts runs ahRefresh', () => {
    evalGlobal(fnSrc(boot, '_rebackfillActiveView') + '\nwindow._rebackfillActiveView = _rebackfillActiveView;');
    window._activeTab = 'dashboard';
    window._subTabState = { dashboard: 'all' };
    const was = window.ahRefresh;
    window.ahRefresh = vi.fn(() => Promise.resolve());
    window._rebackfillActiveView();
    expect(window.ahRefresh).toHaveBeenCalledTimes(1);
    window.ahRefresh = was;
    delete window._activeTab;
  });

  test('each host keeps its colour while the busiest order changes', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const levels = { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 };
    stubFetch(THREE, levels);
    await window.ahEnter();
    await vi.advanceTimersByTimeAsync(0);
    const colours = () => Object.fromEntries([...document.querySelectorAll('#ah-b-cpu .legend [data-kit-series]')]
      .map(c => [c.getAttribute('data-kit-series'), c.querySelector('i').getAttribute('style')]));
    const first = colours();
    expect(Object.values(first)).toEqual(['background:var(--s1)', 'background:var(--s2)', 'background:var(--s3)']);
    levels[ID_SYS] = 95;
    await vi.advanceTimersByTimeAsync(60000);
    expect(colours()).toEqual(first);
  });

  test('after a partial read the whole range is retried every 5 minutes, with 5-minute polls between', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const levels = { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 };
    const failed = { list: ['ram_percent'] };
    const calls = stubFetch(THREE, levels, [], { n: 0 }, failed);
    // F = the whole range (always followed at once by a 5-minute fetch), 5 = the last five minutes.
    const kinds = () => historyCalls(calls).map(u => (u.includes('since_minutes=1440&max_rows=864') ? 'F'
      : u.endsWith('?since_minutes=5') ? '5' : u)).join('');
    const minutes = async (n) => { for (let i = 0; i < n; i++) await vi.advanceTimersByTimeAsync(60000); };
    await window.ahEnter();
    await vi.advanceTimersByTimeAsync(0);
    // Hosts with no cache show what arrived.
    expect(rowOf('llm-systems-agent-llama').textContent).toContain('30%');
    await minutes(4);
    expect(kinds()).toBe('F55555');
    await minutes(1);
    expect(kinds()).toBe('F55555F5');
    // A clean 5-minute poll does not end the retry; the next clean whole-range read does.
    failed.list = [];
    await minutes(5);
    expect(kinds()).toBe('F55555F55555F5');
    await minutes(1);
    expect(kinds()).toBe('F55555F55555F55');
    // A poll answer with a failed field leaves the cached rows alone and starts the 5-minute retry again.
    failed.list = ['cpu_total'];
    levels[ID_LLAMA] = 70;
    await minutes(1);
    expect(rowOf('llm-systems-agent-llama').textContent).toContain('30%');
    expect(rowOf('llm-systems-agent-llama').textContent).not.toContain('70%');
    failed.list = [];
    await minutes(1);
    expect(rowOf('llm-systems-agent-llama').textContent).toContain('70%');
    await minutes(2);
    expect(kinds()).toBe('F55555F55555F55555F5');
  });

  test('polls use five minutes or the whole range, never a longer gap', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = stubFetch(THREE, { [ID_LLAMA]: 30, [ID_LMS]: 20, [ID_SYS]: 5 });
    await window.ahEnter();
    await vi.advanceTimersByTimeAsync(0);
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    await vi.advanceTimersByTimeAsync(7 * 60000);
    hidden.mockReturnValue(false);
    await vi.advanceTimersByTimeAsync(60000);
    await vi.advanceTimersByTimeAsync(60000);
    const urls = historyCalls(calls);
    expect(urls.slice(2)).toEqual(['/api/history/hosts?since_minutes=1440&max_rows=864', '/api/history/hosts?since_minutes=5', '/api/history/hosts?since_minutes=5']);
    for (const u of urls) expect(/since_minutes=(5|1440)\b/.test(u), u).toBe(true);
  });

  test('Right now has values right after the whole-range seed, whose newest point is 8 minutes old', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    load();
    const calls = [];
    // The seed ends 8 minutes ago (coarse buckets); the 5-minute fetch carries the last minutes.
    window.fetch = vi.fn(async (url) => {
      calls.push(url);
      if (url.startsWith('/api/agents/hosts')) return { ok: true, json: async () => ({ hosts: THREE }) };
      const now = Date.now(), seed = !url.includes('since_minutes=5');
      const out = {};
      THREE.forEach((h, i) => {
        const rows = seed ? rowsFor(10 * (i + 1), now - 8 * 60000) : rowsFor(10 * (i + 1), now).slice(0, 5);
        out[h.hostname] = rows;
      });
      return { ok: true, json: async () => ({ hosts: out }) };
    });
    await window.ahEnter();
    await vi.advanceTimersByTimeAsync(0);
    expect(historyCalls(calls)).toEqual(['/api/history/hosts?since_minutes=1440&max_rows=864', '/api/history/hosts?since_minutes=5']);
    expect(rowOf('llm-systems-agent-llama').textContent).toContain('10%');
    expect(rowOf('llm-systems-agent-bare').textContent).toContain('30%');
    // The next regular poll is still a minute after entry.
    await vi.advanceTimersByTimeAsync(59000);
    expect(historyCalls(calls)).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1000);
    expect(historyCalls(calls)).toHaveLength(3);
  });
});

