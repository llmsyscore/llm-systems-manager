// #797: Settings sub-tab renderer — group cards, bool toggles, default hints,
// reset/clear-to-default and the client-side validation that gates Save.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile, flush } from './helpers/harness.js';

const foundationSrc = srcFile('js/foundation.js');
const settingsSrc = srcFile('js/admin-settings.js');
const indexSrc = srcFile('index.html');
const _stAt = indexSrc.indexOf('<div id="admin-settings" class="sub-tab-panel">');
const PANEL = indexSrc.slice(_stAt, indexSrc.indexOf('\n\n    </div>', _stAt));

const PORT = { path: 'manager.ws_proxy_port', label: 'WS proxy port', help: 'Browser relay port.',
               group: 'network', service: 'manager', type: 'int', min: 0, max: 65535 };
const URLE = { path: 'manager.alarm_engine_url', label: 'Alarm engine URL', help: 'Where the manager finds the AE.',
               group: 'network', service: 'manager', type: 'str' };
const IDLE = { path: 'manager.poll_interval', label: 'Idle poll interval (s)', help: 'Dashboard cadence.',
               group: 'network', service: 'manager', type: 'int', min: 5, max: 3600, hot: true, common: true };
const SCHED = { path: 'manager.backup.enabled', label: 'Scheduled backups',
                help: 'Export an archive on a schedule. Archives land in data/backups/.',
                group: 'backup', service: 'manager', type: 'bool', common: true };
const LEVEL = { path: 'logging.level', label: 'Log level', help: 'Journal verbosity.',
                group: 'backup', service: 'both', type: 'choice', choices: ['INFO', 'DEBUG'] };
const SECRET = { path: 'manager.backup.passphrase', label: 'Backup passphrase', help: '12+ chars.',
                 group: 'backup', service: 'manager', type: 'str', secret: true };

function payload(over = {}) {
  return {
    ok: true,
    groups: [{ key: 'network', title: 'Network & TLS' }, { key: 'backup', title: 'Backups' }],
    entries: [PORT, URLE, IDLE, SCHED, LEVEL, SECRET],
    values: { 'manager.ws_proxy_port': 5001, 'manager.alarm_engine_url': 'https://ae:8081',
              'manager.poll_interval': 30, 'manager.backup.enabled': true, 'logging.level': 'INFO' },
    defaults: { 'manager.ws_proxy_port': 5001, 'manager.alarm_engine_url': 'http://127.0.0.1:8081',
                'manager.poll_interval': 30, 'manager.backup.enabled': false, 'logging.level': 'INFO' },
    secrets: { 'manager.backup.passphrase': 'set' },
    drift: {}, restart_pending: [], ae_sync_pending: [], ae_sync_retry_s: 30,
    topology: { split: false, ae_config_reachable: true },
    ...over,
  };
}

