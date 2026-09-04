// #846: Access Control → "Access settings" card mirrors the Auth & Security
// catalog group through the shared field renderer and /api/admin/settings.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = (f) => readFileSync(join(here, '..', 'js', f), 'utf8');
const indexSrc = readFileSync(join(here, '..', 'index.html'), 'utf8');
const CARD = indexSrc.slice(indexSrc.indexOf('<div class="card collapsed" id="adminAccessSettingsCard"'),
  indexSrc.indexOf('</div>', indexSrc.indexOf('id="acCfgFoot"')) + 6);

const CATALOG = {
  ok: true,
  entries: [
    { path: 'manager.auth.mode', type: 'choice', label: 'Auth mode', help: '', group: 'auth', service: 'manager', choices: ['auto', 'required'] },
    { path: 'manager.auth.session_lifetime_days', type: 'int', label: 'Session lifetime (days)', help: 'Browser session validity.', group: 'auth', service: 'manager', min: 1, max: 365 },
    { path: 'manager.auth.lockout_threshold', type: 'int', label: 'Lockout threshold', help: 'Failed logins before lockout.', group: 'auth', service: 'manager', min: 1, max: 100 },
    { path: 'manager.security.admin_cidrs', type: 'list', label: 'Admin CIDRs', help: 'One CIDR per line.', group: 'auth', service: 'manager' },
    { path: 'manager.port', type: 'int', label: 'HTTP port', help: '', group: 'network', service: 'manager' },
  ],
  values: { 'manager.auth.session_lifetime_days': 14, 'manager.auth.lockout_threshold': 5, 'manager.security.admin_cidrs': ['192.0.2.0/24'], 'manager.port': 5000 },
  defaults: { 'manager.auth.session_lifetime_days': 30, 'manager.auth.lockout_threshold': 5, 'manager.security.admin_cidrs': [] },
  secrets: {}, groups: [{ key: 'auth', title: 'Auth & Security' }], restart_pending: [],
};

