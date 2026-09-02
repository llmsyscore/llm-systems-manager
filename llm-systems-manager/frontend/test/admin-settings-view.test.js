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

async function boot(data) {
  const dom = new JSDOM(
    `<!doctype html><html><body><div id="adminTab">${PANEL}</div></body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  dom.window.__puts = [];
  dom.window.fetch = (url, opts) => {
    if (opts && opts.method === 'PUT') {
      dom.window.__puts.push(JSON.parse(opts.body));
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, applied: [] }) });
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

const field = (doc, path) => doc.querySelector(`.settings-row[data-path="${path.replace(/\./g, '\\.')}"]`);
const input = (doc, path) => doc.querySelector(`.st-input[data-path="${path.replace(/\./g, '\\.')}"]`);

function type(win, path, value) {
  const el = input(win.document, path);
  el.value = value;
  el.dispatchEvent(new win.Event('input', { bubbles: true }));
  return el;
}

describe('group cards', () => {
  test('Most used leads, open; group cards follow alphabetically, all collapsed', async () => {
    const doc = (await boot(payload())).document;
    const cards = [...doc.querySelectorAll('#adminSettingsRoot .card')];
    expect(cards).toHaveLength(3);
    expect(cards[0].querySelector('h3').textContent).toBe('Most used');
    expect(cards[0].classList.contains('collapsed')).toBe(false);
    expect(cards[1].querySelector('h3').textContent).toBe('Backups');
    expect(cards[1].classList.contains('collapsed')).toBe(true);
    expect(cards[2].querySelector('h3').textContent).toBe('Network & TLS');
    expect(cards[2].classList.contains('collapsed')).toBe(true);
  });

  test('group meta counts settings and names the host', async () => {
    const doc = (await boot(payload())).document;
    const metas = [...doc.querySelectorAll('#adminSettingsRoot .card-h .meta')].map(m => m.textContent);
    expect(metas[0]).toBe('2 settings');
    expect(metas[1]).toContain('3 settings');
    expect(metas[1]).toContain('both hosts');
    expect(metas[2]).toBe('3 settings · manager');
  });

  test('the header summary counts settings, groups, unsaved and invalid', async () => {
    const win = await boot(payload());
    expect(win.document.getElementById('stSummary').textContent)
      .toBe('6 settings2 groups0 unsaved0 invalid');
  });

  test('the filter narrows fields and expands the matching cards', async () => {
    const win = await boot(payload());
    const f = win.document.getElementById('stFilter');
    f.value = 'passphrase';
    f.dispatchEvent(new win.Event('input', { bubbles: true }));
    const cards = [...win.document.querySelectorAll('#adminSettingsRoot .card')];
    expect(cards).toHaveLength(1);
    expect(cards[0].classList.contains('collapsed')).toBe(false);
    expect(win.document.querySelectorAll('.settings-row')).toHaveLength(1);
  });
});

describe('Most used card (#801)', () => {
  test('a common setting renders twice: once in Most used, once in its group', async () => {
    const doc = (await boot(payload())).document;
    expect(doc.querySelectorAll('.settings-row[data-path="manager.poll_interval"]')).toHaveLength(2);
    expect(doc.querySelectorAll('.st-input[data-path="manager.poll_interval"]')).toHaveLength(2);
  });

  test('editing the Most used copy marks both rows dirty and syncs the group twin by property', async () => {
    const win = await boot(payload());
    const inputs = win.document.querySelectorAll('.st-input[data-path="manager.poll_interval"]');
    type(win, 'manager.poll_interval', '45'); // input() grabs the first match: Most used
    const rows = win.document.querySelectorAll('.settings-row[data-path="manager.poll_interval"]');
    expect([...rows].every(r => r.classList.contains('dirty'))).toBe(true);
    expect(inputs[1].value).toBe('45');
    expect(inputs[1].getAttribute('value')).not.toBe('45'); // set by property, not via HTML string
  });

  test('editing the group copy marks both rows dirty and syncs the Most used twin', async () => {
    const win = await boot(payload());
    const inputs = win.document.querySelectorAll('.st-input[data-path="manager.poll_interval"]');
    inputs[1].value = '50';
    inputs[1].dispatchEvent(new win.Event('input', { bubbles: true }));
    const rows = win.document.querySelectorAll('.settings-row[data-path="manager.poll_interval"]');
    expect([...rows].every(r => r.classList.contains('dirty'))).toBe(true);
    expect(inputs[0].value).toBe('50');
  });

  test('toggling the Most used copy of a bool flips the group twin too', async () => {
    const win = await boot(payload());
    const toggles = win.document.querySelectorAll('.mc-toggle[data-path="manager.backup.enabled"]');
    expect(toggles).toHaveLength(2);
    toggles[0].dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    expect(toggles[0].classList.contains('on')).toBe(false);
    expect(toggles[1].classList.contains('on')).toBe(false);
    expect(toggles[1].getAttribute('aria-pressed')).toBe('false');
  });

  test('save sends a changed common path once, even though it renders twice', async () => {
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
    expect(tg.querySelector('.tlbl').textContent).toBe('Export an archive on a schedule');
    expect(doc.querySelector('#adminSettingsRoot input[type="checkbox"]')).toBeNull();
  });

  test('an int strips its unit from the label and shows it beside the input', async () => {
    const doc = (await boot(payload())).document;
    const row = field(doc, 'manager.poll_interval');
    expect(row.querySelector('label').textContent.trim()).toBe('Idle poll interval');
    expect(row.querySelector('.unit').textContent).toBe('s');
  });

  test('a choice renders a select and a shared key carries the both-hosts tag', async () => {
    const doc = (await boot(payload())).document;
    const row = field(doc, 'logging.level');
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
    const doc = (await boot(payload())).document;
    const row = field(doc, 'manager.ws_proxy_port');
    expect(row.querySelector('.dflt').textContent).toBe('');
    expect(row.querySelector('[data-reset]')).toBeNull();
  });

  test('a value differing from its default shows the hint and the reset button', async () => {
    const doc = (await boot(payload())).document;
    const row = field(doc, 'manager.alarm_engine_url');
    expect(row.querySelector('.dflt').textContent).toBe('default http://127.0.0.1:8081');
    expect(row.querySelector('[data-reset]')).toBeTruthy();
  });

  test('clearing a non-secret input reads "cleared → default" and submits null', async () => {
    const win = await boot(payload());
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
    type(win, 'manager.ws_proxy_port', '70000');
    const row = field(win.document, 'manager.ws_proxy_port');
    expect(row.classList.contains('invalid')).toBe(true);
    expect(row.querySelector('.err').textContent).toBe('Must be a whole number from 0 to 65535.');
    expect(win.document.getElementById('adminSettingsSaveBtn').disabled).toBe(true);
    expect(win.document.getElementById('stSummary').textContent).toContain('1 invalid');
  });

  test('a non-numeric int is rejected too, and fixing it re-enables Save', async () => {
    const win = await boot(payload());
    type(win, 'manager.ws_proxy_port', 'abc');
    expect(win.document.getElementById('adminSettingsSaveBtn').disabled).toBe(true);
    type(win, 'manager.ws_proxy_port', '5002');
    expect(field(win.document, 'manager.ws_proxy_port').classList.contains('invalid')).toBe(false);
    expect(win.document.getElementById('adminSettingsSaveBtn').disabled).toBe(false);
  });

  test('Save is a no-op while any field is invalid', async () => {
    const win = await boot(payload());
    type(win, 'manager.ws_proxy_port', '70000');
    win.document.getElementById('adminSettingsSaveBtn').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 0));
    expect(win.__puts).toHaveLength(0);
  });

  test('the save bar names which keys need a restart and which apply live', async () => {
    const win = await boot(payload());
    type(win, 'manager.poll_interval', '45');
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
  test('renders a standalone st-grid from caller-supplied entries and defaults', async () => {
    const win = await boot(payload());
    const html = win.SettingsFields.render([SCHED, PORT],
      { 'manager.backup.enabled': true, 'manager.ws_proxy_port': 9999 },
      { 'manager.backup.enabled': false, 'manager.ws_proxy_port': 5001 });
    expect(html).toContain('st-grid');
    expect(html).toContain('mc-toggle');
    expect(html).toContain('default <b>5001</b>');
  });
});

describe('review fixes (#797)', () => {
  test('clearing a field that already sits at its default is not a change', async () => {
    const win = await boot(payload());
    type(win, 'manager.ws_proxy_port', '');
    expect(win.document.getElementById('adminSettingsSaveBar')).toBeNull();
    expect(field(win.document, 'manager.ws_proxy_port').classList.contains('dirty')).toBe(false);
  });

  test('typed text survives a full re-render without entering the HTML string', async () => {
    const win = await boot(payload());
    type(win, 'manager.alarm_engine_url', 'https://x/<b>"');
    win.document.getElementById('stExpandAll').click();
    const again = input(win.document, 'manager.alarm_engine_url');
    expect(again.value).toBe('https://x/<b>"');
    expect(again.getAttribute('value')).toBe('');
  });
});