async function boot(data, opts) {
  const dom = new JSDOM(
    `<!doctype html><html><body><div id="adminTab">${PANEL}</div></body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  dom.window.__puts = [];
  dom.window.__posts = [];
  dom.window.fetch = (url, fetchOpts) => {
    if (url === '/api/tower/state') {
      dom.window.__towerReads = (dom.window.__towerReads || 0) + 1;
      const st = (opts && opts.tower) || { ok: true, enabled: false };
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(st))) });
    }
    if (url === '/api/tower/check') {
      dom.window.__posts.push(url);
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, check: (opts && opts.towerCheck) || null }) });
    }
    if (url === '/api/tower/eval' || url === '/api/tower/models' || url === '/api/tower/models/get') {
      if (fetchOpts && fetchOpts.method === 'POST') {
        dom.window.__posts.push(url + ' ' + (fetchOpts.body || ''));
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, job: { id: 'j1', kind: 'tower_eval', status: 'queued' } }) });
      }
      const body = url === '/api/tower/eval' ? ((opts && opts.towerEval) || { ok: true, results: [], live: null, model: null, admin: true })
        : ((opts && opts.towerModels) || { ok: true, models: [], host: '', live: null, last: null, admin: true });
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(body))) });
    }
    if (fetchOpts && fetchOpts.method === 'PUT') {
      dom.window.__puts.push(JSON.parse(fetchOpts.body));
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, applied: [] }) });
    }
    if (url === '/api/gateway/v1/models') {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ data: (opts && opts.gatewayModels) || [] }) });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });
  };
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.body.appendChild(s);
  };
  inject(`if (!window.CSS) window.CSS = { escape: s => String(s).replace(/([^a-zA-Z0-9_-])/g, '\\\\$1') };`);
  inject(foundationSrc);
  inject(srcFile('js/lib/tower-view.js'));
  inject(settingsSrc);
  await dom.window.adminSettingsLoad();
  return dom.window;
}

const cssq = s => String(s).replace(/[^a-zA-Z0-9_-]/g, ch => '\\' + ch);
const field = (doc, path) => doc.querySelector(`.settings-row[data-path="${cssq(path)}"]`);
const input = (doc, path) => doc.querySelector(`.st-input[data-path="${cssq(path)}"]`);

function type(win, path, value) {
  const el = input(win.document, path);
  el.value = value;
  el.dispatchEvent(new win.Event('input', { bubbles: true }));
  return el;
}

describe('rail + pane (#945)', () => {
  const navBtns = doc => [...doc.querySelectorAll('#stNav button')];
  test('rail lists Most used first then groups alphabetically; Most used starts active', async () => {
    const doc = (await boot(payload())).document;
    expect(navBtns(doc).map(b => b.dataset.group)).toEqual(['__most_used__', 'backup', 'network']);
    expect(navBtns(doc)[0].classList.contains('on')).toBe(true);
    expect([...doc.querySelectorAll('#stNavSel option')].map(o => o.value)).toEqual(['__most_used__', 'backup', 'network']);
  });

  test('only the active group renders as a pane card', async () => {
    const doc = (await boot(payload())).document;
    const cards = [...doc.querySelectorAll('#adminSettingsRoot .card')];
    expect(cards).toHaveLength(1);
    expect(cards[0].dataset.group).toBe('__most_used__');
    expect(cards[0].querySelector('h3').textContent).toBe('Most used');
    expect(doc.querySelectorAll('.settings-row')).toHaveLength(2);
  });

  test('clicking a rail entry switches the pane and keeps unsaved edits', async () => {
    const win = await boot(payload());
    type(win, 'manager.poll_interval', '45');
    navBtns(win.document)[2].click();
    const card = win.document.querySelector('#adminSettingsRoot .card');
    expect(card.dataset.group).toBe('network');
    expect(card.querySelector('.card-h .meta').textContent).toContain('3 settings');
    expect(win.document.querySelector('.st-input[data-path="manager.poll_interval"]').value).toBe('45');
    expect(navBtns(win.document)[2].classList.contains('on')).toBe(true);
  });

  test('the rail shows a dirty count per group and a flag for restart-pending groups', async () => {
    const win = await boot(payload({ restart_pending: ['manager'], restart_pending_paths: ['manager.ws_proxy_port'] }));
    type(win, 'manager.poll_interval', '45');
    const btns = navBtns(win.document);
    expect(btns[2].querySelector('.cnt').textContent).toBe('1');
    expect(btns[0].querySelector('.cnt').textContent).toBe('1');
    expect(btns[2].querySelector('.flag').textContent).toBe('!');
    expect(btns[1].querySelector('.flag')).toBeNull();
  });

  test('adminSettingsOpenGroup(key) selects that group', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('backup');
    expect(win.document.querySelector('#adminSettingsRoot .card').dataset.group).toBe('backup');
  });

  test('the filter shows matching rows from every group under group headings and dims empty rail entries', async () => {
    const win = await boot(payload());
    const f = win.document.getElementById('stFilter');
    f.value = 'passphrase';
    f.dispatchEvent(new win.Event('input', { bubbles: true }));
    const cards = [...win.document.querySelectorAll('#adminSettingsRoot .card')];
    expect(cards).toHaveLength(1);
    expect(cards[0].dataset.group).toBe('backup');
    expect(win.document.querySelectorAll('.settings-row')).toHaveLength(1);
    expect(navBtns(win.document)[2].classList.contains('dim')).toBe(true);
    f.value = '';
    f.dispatchEvent(new win.Event('input', { bubbles: true }));
    expect(win.document.querySelector('#adminSettingsRoot .card').dataset.group).toBe('__most_used__');
  });

  test('the mobile select mirrors the rail and switches the pane', async () => {
    const win = await boot(payload());
    const sel = win.document.getElementById('stNavSel');
    sel.value = 'network';
    sel.dispatchEvent(new win.Event('change', { bubbles: true }));
    expect(win.document.querySelector('#adminSettingsRoot .card').dataset.group).toBe('network');
  });

  test('the header summary counts settings, groups, unsaved and invalid', async () => {
    const win = await boot(payload());
    expect(win.document.getElementById('stSummary').textContent)
      .toBe('6 settings2 groups0 unsaved0 invalid');
  });
});

describe('group normalization (#945 fix round 1)', () => {
  test('with no common entries, the rail and pane agree on the first real group', async () => {
    const entries = [PORT, URLE, { ...IDLE, common: false }, { ...SCHED, common: false }, LEVEL, SECRET];
    const win = await boot(payload({ entries }));
    const card = win.document.querySelector('#adminSettingsRoot .card');
    expect(card.dataset.group).toBe('backup');
    const on = [...win.document.querySelectorAll('#stNav button')].find(b => b.classList.contains('on'));
    expect(on.dataset.group).toBe('backup');
  });

  test('a synchronous openGroup call while the first load is in flight is not clobbered', async () => {
    const dom = new JSDOM(
      `<!doctype html><html><body><div id="adminTab">${PANEL}</div></body></html>`,
      { runScripts: 'dangerously', url: 'http://localhost/' });
    dom.window.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload()) });
    const inject = (code) => {
      const s = dom.window.document.createElement('script');
      s.textContent = code;
      dom.window.document.body.appendChild(s);
    };
    inject(`if (!window.CSS) window.CSS = { escape: s => String(s).replace(/([^a-zA-Z0-9_-])/g, '\\\\$1') };`);
    inject(foundationSrc);
    inject(settingsSrc);
    const loadPromise = dom.window.adminSettingsLoad(); // not awaited: load is still in flight
    dom.window.adminSettingsOpenGroup('network'); // synchronous deep link, races the fetch
    await loadPromise;
    const card = dom.window.document.querySelector('#adminSettingsRoot .card');
    expect(card.dataset.group).toBe('network');
    const on = [...dom.window.document.querySelectorAll('#stNav button')].find(b => b.classList.contains('on'));
    expect(on.dataset.group).toBe('network');
  });
});

describe('Most used card (#801)', () => {
  test('a common setting renders once, in the Most used pane by default', async () => {
    const doc = (await boot(payload())).document;
    expect(doc.querySelectorAll('.settings-row[data-path="manager.poll_interval"]')).toHaveLength(1);
    expect(doc.querySelectorAll('.st-input[data-path="manager.poll_interval"]')).toHaveLength(1);
  });

  test('editing a common field in Most used keeps the edit when the pane switches to its own group', async () => {
    const win = await boot(payload());
    type(win, 'manager.poll_interval', '45');
    win.adminSettingsOpenGroup('network');
    const row = field(win.document, 'manager.poll_interval');
    const el = input(win.document, 'manager.poll_interval');
    expect(row.classList.contains('dirty')).toBe(true);
    expect(el.value).toBe('45');
    expect(el.getAttribute('value')).not.toBe('45'); // set by property, not via HTML string
  });

  test('toggling a common bool in Most used keeps its state when the pane switches to its own group', async () => {
    const win = await boot(payload());
    const tg = win.document.querySelector('.mc-toggle[data-path="manager.backup.enabled"]');
    tg.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    expect(tg.classList.contains('on')).toBe(false);
    win.adminSettingsOpenGroup('backup');
    const tg2 = win.document.querySelector('.mc-toggle[data-path="manager.backup.enabled"]');
    expect(tg2.classList.contains('on')).toBe(false);
    expect(tg2.getAttribute('aria-pressed')).toBe('false');
  });

  test('save sends a changed common path once', async () => {
    const win = await boot(payload());
    type(win, 'manager.poll_interval', '45');
    win.document.getElementById('adminSettingsSaveBtn').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts[0].changes).toEqual({ 'manager.poll_interval': 45 });
  });
});

describe('controls by type', () => {
  test('a bool renders an mc-toggle, never a checkbox', async () => {
    const doc = (await boot(payload())).document;
    const row = field(doc, 'manager.backup.enabled');
    const tg = row.querySelector('.mc-toggle');
    expect(tg).toBeTruthy();
    expect(tg.classList.contains('on')).toBe(true);
    expect(tg.querySelector('.tlbl').textContent).toBe('On');
    expect(doc.querySelector('#adminSettingsRoot input[type="checkbox"]')).toBeNull();
  });

  test('an int strips its unit from the label and shows it beside the input', async () => {
    const doc = (await boot(payload())).document;
    const row = field(doc, 'manager.poll_interval');
    expect(row.querySelector('label').textContent.trim()).toBe('Idle poll interval');
    expect(row.querySelector('.unit').textContent).toBe('s');
  });

  test('a choice renders a select and a shared key carries the both-hosts tag', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('backup');
    const row = field(win.document, 'logging.level');
    expect(row.querySelector('select.sel')).toBeTruthy();
    expect(row.querySelector('.tag.both')).toBeTruthy();
  });

  test('toggling a bool marks the field dirty and queues the new value', async () => {
    const win = await boot(payload());
    const tg = win.document.querySelector('.mc-toggle[data-path="manager.backup.enabled"]');
    tg.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    expect(tg.classList.contains('on')).toBe(false);
    expect(field(win.document, 'manager.backup.enabled').classList.contains('dirty')).toBe(true);
    win.document.getElementById('adminSettingsSaveBtn').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts[0].changes).toEqual({ 'manager.backup.enabled': false });
  });
});

describe('defaults', () => {
  test('a value equal to its default shows no hint and no reset button', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    const row = field(win.document, 'manager.ws_proxy_port');
    expect(row.querySelector('.dflt').textContent).toBe('');
    expect(row.querySelector('[data-reset]')).toBeNull();
  });

  test('a value differing from its default shows the hint and the reset button', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    const row = field(win.document, 'manager.alarm_engine_url');
    expect(row.querySelector('.dflt').textContent).toBe('default http://127.0.0.1:8081');
    expect(row.querySelector('[data-reset]')).toBeTruthy();
  });

  test('clearing a non-secret input reads "cleared → default" and submits null', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    type(win, 'manager.alarm_engine_url', '');
    const row = field(win.document, 'manager.alarm_engine_url');
    expect(row.querySelector('.dflt').textContent).toBe('cleared → default http://127.0.0.1:8081');
    expect(row.classList.contains('dirty')).toBe(true);
    win.document.getElementById('adminSettingsSaveBtn').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts[0].changes).toEqual({ 'manager.alarm_engine_url': null });
  });

  test('the reset button queues the same null clear', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    win.document.querySelector('[data-reset="manager.alarm_engine_url"]')
      .dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    win.document.getElementById('adminSettingsSaveBtn').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts[0].changes).toEqual({ 'manager.alarm_engine_url': null });
  });
});

describe('client-side validation', () => {
  test('an out-of-range int marks the field invalid and disables Save', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    type(win, 'manager.ws_proxy_port', '70000');
    const row = field(win.document, 'manager.ws_proxy_port');
    expect(row.classList.contains('invalid')).toBe(true);
    expect(row.querySelector('.err').textContent).toBe('Must be a whole number from 0 to 65535.');
    expect(win.document.getElementById('adminSettingsSaveBtn').disabled).toBe(true);
    expect(win.document.getElementById('stSummary').textContent).toContain('1 invalid');
  });

  test('a non-numeric int is rejected too, and fixing it re-enables Save', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    type(win, 'manager.ws_proxy_port', 'abc');
    expect(win.document.getElementById('adminSettingsSaveBtn').disabled).toBe(true);
    type(win, 'manager.ws_proxy_port', '5002');
    expect(field(win.document, 'manager.ws_proxy_port').classList.contains('invalid')).toBe(false);
    expect(win.document.getElementById('adminSettingsSaveBtn').disabled).toBe(false);
  });

  test('Save is a no-op while any field is invalid', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    type(win, 'manager.ws_proxy_port', '70000');
    win.document.getElementById('adminSettingsSaveBtn').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts).toHaveLength(0);
  });

  test('the save bar names which keys need a restart and which apply live', async () => {
    const win = await boot(payload());
    type(win, 'manager.poll_interval', '45');
    win.adminSettingsOpenGroup('network');
    type(win, 'manager.alarm_engine_url', 'http://ae:8081');
    const note = win.document.querySelector('#adminSettingsSaveBar .note').textContent;
    expect(note).toContain('Idle poll interval (s) applies without restart');
    expect(note).toContain('Alarm engine URL needs a');
    expect(note).toContain('manager restart');
  });

  test('Discard drops every pending edit', async () => {
    const win = await boot(payload());
    type(win, 'manager.poll_interval', '45');
    expect(win.document.getElementById('adminSettingsSaveBar')).toBeTruthy();
    win.document.getElementById('adminSettingsDiscardBtn')
      .dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
    expect(win.document.getElementById('adminSettingsSaveBar')).toBeNull();
  });
});

describe('SettingsFields export', () => {
  test('renders a standalone st-rows from caller-supplied entries and defaults', async () => {
    const win = await boot(payload());
    const html = win.SettingsFields.render([SCHED, PORT],
      { 'manager.backup.enabled': true, 'manager.ws_proxy_port': 9999 },
      { 'manager.backup.enabled': false, 'manager.ws_proxy_port': 5001 });
    expect(html).toContain('st-rows');
    expect(html).toContain('mc-toggle');
    expect(html).toContain('default <b>5001</b>');
  });
});

describe('review fixes (#797)', () => {
  test('clearing a field that already sits at its default is not a change', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    type(win, 'manager.ws_proxy_port', '');
    expect(win.document.getElementById('adminSettingsSaveBar')).toBeNull();
    expect(field(win.document, 'manager.ws_proxy_port').classList.contains('dirty')).toBe(false);
  });
});

describe('rail layout (#945)', () => {
  test('the panel has a nav rail, a mobile select and no Expand all button', () => {
    expect(PANEL).toContain('id="stNav"');
    expect(PANEL).toContain('id="stNavSel"');
    expect(PANEL).not.toContain('id="stExpandAll"');
  });

  test('a row is name | help | control with the dotted key under the name', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    const row = field(win.document, 'manager.ws_proxy_port');
    expect(row.classList.contains('st-fld')).toBe(true);
    expect(row.children[0].className).toBe('st-lb');
    expect(row.children[0].querySelector('.key').textContent).toBe('manager.ws_proxy_port');
    expect(row.children[1].className).toBe('help');
    expect(row.children[2].className).toBe('st-ct');
    expect(row.querySelector('.st-ct .st-input')).not.toBeNull();
  });

  test('a bool row shows the full help in the help column and On/Off on the toggle', async () => {
    const doc = (await boot(payload())).document;
    const row = field(doc, 'manager.backup.enabled');
    expect(row.querySelector('.help').textContent).toBe(SCHED.help);
    expect(row.querySelector('.mc-toggle .tlbl').textContent).toBe('On');
  });

  test('a validation error renders inside the control column', async () => {
    const win = await boot(payload());
    win.adminSettingsOpenGroup('network');
    type(win, 'manager.ws_proxy_port', '99999');
    const row = field(win.document, 'manager.ws_proxy_port');
    expect(row.querySelector('.st-ct .err').textContent).toMatch(/0 to 65535/);
  });
});

describe('rail selection review fixes (#945)', () => {
  const navBtns = doc => [...doc.querySelectorAll('#stNav button')];

  test('a drift entry (not just restart-pending) also sets the rail flag', async () => {
    const win = await boot(payload({
      drift: { 'manager.backup.passphrase': { local: 'a', ae: 'b', secret: true } },
    }));
    const backup = navBtns(win.document).find(b => b.dataset.group === 'backup');
    expect(backup.querySelector('.flag')).toBeTruthy();
  });

  test('clicking a rail entry clears an active filter', async () => {
    const win = await boot(payload());
    const f = win.document.getElementById('stFilter');
    f.value = 'passphrase';
    f.dispatchEvent(new win.Event('input', { bubbles: true }));
    navBtns(win.document).find(b => b.dataset.group === 'network').click();
    expect(f.value).toBe('');
    expect(win.document.querySelector('#adminSettingsRoot .card').dataset.group).toBe('network');
  });

  test('a deep link wins over an active filter', async () => {
    const win = await boot(payload());
    const f = win.document.getElementById('stFilter');
    f.value = 'passphrase';
    f.dispatchEvent(new win.Event('input', { bubbles: true }));
    win.adminSettingsOpenGroup('network');
    const cards = [...win.document.querySelectorAll('#adminSettingsRoot .card')];
    expect(cards).toHaveLength(1);
    expect(cards[0].dataset.group).toBe('network');
    expect(f.value).toBe('');
    const on = navBtns(win.document).find(b => b.classList.contains('on'));
    expect(on.dataset.group).toBe('network');
  });

  test('Most used is dimmed while a filter is active', async () => {
    const win = await boot(payload());
    const f = win.document.getElementById('stFilter');
    f.value = 'passphrase';
    f.dispatchEvent(new win.Event('input', { bubbles: true }));
    const mostUsed = navBtns(win.document).find(b => b.dataset.group === '__most_used__');
    expect(mostUsed.classList.contains('dim')).toBe(true);
  });
});

describe('gateway model picker (#924)', () => {
  const MODEL = { path: 'manager.tower.model', label: 'Primary model', help: 'Pinned model.', group: 'network', service: 'manager', type: 'str', hot: true, datalist: 'gateway_models' };
  test('renders a select of auto plus the gateway models, current value selected', async () => {
    const win = await boot(payload({ entries: [PORT, MODEL], values: { 'manager.ws_proxy_port': 5001, 'manager.tower.model': 'qwen3-14b' } }),
      { gatewayModels: [{ id: 'qwen3-14b', provider: 'llama' }, { id: 'gemma-4', provider: 'lms' }] });
    const sel = input(win.document, 'manager.tower.model');
    expect(sel.tagName).toBe('SELECT');
    expect([...sel.options].map(o => o.value)).toEqual(['auto', 'qwen3-14b', 'gemma-4']);
    expect(sel.value).toBe('qwen3-14b');
    expect(win.document.querySelector('#stGatewayModels')).toBeNull();
  });
  test('a blank or unknown stored value still shows: blank as auto, unknown appended', async () => {
    let win = await boot(payload({ entries: [PORT, MODEL], values: { 'manager.ws_proxy_port': 5001, 'manager.tower.model': '' } }), { gatewayModels: [{ id: 'qwen3-14b' }] });
    expect(input(win.document, 'manager.tower.model').value).toBe('auto');
    win = await boot(payload({ entries: [PORT, MODEL], values: { 'manager.ws_proxy_port': 5001, 'manager.tower.model': 'gone-model' } }), { gatewayModels: [{ id: 'qwen3-14b' }] });
    const sel = input(win.document, 'manager.tower.model');
    expect([...sel.options].map(o => o.value)).toEqual(['auto', 'qwen3-14b', 'gone-model']);
    expect(sel.value).toBe('gone-model');
  });
  test('picking a model queues it for Save', async () => {
    const win = await boot(payload({ entries: [PORT, MODEL], values: { 'manager.ws_proxy_port': 5001, 'manager.tower.model': 'auto' } }), { gatewayModels: [{ id: 'qwen3-14b' }] });
    const sel = input(win.document, 'manager.tower.model');
    sel.value = 'qwen3-14b'; sel.dispatchEvent(new win.Event('change', { bubbles: true }));
    win.document.getElementById('adminSettingsSaveBtn').click();
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts[0].changes['manager.tower.model']).toBe('qwen3-14b');
  });
});


describe('choice labels (#924)', () => {
  const MODE = { path: 'manager.tower.mode', label: 'Answer mode', help: 'How Tower answers.', group: 'network',
                 service: 'manager', type: 'choice', choices: ['balanced', 'fast'],
                 labels: { balanced: 'Balanced — quality first', fast: 'Fast — short answers' } };
  const data = () => payload({ entries: [PORT, MODE],
    values: { 'manager.ws_proxy_port': 5001, 'manager.tower.mode': 'fast' },
    defaults: { 'manager.ws_proxy_port': 5001, 'manager.tower.mode': 'balanced' } });

  test('options show labels with raw values and the default hint shows the label', async () => {
    const win = await boot(data());
    const sel = input(win.document, 'manager.tower.mode');
    expect([...sel.options].map(o => o.value)).toEqual(['balanced', 'fast']);
    expect([...sel.options].map(o => o.textContent)).toEqual(['Balanced — quality first', 'Fast — short answers']);
    expect(sel.value).toBe('fast');
    expect(field(win.document, 'manager.tower.mode').querySelector('[data-dflt]').textContent)
      .toBe('default Balanced — quality first');
  });

  test('an entry without labels still renders raw choice text', async () => {
    const bare = Object.assign({}, MODE, { labels: undefined });
    const win = await boot(payload({ entries: [PORT, bare], values: { 'manager.tower.mode': 'fast' } }));
    const sel = input(win.document, 'manager.tower.mode');
    expect([...sel.options].map(o => o.textContent)).toEqual(['balanced', 'fast']);
  });
});

describe('tools setting (#924)', () => {
  const TOOLS = { path: 'manager.tower.disabled_tools', label: 'Available tools', help: 'Tools Tower may use.', group: 'network',
                  service: 'manager', type: 'chips', hot: true, choices: ['alarms', 'help', 'log_tail'], exclude: true,
                  groups: { 'Read tools': ['alarms', 'help'], Actions: ['log_tail'] } };
  const data = (over = {}) => payload({ entries: [PORT, Object.assign({}, TOOLS, over)],
    values: { 'manager.ws_proxy_port': 5001, 'manager.tower.disabled_tools': ['log_tail'] },
    defaults: { 'manager.ws_proxy_port': 5001, 'manager.tower.disabled_tools': [] } });

  const box = doc => doc.querySelector('.st-tools[data-path="manager.tower.disabled_tools"]');
  const sum = doc => box(doc).querySelector('.st-tools-sum').textContent;
  const grps = doc => [...box(doc).querySelectorAll('.st-tools-grp')];
  const edit = doc => box(doc).querySelector('[data-tools-edit]');
  const chip = (doc, name) => box(doc).querySelector(`.st-tool-chip[data-tool="${name}"]`);

  test('collapses to Edit then a one-line summary, with no chips', async () => {
    const win = await boot(data());
    expect(box(win.document).children[0]).toBe(edit(win.document));
    expect(box(win.document).children[1].className).toContain('st-tools-sum');
    expect(sum(win.document)).toBe('2 of 3 tools on · off: log_tail');
    expect(box(win.document).querySelectorAll('.st-tool-chip')).toHaveLength(0);
    expect(edit(win.document).textContent).toBe('Edit');
    expect(field(win.document, 'manager.tower.disabled_tools').querySelector('[data-dflt]').textContent).toBe('default all');
  });

  test('Edit expands grouped chips with per-group counts, Done collapses again', async () => {
    const win = await boot(data());
    edit(win.document).click();
    expect(box(win.document).classList.contains('open')).toBe(true);
    expect(edit(win.document).textContent).toBe('Done');
    const g = grps(win.document);
    expect(g).toHaveLength(2);
    expect(g.map(s => s.querySelector('h5').firstChild.textContent)).toEqual(['Read tools', 'Actions']);
    expect(g.map(s => s.querySelector('.cnt').textContent)).toEqual(['2/2', '0/1']);
    expect(g.map(s => [...s.querySelectorAll('.st-tool-chip')].map(c => c.dataset.tool)))
      .toEqual([['alarms', 'help'], ['log_tail']]);
    expect(g[0].querySelectorAll('.st-tool-chip')[1].classList.contains('on')).toBe(true);
    expect(g[0].querySelectorAll('.st-tool-chip')[1].getAttribute('aria-pressed')).toBe('true');
    expect(g[1].querySelectorAll('.st-tool-chip')[0].classList.contains('on')).toBe(false);
    expect(g[1].querySelectorAll('.st-tool-chip')[0].getAttribute('aria-pressed')).toBe('false');
    expect(box(win.document).querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
    edit(win.document).click();
    expect(box(win.document).classList.contains('open')).toBe(false);
    expect(box(win.document).querySelectorAll('.st-tools-grp')).toHaveLength(0);
    expect(edit(win.document).textContent).toBe('Edit');
  });

  test('clicking a chip toggles it, updates the summary, and Save sends the excluded list', async () => {
    const win = await boot(data());
    edit(win.document).click();
    chip(win.document, 'help').click();
    expect(chip(win.document, 'help').classList.contains('on')).toBe(false);
    expect(chip(win.document, 'help').getAttribute('aria-pressed')).toBe('false');
    expect(sum(win.document)).toBe('1 of 3 tools on · off: help, log_tail');
    expect(box(win.document).classList.contains('dirty')).toBe(true);
    expect(grps(win.document)[0].querySelector('.cnt').textContent).toBe('1/2');
    win.document.getElementById('adminSettingsSaveBtn').click();
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts[0].changes['manager.tower.disabled_tools']).toEqual(['help', 'log_tail']);
  });

  test('All on / All off apply only to their own group', async () => {
    const win = await boot(data());
    edit(win.document).click();
    chip(win.document, 'help').click();
    grps(win.document)[1].querySelector('[data-tools-all="on"]').click();
    expect(sum(win.document)).toBe('2 of 3 tools on · off: help');
    expect(grps(win.document).map(s => s.querySelector('.cnt').textContent)).toEqual(['1/2', '1/1']);
    grps(win.document)[0].querySelector('[data-tools-all="off"]').click();
    expect(sum(win.document)).toBe('1 of 3 tools on · off: alarms, help');
    expect(grps(win.document).map(s => s.querySelector('.cnt').textContent)).toEqual(['0/2', '1/1']);
    win.document.getElementById('adminSettingsSaveBtn').click();
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts[0].changes['manager.tower.disabled_tools']).toEqual(['alarms', 'help']);
  });

  test('without groups every choice lands in a single "Tools" group', async () => {
    const win = await boot(data({ groups: undefined }));
    edit(win.document).click();
    const g = grps(win.document);
    expect(g).toHaveLength(1);
    expect(g[0].querySelector('h5').firstChild.textContent).toBe('Tools');
    expect([...g[0].querySelectorAll('.st-tool-chip')].map(c => c.dataset.tool)).toEqual(['alarms', 'help', 'log_tail']);
  });

  test('a choice missing from every group still renders, under "Other"', async () => {
    const win = await boot(data({ groups: { 'Read tools': ['alarms', 'help'] } }));
    edit(win.document).click();
    const g = grps(win.document);
    expect(g.map(s => s.querySelector('h5').firstChild.textContent)).toEqual(['Read tools', 'Other']);
    expect([...g[1].querySelectorAll('.st-tool-chip')].map(c => c.dataset.tool)).toEqual(['log_tail']);
  });

  test('the expanded state survives a re-render', async () => {
    const win = await boot(data());
    edit(win.document).click();
    win.adminSettingsOpenGroup('network');
    expect(box(win.document).classList.contains('open')).toBe(true);
    expect(box(win.document).querySelectorAll('.st-tool-chip')).toHaveLength(3);
  });
});

describe('Tower model check row (#1039)', () => {
  const TOWER = { path: 'manager.tower.enabled', label: 'Tower', help: 'Assistant drawer.',
                  group: 'tower', service: 'manager', type: 'bool', hot: true };
  const towerPayload = () => payload({
    groups: [{ key: 'network', title: 'Network & TLS' }, { key: 'tower', title: 'Tower' }],
    entries: [PORT, IDLE, TOWER],
    values: { 'manager.ws_proxy_port': 5001, 'manager.poll_interval': 30, 'manager.tower.enabled': true },
    defaults: { 'manager.ws_proxy_port': 5001, 'manager.poll_interval': 30, 'manager.tower.enabled': false },
    secrets: {},
  });
  const STATE = { ok: true, enabled: true, admin: true, model: 'qwen3-14b',
                  check: { model: 'qwen3-14b', grade: 'fenced', size_b: 4, small: true, at: Date.now() / 1000 } };

  async function towerCard(opts) {
    const win = await boot(towerPayload(), opts);
    win.adminSettingsOpenGroup('tower');
    await flush();
    return win;
  }

  const TOOLMODE = { path: 'manager.tower.tool_mode', label: 'Tool calls', help: 'How.', group: 'tower', service: 'manager', type: 'choice', choices: ['auto', 'native', 'prompt'], hot: true };
  const PRIMARY = { path: 'manager.tower.model', label: 'Primary model', help: 'Which.', group: 'tower', service: 'manager', type: 'str', hot: true };
  const CAP = { path: 'manager.tower.max_tool_calls', label: 'Tool calls per question', help: 'Cap.', group: 'tower', service: 'manager', type: 'int', hot: true };

  test('the row sits after Tool calls, grades the primary as chips with the model in the tip, and offers Verify to an admin', async () => {
    const win = await boot(payload({
      groups: [{ key: 'tower', title: 'Tower' }], entries: [TOWER, TOOLMODE, PRIMARY, CAP],
      values: { 'manager.tower.enabled': true, 'manager.tower.tool_mode': 'auto', 'manager.tower.model': 'auto', 'manager.tower.max_tool_calls': 16 },
      defaults: { 'manager.tower.enabled': false, 'manager.tower.tool_mode': 'auto', 'manager.tower.model': 'auto', 'manager.tower.max_tool_calls': 16 }, secrets: {} }),
      { tower: STATE });
    win.adminSettingsOpenGroup('tower');
    await flush();
    const rows = [...win.document.querySelectorAll('.st-rows > .settings-row')].map(r => r.id || r.dataset.path);
    expect(rows).toEqual(['manager.tower.enabled', 'manager.tower.tool_mode', 'stTowerCheck', 'manager.tower.model', 'stTowerEval', 'stTowerGet', 'manager.tower.max_tool_calls']);
    const row = win.document.getElementById('stTowerCheck');
    expect(row.querySelector('.st-lb label').textContent).toBe('Tool response check');
    expect(row.querySelector('.key')).toBeNull();
    const primary = row.querySelector('.st-ct .row');
    expect(primary.textContent).toContain('Primary');
    const ok = primary.querySelector('.st-chip.ok');
    expect(ok.textContent).toBe('Tools OK');
    expect(ok.classList.contains('outline')).toBe(true);
    expect(ok.getAttribute('data-tip')).toBe('qwen3-14b · Tool calls work: the model writes them in text prompt mode');
    const small = primary.querySelector('.st-chip.warn');
    expect(small.textContent).toBe('small model');
    expect(small.getAttribute('data-tip')).toContain('Small model');
    expect(row.textContent).not.toContain('4B');
    const btn = row.querySelector('.st-lb #stTowerCheckBtn');
    expect(btn.textContent).toBe('Verify');
    expect(btn.classList.contains('mcbtn')).toBe(true);
    const fb = row.querySelector('.row.fb');
    expect(fb.textContent).toContain('Fallback');
    expect(fb.querySelector('.st-chip.dim').textContent).toBe('Off');
  });

  test('the fallback line shows None when the toggle is on without a second model, and its chips when one is resident', async () => {
    const none = await towerCard({ tower: { ...STATE, fallback_enabled: true } });
    expect(none.document.querySelector('#stTowerCheck .row.fb .st-chip.dim').textContent).toBe('None');
    const win = await towerCard({ tower: { ...STATE, fallback_enabled: true,
                                            fallback: { model: 'gemma-3-12b', check: { grade: 'failed', detail: 'no call' } } } });
    const fb = win.document.querySelector('#stTowerCheck .row.fb');
    expect(fb.textContent).not.toContain('gemma-3-12b');
    const chip = fb.querySelector('.st-chip.crit');
    expect(chip.textContent).toBe('No tool support');
    expect(chip.getAttribute('data-tip')).toBe('gemma-3-12b · No tool support: the model made no tool call in either mode (no call)');
  });

  test('Verify posts to /api/tower/check and repaints with the new grade', async () => {
    const win = await towerCard({ tower: STATE, towerCheck: { model: 'qwen3-14b', grade: 'native', size_b: 14, small: false } });
    win.document.getElementById('stTowerCheckBtn').click();
    await flush();
    expect(win.__posts).toEqual(['/api/tower/check']);
    const chip = win.document.querySelector('#stTowerCheck .st-ct .st-chip');
    expect(chip.textContent).toBe('Tools OK');
    expect(chip.classList.contains('ok')).toBe(true);
    expect(chip.classList.contains('outline')).toBe(false);
    expect(win.document.querySelector('#stTowerCheck .st-ct .st-chip.warn')).toBeNull();
  });

  const EVAL = { id: 'e1', model: 'qwen3-14b', provider: 'llama', quant: 'Q4_K_M', server: 'llama.cpp b6400', at: Date.now() / 1000 - 120,
                 ms: 48000, passed: 7, total: 8, calls: 11, calls_per_case: 1.4, corrections: 1, retries: 0, score_pct: 87.5,
                 cases: [{ id: 'hosts', title: 'Plain read', passed: true, calls: 1, ms: 900, detail: 'ok' },
                         { id: 'timer', title: 'Timer', passed: false, calls: 2, ms: 3000, detail: 'no tool call' }] };
  const MODELS = { ok: true, host: 'box', hosts: [{ provider: 'llama', label: 'llama.cpp', host: 'box', agent_id: 'a1', primary: true }], live: null, last: null, admin: true, models: [
    { key: 'qwen3-8b-q4', name: 'Qwen3 8B', tier_gb: 8, quant: 'Q4_K_M', size_gb: 5.0, expected: 'good', present: false, loaded: false, eval: null },
    { key: 'qwen3-14b-q4', name: 'Qwen3 14B', tier_gb: 12, quant: 'Q4_K_M', size_gb: 9.0, expected: 'high', present: true, loaded: true,
      eval: { passed: 8, total: 8 } }] };

  test('the eval row scores the primary model, lists its questions, exports, and Run eval posts (#1047)', async () => {
    const win = await towerCard({ tower: STATE, towerEval: { ok: true, results: [EVAL], live: null, model: 'qwen3-14b', admin: true }, towerModels: MODELS });
    const row = win.document.getElementById('stTowerEval');
    expect(row.querySelector('.st-lb label').textContent).toBe('Model evaluation');
    const chip = row.querySelector('.st-ct .row.ev .st-chip');
    expect(chip.textContent).toBe('7/8');
    expect(chip.classList.contains('warn')).toBe(true);
    expect(chip.getAttribute('data-tip')).toBe('7/8 passed · qwen3-14b · Q4_K_M · llama.cpp b6400 · 2 min ago');
    expect(row.querySelector('.row.ev .d.line').textContent).toBe('1.4 calls per question · 1 corrected · 48 s');
    expect(row.querySelector('#stTowerEvalBtn').textContent).toBe('Run');
    const cases = [...row.querySelectorAll('.st-evalcases tbody tr, .st-evalcases tr')].slice(1);
    expect(cases).toHaveLength(2);
    expect(cases[1].querySelector('td.bad').textContent).toBe('✗');
    expect(cases[1].querySelector('td.det').textContent).toBe('no tool call');
    expect(row.querySelector('.st-lb a[download]').getAttribute('href')).toBe('/api/tower/eval/e1?export=1');
    win.document.getElementById('stTowerEvalBtn').click();
    await flush();
    expect(win.__posts).toEqual(['/api/tower/eval {}']);
  });

  test('another model\'s result carries its name in the line, not the lead column (#1047)', async () => {
    const other = { ...EVAL, id: 'e2', model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', passed: 8, score_pct: 100 };
    const win = await towerCard({ tower: STATE, towerEval: { ok: true, results: [EVAL, other], live: null, model: 'qwen3-14b', admin: true }, towerModels: MODELS });
    const rows = [...win.document.querySelectorAll('#stTowerEval .row.ev')];
    expect(rows).toHaveLength(2);
    expect(rows[1].querySelector('.d.w').textContent).toBe('Other');
    expect(rows[1].querySelector('.d.line b').textContent).toBe('Qwen3.8-27B-GGUF:Q4_K_M');
    expect(rows[1].querySelector('.st-chip').getAttribute('data-tip')).toContain('bartowski/Qwen3.8-27B-GGUF:Q4_K_M');
    expect(rows[1].querySelector('.st-chip').textContent).toBe('8/8');
  });

  test('the Get a Tower model row groups the list by VRAM tier, shows what the host has, and Get posts the key (#1047)', async () => {
    const win = await towerCard({ tower: STATE, towerModels: MODELS });
    const row = win.document.getElementById('stTowerGet');
    expect(row.querySelector('.st-lb label').textContent).toBe('Download a Tower model');
    expect(row.querySelector('#stTowerGetBtn').textContent).toBe('Download');
    expect(row.querySelector('#stTowerGetSel').classList.contains('st-input')).toBe(true);
    const groups = [...row.querySelectorAll('optgroup')].map(g => g.label);
    expect(groups).toEqual(['8 GB VRAM', '12 GB VRAM']);
    const opts = [...row.querySelectorAll('option')].map(o => o.textContent);
    expect(opts).toEqual(['Qwen3 8B · Q4_K_M · 5 GB · expected good', 'Qwen3 14B · Q4_K_M · 9 GB · scored 8/8 · loaded']);
    expect(row.querySelector('.note').textContent).toContain('VRAM');
    expect(row.querySelector('#stTowerGetHost')).toBeNull();
    expect([...row.querySelectorAll('.row.ev')][1].textContent).toContain('box · llama.cpp · primary');
    expect(row.querySelector('.note').textContent).toContain('restarts after the download');
    win.document.getElementById('stTowerGetSel').value = 'qwen3-14b-q4';
    win.document.getElementById('stTowerGetBtn').click();
    await flush();
    expect(win.__posts).toEqual(['/api/tower/models/get {"key":"qwen3-14b-q4","agent_id":"a1"}']);
  });

  test('several hosts give a host choice, Download confirms then posts the agent id; none says so (#1047)', async () => {
    const two = { ...MODELS, hosts: [...MODELS.hosts, { provider: 'lms', label: 'LM Studio', host: 'mac', agent_id: 'a2', primary: false },
                                    { provider: 'llama', label: 'llama.cpp', host: 'box2', agent_id: 'a3', primary: false }] };
    const win = await towerCard({ tower: STATE, towerModels: two });
    const sel = win.document.getElementById('stTowerGetHost');
    expect([...sel.options].map(o => o.textContent)).toEqual(['box · llama.cpp · primary', 'mac · LM Studio', 'box2 · llama.cpp']);
    const asked = [];
    win._themedConfirm = async (o) => { asked.push(o); return o.title.includes('mac'); };
    sel.value = 'a3';
    win.document.getElementById('stTowerGetBtn').click();
    await flush();
    expect(asked[0].title).toBe('Download Qwen3 8B to box2?');
    expect(asked[0].bodyHtml).toContain('llama.cpp on box2 restarts after the download');
    expect(asked[0].danger).toBe(true);
    expect(win.__posts).toEqual([]);   // declined
    sel.value = 'a2';
    win.document.getElementById('stTowerGetBtn').click();
    await flush();
    expect(asked[1].bodyHtml).not.toContain('restarts');
    expect(asked[1].danger).toBe(false);
    expect(win.__posts).toEqual(['/api/tower/models/get {"key":"qwen3-8b-q4","agent_id":"a2"}']);
    const none = await towerCard({ tower: STATE, towerModels: { ...MODELS, hosts: [] } });
    expect(none.document.getElementById('stTowerGet').textContent).toContain('No primary llama.cpp or LM Studio host');
  });

  test('the Primary model choices follow the gateway index on each Tower refresh, keeping the field value (#1047)', async () => {
    const MODEL = { path: 'manager.tower.model', label: 'Primary model', help: 'Which.', group: 'tower', service: 'manager', type: 'str', datalist: 'gateway_models', hot: true };
    const opts = { tower: STATE, towerModels: MODELS, gatewayModels: [{ id: 'qwen3-14b' }, { id: 'gone-model' }] };
    const win = await boot(payload({ groups: [{ key: 'tower', title: 'Tower' }], entries: [TOWER, MODEL],
      values: { 'manager.tower.enabled': true, 'manager.tower.model': 'gone-model' },
      defaults: { 'manager.tower.enabled': false, 'manager.tower.model': 'auto' }, secrets: {} }), opts);
    win.adminSettingsOpenGroup('tower');
    await flush();
    const sel = () => win.document.querySelector('select.st-input[data-path="manager.tower.model"]');
    expect([...sel().options].map(o => o.value)).toEqual(['auto', 'qwen3-14b', 'gone-model']);
    opts.gatewayModels = [{ id: 'qwen3-14b' }, { id: 'fresh-model' }];
    await win.adminSettingsRefreshTower();
    await flush();
    expect([...sel().options].map(o => o.value)).toEqual(['auto', 'qwen3-14b', 'fresh-model', 'gone-model']);
    expect(sel().value).toBe('gone-model');
    sel().value = 'qwen3-14b';
    opts.gatewayModels = [{ id: 'qwen3-14b' }];
    await win.adminSettingsRefreshTower();
    await flush();
    expect([...sel().options].map(o => o.value)).toEqual(['auto', 'qwen3-14b']);
  });

  test('Pin as primary only for a model the gateway still lists and that is not the primary yet; Stop on LM Studio says so (#1047)', async () => {
    const last = { id: 'j8', kind: 'tower_get_model', status: 'done', message: 'Qwen3 8B ready · 8/8 passed',
                   result: { model: 'Qwen/Qwen3-8B-GGUF:Q4_K_M', host: 'box', pin_offer: true } };
    const gone = await towerCard({ tower: STATE, towerModels: { ...MODELS, last }, gatewayModels: [{ id: 'qwen3-14b' }] });
    expect(gone.document.getElementById('stTowerPinBtn')).toBeNull();
    expect(gone.document.getElementById('stTowerGet').textContent).toContain('no longer on the host');
    const MODEL = { path: 'manager.tower.model', label: 'Primary model', help: 'Which.', group: 'tower', service: 'manager', type: 'str', datalist: 'gateway_models', hot: true };
    const pinned = await boot(payload({ groups: [{ key: 'tower', title: 'Tower' }], entries: [TOWER, MODEL],
      values: { 'manager.tower.enabled': true, 'manager.tower.model': 'Qwen/Qwen3-8B-GGUF:Q4_K_M' },
      defaults: { 'manager.tower.enabled': false, 'manager.tower.model': 'auto' }, secrets: {} }),
      { tower: STATE, towerModels: { ...MODELS, last }, gatewayModels: [{ id: 'Qwen/Qwen3-8B-GGUF:Q4_K_M' }] });
    pinned.adminSettingsOpenGroup('tower');
    await flush();
    expect(pinned.document.getElementById('stTowerPinBtn')).toBeNull();
    expect(pinned.document.getElementById('stTowerGet').textContent).toContain('· primary');
    const stopped = { id: 'j7', kind: 'tower_get_model', status: 'cancelled', label: 'Download Tower model · Gemma 4 12B',
                      message: 'cancelled by the operator', spec: { provider: 'lms' } };
    const lms = await towerCard({ tower: STATE, towerModels: { ...MODELS, last: stopped } });
    expect(lms.document.getElementById('stTowerGet').textContent).toContain('LM Studio keeps downloading, cancel it there');
  });

  test('the Primary model choices name the server and hosts, and mark a pinned model the index lost (#1047)', async () => {
    const MODEL = { path: 'manager.tower.model', label: 'Primary model', help: 'Which.', group: 'tower', service: 'manager', type: 'str', datalist: 'gateway_models', hot: true };
    const index = [{ id: 'qwen3-14b', provider: 'llama', hosts: ['box', 'box-two'], loaded: true }, { id: 'gemma-3-12b', provider: 'lms', hosts: ['mac'], loaded: false }];
    const win = await boot(payload({ groups: [{ key: 'tower', title: 'Tower' }], entries: [TOWER, MODEL],
      values: { 'manager.tower.enabled': true, 'manager.tower.model': 'gone-model' },
      defaults: { 'manager.tower.enabled': false, 'manager.tower.model': 'auto' }, secrets: {} }),
      { tower: STATE, towerModels: { ...MODELS, index }, gatewayModels: [{ id: 'qwen3-14b' }, { id: 'gemma-3-12b' }] });
    win.adminSettingsOpenGroup('tower');
    await flush();
    const sel = win.document.querySelector('select.st-input[data-path="manager.tower.model"]');
    expect([...sel.options].map(o => o.textContent)).toEqual(['auto', 'qwen3-14b · llama.cpp · box, box-two · loaded', 'gemma-3-12b · LM Studio · mac', 'gone-model · not available']);
    expect(sel.value).toBe('gone-model');
  });

  test('Pin as primary shows the pinned model in the Primary model field without a reload (#1047)', async () => {
    const MODEL = { path: 'manager.tower.model', label: 'Primary model', help: 'Which.', group: 'tower', service: 'manager', type: 'str', datalist: 'gateway_models', hot: true };
    const last = { id: 'j8', kind: 'tower_get_model', status: 'done', message: 'Qwen3 8B ready · 8/8 passed',
                   result: { model: 'Qwen/Qwen3-8B-GGUF:Q4_K_M', host: 'box', pin_offer: true } };
    const win = await boot(payload({ groups: [{ key: 'tower', title: 'Tower' }], entries: [TOWER, MODEL],
      values: { 'manager.tower.enabled': true, 'manager.tower.model': 'auto' },
      defaults: { 'manager.tower.enabled': false, 'manager.tower.model': 'auto' }, secrets: {} }),
      { tower: STATE, towerModels: { ...MODELS, last }, gatewayModels: [{ id: 'qwen3-14b' }, { id: 'Qwen/Qwen3-8B-GGUF:Q4_K_M' }] });
    win.adminSettingsOpenGroup('tower');
    await flush();
    const sel = () => win.document.querySelector('select.st-input[data-path="manager.tower.model"]');
    sel().value = 'qwen3-14b';
    sel().dispatchEvent(new win.Event('change', { bubbles: true }));
    win.document.getElementById('stTowerPinBtn').click();
    await flush();
    expect(win.__puts).toEqual([{ model: 'Qwen/Qwen3-8B-GGUF:Q4_K_M' }]);
    expect(sel().value).toBe('Qwen/Qwen3-8B-GGUF:Q4_K_M');
    expect(sel().classList.contains('dirty')).toBe(false);
    await win.adminSettingsRefreshTower();
    await flush();
    expect(sel().value).toBe('Qwen/Qwen3-8B-GGUF:Q4_K_M');
  });

  test('a live job shows progress with Stop, a finished download offers Pin as primary, and non-admins get no buttons (#1047)', async () => {
    const live = { id: 'j9', kind: 'tower_get_model', status: 'running', label: 'Get Tower model · Qwen3 8B', can_cancel: true,
                   state: { phase: 'download', pct: 40 } };
    const win = await towerCard({ tower: STATE, towerModels: { ...MODELS, live } });
    const row = win.document.getElementById('stTowerGet');
    expect(row.textContent).toContain('Downloading · 40 %');
    expect(row.querySelector('[data-tower-cancel]').dataset.towerCancel).toBe('j9');
    expect(row.querySelector('#stTowerGetBtn').disabled).toBe(true);
    expect(win.document.querySelector('#stTowerEvalBtn').disabled).toBe(true);
    const last = { id: 'j8', kind: 'tower_get_model', status: 'done', message: 'Qwen3 8B ready · 8/8 passed',
                   result: { model: 'Qwen/Qwen3-8B-GGUF:Q4_K_M', host: 'box', pin_offer: true } };
    const done = await towerCard({ tower: STATE, towerModels: { ...MODELS, last }, gatewayModels: [{ id: 'Qwen/Qwen3-8B-GGUF:Q4_K_M' }] });
    const pin = done.document.getElementById('stTowerPinBtn');
    expect(pin.dataset.model).toBe('Qwen/Qwen3-8B-GGUF:Q4_K_M');
    expect(done.document.getElementById('stTowerGet').textContent).toContain('Qwen3 8B ready · 8/8 passed');
    pin.click();
    await flush();
    expect(done.__puts).toEqual([{ model: 'Qwen/Qwen3-8B-GGUF:Q4_K_M' }]);
    const ro = await towerCard({ tower: { ...STATE, admin: false }, towerModels: MODELS });
    expect(ro.document.getElementById('stTowerEvalBtn')).toBeNull();
    expect(ro.document.getElementById('stTowerGetBtn')).toBeNull();
    expect(ro.document.querySelector('#stTowerEval .st-chip.dim').textContent).toBe('Not run');
  });

  test('a non-admin sees the grade without the button, and a model-less Tower says so', async () => {
    const ro = await towerCard({ tower: { ...STATE, admin: false } });
    expect(ro.document.getElementById('stTowerCheckBtn')).toBeNull();
    expect(ro.document.querySelector('#stTowerCheck .st-ct .st-chip').textContent).toBe('Tools OK');
    const none = await towerCard({ tower: { ok: true, enabled: true, admin: true, model: null, check: null } });
    expect(none.document.getElementById('stTowerCheck').textContent).toContain('No model loaded');
  });

  test('re-rendering the group reuses the last state read', async () => {
    const win = await towerCard({ tower: STATE });
    expect(win.__towerReads).toBe(1);
    win.adminSettingsOpenGroup('network');
    win.adminSettingsOpenGroup('tower');
    await flush();
    expect(win.__towerReads).toBe(1);
    expect(win.document.querySelector('#stTowerCheck .st-ct .st-chip').textContent).toBe('Tools OK');
  });

  test('other groups render no check row', async () => {
    const win = await boot(towerPayload(), { tower: STATE });
    win.adminSettingsOpenGroup('network');
    await flush();
    expect(win.document.getElementById('stTowerCheck')).toBeNull();
  });
});