function harness(putResponder) {
  const dom = new JSDOM(`<!doctype html><html><head></head><body><div id="adminTab">${CARD}</div></body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const w = dom.window;
  w.calls = [];
  w.authLoads = 0;
  w.CSS = { escape: (v) => String(v).replace(/[^a-zA-Z0-9_-]/g, c => '\\' + c) };
  w.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET';
    w.calls.push({ url, method, body: opts.body ? JSON.parse(opts.body) : null });
    if (method === 'PUT') { const [status, body] = await putResponder(w.calls); return { ok: status < 300, status, json: async () => body }; }
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(CATALOG)) };
  };
  const inject = (code) => { const s = w.document.createElement('script'); s.textContent = code; w.document.head.appendChild(s); };
  inject(src('foundation.js'));
  inject(src('admin.js'));
  inject(src('admin-settings.js'));
  inject(src('admin-access-settings.js'));
  inject('window.adminAuthLoad = () => { window.authLoads++; };');
  w.AccessSettings.bind();
  return w;
}
const tick = () => new Promise(r => setTimeout(r, 5));
const q = (w, sel) => w.document.querySelector(sel);
const type = (w, el, v) => { el.value = v; el.dispatchEvent(new w.Event('input', { bubbles: true })); };
async function expand(w) { q(w, '#acCfgHead').click(); await tick(); await tick(); }

describe('Access settings card', () => {
  test('pick keeps the auth group and leaves the auth mode to the Login card', () => {
    const w = harness(async () => [200, { ok: true }]);
    expect(w.AccessSettings.pick(CATALOG).map(e => e.path)).toEqual([
      'manager.auth.session_lifetime_days', 'manager.auth.lockout_threshold', 'manager.security.admin_cidrs']);
  });

  test('starts collapsed, loads on first expand, and renders the shared fields', async () => {
    const w = harness(async () => [200, { ok: true }]);
    const card = q(w, '#adminAccessSettingsCard');
    expect(card.classList.contains('collapsed')).toBe(true);
    expect(w.calls.length).toBe(0);
    await expand(w);
    expect(card.classList.contains('collapsed')).toBe(false);
    expect(w.calls.length).toBe(1);
    const rows = [...w.document.querySelectorAll('#acCfgBody .settings-row')].map(r => r.dataset.path);
    expect(rows).toEqual(['manager.auth.session_lifetime_days', 'manager.auth.lockout_threshold', 'manager.security.admin_cidrs']);
    expect(q(w, '#acCfgBody .st-input[data-path="manager.auth.session_lifetime_days"]').value).toBe('14');
    expect(q(w, '[data-ac-save]').disabled).toBe(true);
    expect(q(w, '#acCfgMeta').textContent).toContain('3');
    q(w, '#acCfgHead').click();
    expect(card.classList.contains('collapsed')).toBe(true);
  });

  test('tracks dirty and invalid edits in place and gates Save', async () => {
    const w = harness(async () => [200, { ok: true }]);
    await expand(w);
    const inp = q(w, '#acCfgBody .st-input[data-path="manager.auth.lockout_threshold"]');
    type(w, inp, '0');
    expect(q(w, '#acCfgFoot').textContent).toContain('1 invalid');
    expect(q(w, '[data-ac-save]').disabled).toBe(true);
    expect(q(w, '#acCfgBody .settings-row[data-path="manager.auth.lockout_threshold"] .err').textContent).toContain('from 1 to 100');
    type(w, inp, '7');
    expect(q(w, '#acCfgFoot').textContent).toContain('1 unsaved change');
    expect(q(w, '[data-ac-save]').disabled).toBe(false);
    expect(q(w, '#acCfgBody .settings-row[data-path="manager.auth.lockout_threshold"]').classList.contains('dirty')).toBe(true);
    type(w, inp, '5');
    expect(q(w, '[data-ac-save]').disabled).toBe(true);
    type(w, inp, '9');
    q(w, '[data-ac-discard]').click();
    expect(q(w, '#acCfgBody .st-input[data-path="manager.auth.lockout_threshold"]').value).toBe('5');
    expect(q(w, '[data-ac-save]').disabled).toBe(true);
  });

  test('Save PUTs only the changed paths and reports a required restart', async () => {
    const w = harness(async () => [200, { ok: true, applied: ['manager.auth.lockout_threshold'], restart_required: ['manager'], restart_paths: ['manager.auth.lockout_threshold'], errors: {} }]);
    await expand(w);
    type(w, q(w, '#acCfgBody .st-input[data-path="manager.auth.lockout_threshold"]'), '7');
    type(w, q(w, '#acCfgBody .st-input[data-path="manager.security.admin_cidrs"]'), '10.0.0.0/8\n192.0.2.0/24');
    q(w, '[data-ac-save]').click();
    await tick(); await tick();
    const put = w.calls.find(c => c.method === 'PUT');
    expect(put.body).toEqual({ changes: { 'manager.auth.lockout_threshold': 7, 'manager.security.admin_cidrs': ['10.0.0.0/8', '192.0.2.0/24'] } });
    expect(q(w, '#acCfgMsg').textContent).toContain('Saved');
    // #816: the restart prompt names the field and carries the restart button inline.
    const notice = q(w, '#acCfgBody .notice');
    expect(notice.textContent).toContain('Restart required');
    expect(notice.textContent).toContain('Lockout threshold');
    expect(notice.querySelector('[data-restart="manager"]')).not.toBeNull();
    expect(w.authLoads).toBe(1);
    expect(w.calls.filter(c => c.method === 'GET' && c.url === '/api/admin/settings').length).toBe(2);
  });

  test('a rejected save keeps the edits and marks the offending field', async () => {
    const w = harness(async () => [400, { ok: false, errors: { 'manager.auth.lockout_threshold': 'must be at most 100' } }]);
    await expand(w);
    type(w, q(w, '#acCfgBody .st-input[data-path="manager.auth.lockout_threshold"]'), '50');
    q(w, '[data-ac-save]').click();
    await tick();
    expect(q(w, '#acCfgBody .st-input[data-path="manager.auth.lockout_threshold"]').value).toBe('50');
    expect(q(w, '#acCfgBody .settings-row[data-path="manager.auth.lockout_threshold"] .err').textContent).toBe('must be at most 100');
    expect(q(w, '#acCfgMsg').textContent).toContain('fix the highlighted fields');
  });

  test('invalidate reloads a clean card but never discards unsaved edits', async () => {
    const w = harness(async () => [200, { ok: true }]);
    w.AccessSettings.invalidate();
    await tick();
    expect(w.calls.length).toBe(0);
    await expand(w);
    w.AccessSettings.invalidate();
    await tick();
    expect(w.calls.length).toBe(2);
    type(w, q(w, '#acCfgBody .st-input[data-path="manager.auth.lockout_threshold"]'), '8');
    w.AccessSettings.invalidate();
    await tick();
    expect(w.calls.length).toBe(2);
  });
});
