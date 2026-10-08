// #1208: operators do not see admin-only buttons (Terminal, Server config Save),
// see a visible note instead, and cannot apply a vLLM Auto-Tune result.
import { describe, test, expect, beforeEach, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal } from './helpers/harness.js';

const foundation = srcFile('js/foundation.js');
const index = srcFile('index.html');
const autotune = srcFile('js/autotune.js');

function loadGating() {
  const fn = fnSrc(foundation, 'applyRoleGating');
  expect(fn, 'applyRoleGating not found').toBeTruthy();
  evalGlobal(fn + '\nwindow.applyRoleGating = applyRoleGating;');
}

beforeEach(() => {
  document.body.innerHTML = `
    <button id="tabBtnAdmin">Admin</button>
    <button data-admin-only id="term">Terminal</button>
    <button data-admin-only id="save">Save</button>
    <span data-operator-note id="note" style="display:none">Saving needs an admin</span>`;
  window._sdRenderAccount = vi.fn();
  window.switchTab = vi.fn();
  window._activeTab = 'llm';
  window.towerRefreshState = undefined;
  loadGating();
});

describe('#1208 markup marks every admin-only control', () => {
  test('the three Terminal buttons and both Server config save buttons carry data-admin-only', () => {
    for (const fn of ['toggleTerminal()', 'toggleLmsTerminal()', 'toggleVllmTerminal()']) {
      expect(index).toMatch(new RegExp(`<button[^>]*data-admin-only[^>]*onclick="${fn.replace(/[()]/g, '\\$&')}"`));
    }
    expect(index).toMatch(/data-admin-only onclick="saveSvcConfig\(false\)"/);
    expect(index).toMatch(/data-admin-only onclick="saveSvcConfig\(true\)"/);
    const start = index.indexOf('<div class="svcconfig-actions">');
    const actions = index.slice(start, index.indexOf('closeSvcConfig()', start));
    expect(actions).toMatch(/data-operator-note[^>]*>Saving needs an admin/);
  });
});

describe('#1208 applyRoleGating', () => {
  test('operator: admin-only controls hidden, note shown', () => {
    window._me = { admin_access: false };
    window.applyRoleGating();
    expect(document.getElementById('term').style.display).toBe('none');
    expect(document.getElementById('save').style.display).toBe('none');
    expect(document.getElementById('note').style.display).toBe('');
    expect(document.getElementById('tabBtnAdmin').style.display).toBe('none');
  });
  test('admin: controls shown, note hidden', () => {
    window._me = { admin_access: true };
    window.applyRoleGating();
    expect(document.getElementById('term').style.display).toBe('');
    expect(document.getElementById('save').style.display).toBe('');
    expect(document.getElementById('note').style.display).toBe('none');
  });
});

describe('#1208 vLLM Auto-Tune apply needs an admin', () => {
  test('the Apply button, the apply() entry and the run start all check applyNeedsAdmin', () => {
    expect(autotune).toMatch(/const applyNeedsAdmin = \(\) => isVllm\(\) && !isAdmin\(\);/);
    expect(autotune).toMatch(/btn\.disabled = !n \|\| applyNeedsAdmin\(\);/);
    const apply = autotune.slice(autotune.indexOf('async function apply()'));
    expect(apply.slice(0, apply.indexOf('let step'))).toMatch(/if \(applyNeedsAdmin\(\)\) \{ setMsg\(/);
    const run = autotune.slice(autotune.indexOf('async function run()'), autotune.indexOf('async function startRun('));
    expect(run).toMatch(/if \(applyNeedsAdmin\(\)\) log\(/);
  });
});
