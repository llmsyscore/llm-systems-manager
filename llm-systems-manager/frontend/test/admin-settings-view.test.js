// #797: Settings sub-tab renderer — group cards, bool toggles, default hints,
// reset/clear-to-default and the client-side validation that gates Save.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile } from './helpers/harness.js';

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
  dom.window.fetch = (url, fetchOpts) => {
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

describe('chips setting (#924)', () => {
  const TOOLS = { path: 'manager.tower.disabled_tools', label: 'Available tools', help: 'Tools Tower may use.', group: 'network',
                  service: 'manager', type: 'chips', hot: true, choices: ['alarms', 'help', 'log_tail'], exclude: true };
  const data = () => payload({ entries: [PORT, TOOLS],
    values: { 'manager.ws_proxy_port': 5001, 'manager.tower.disabled_tools': ['log_tail'] },
    defaults: { 'manager.ws_proxy_port': 5001, 'manager.tower.disabled_tools': [] } });

  test('renders one chip per choice, on unless the stored list excludes it, with an "all" default hint', async () => {
    const win = await boot(data());
    const chips = [...win.document.querySelectorAll('.st-chips[data-path="manager.tower.disabled_tools"] .st-chip')];
    expect(chips.map(c => c.dataset.chip)).toEqual(['alarms', 'help', 'log_tail']);
    expect(chips.map(c => c.classList.contains('on'))).toEqual([true, true, false]);
    expect(field(win.document, 'manager.tower.disabled_tools').querySelector('[data-dflt]').textContent).toBe('default all');
  });

  test('clicking chips writes the excluded list and Save sends it', async () => {
    const win = await boot(data());
    const box = win.document.querySelector('.st-chips[data-path="manager.tower.disabled_tools"]');
    box.querySelector('[data-chip="log_tail"]').click();
    box.querySelector('[data-chip="help"]').click();
    expect(box.classList.contains('dirty')).toBe(true);
    expect(field(win.document, 'manager.tower.disabled_tools').querySelector('[data-dflt]').textContent).toBe('default all');
    win.document.getElementById('adminSettingsSaveBtn').click();
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts[0].changes['manager.tower.disabled_tools']).toEqual(['help']);
  });
});
