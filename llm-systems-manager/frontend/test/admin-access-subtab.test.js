// #477: Authentication + Users consolidated into one access-control sub-tab.
import { describe, it, expect, beforeEach } from 'vitest';
import { srcFile, loadSwitchSubTab, runHarness } from './helpers/harness.js';

const bootSrc = srcFile('js/boot.js');
const adminSrc = srcFile('js/admin.js');
const indexSrc = srcFile('index.html');
const _acAt = indexSrc.indexOf('<div id="admin-access" class="sub-tab-panel">');
const PANEL = indexSrc.slice(_acAt, indexSrc.indexOf('<!-- Audit Log sub-tab', _acAt));

function access(bootstrap) {
  return runHarness({ sources: [adminSrc], bodyHtml: `<div id="adminTab">${PANEL}</div>`, bootstrap });
}

describe('access-control sub-tab consolidation (#477)', () => {
  beforeEach(() => {
    document.documentElement.innerHTML = indexSrc;
    loadSwitchSubTab(bootSrc);
  });

  it('index.html has one admin-access panel, no admin-auth/admin-users panels', () => {
    expect(document.getElementById('admin-access')).toBeTruthy();
    expect(document.getElementById('admin-auth')).toBeNull();
    expect(document.getElementById('admin-users')).toBeNull();
  });

  it('nav has a single Access button wired to the access sub-tab', () => {
    const btn = document.getElementById('subTabBtnAdminAccess');
    expect(btn).toBeTruthy();
    expect(btn.getAttribute('onclick')).toContain("switchSubTab('admin','access')");
    expect(document.getElementById('subTabBtnAdminAuth')).toBeNull();
    expect(document.getElementById('subTabBtnAdminUsers')).toBeNull();
  });

  it('merged panel contains both the auth card and users card controls', () => {
    expect(document.querySelector('#admin-access #adminAuthMode')).toBeTruthy();
    expect(document.querySelector('#admin-access #adminUsersTbody')).toBeTruthy();
    expect(document.querySelector('#admin-access #adminUserNew')).toBeTruthy();
  });

  it('auth card note no longer points at a separate Users tab', () => {
    const panel = document.getElementById('admin-access');
    expect(panel.textContent).not.toMatch(/Users\s+tab/i);
  });

  it('login mode is an mc-seg mirroring the hidden select (#797)', () => {
    const panel = document.getElementById('admin-access');
    expect(panel.querySelector('.mc-seg#adminAuthSeg')).toBeTruthy();
    expect(panel.querySelector('#adminAuthModes')).toBeTruthy();
    expect(document.getElementById('adminAuthMode').tagName).toBe('SELECT');
    expect(document.getElementById('adminAuthMode').hidden).toBe(true);
  });

  it('the add-user row starts hidden behind the + Add user button', () => {
    expect(document.getElementById('adminUserAddRow').hidden).toBe(true);
    expect(document.getElementById('adminUserAddBtn')).toBeTruthy();
  });

  it('boot.js\'s real admin subs list swaps auth+users for access', () => {
    // Executes the real _SUB_TAB_MAP object literal (not a regex match on
    // boot.js text), so a leftover 'auth'/'users' entry actually fails.
    expect(window._SUB_TAB_MAP.admin.subs).toContain('access');
    expect(window._SUB_TAB_MAP.admin.subs).not.toContain('auth');
    expect(window._SUB_TAB_MAP.admin.subs).not.toContain('users');
  });

  it('switchSubTab(admin, access) activates the access panel and loads users', () => {
    let calls = 0;
    window.adminUsersLoad = () => { calls++; };
    window.switchSubTab('admin', 'access');
    expect(calls).toBe(1);
    expect(window._subTabState.admin).toBe('access');
    expect(document.getElementById('admin-access').classList.contains('active')).toBe(true);
    expect(document.getElementById('admin-agents').classList.contains('active')).toBe(false);
    expect(document.getElementById('subTabBtnAdminAccess').classList.contains('active')).toBe(true);
  });
});

describe('access control rendering (#797)', () => {
  const D = { ok: true, mode: 'required', policy: 'auto', instant: true, is_default: true,
              current_user: 'llmadmin', admin_cidrs: ['192.168.1.0/24'], bypass_role: 'operator' };

  it('the seg marks the live mode and the matching explainer box', () => {
    const win = access(`_adminAuthState = ${JSON.stringify(D)}; adminRenderAuth(${JSON.stringify(D)});`);
    const d = win.document;
    expect(d.querySelector('#adminAuthSeg button.on').textContent).toBe('Required');
    expect(d.getElementById('adminAuthMode').value).toBe('required');
    expect(d.querySelector('#adminAuthModes .ac-mode.on b').textContent).toBe('Required');
    expect(d.getElementById('adminAuthMeta').textContent).toContain('changes apply instantly');
    expect(d.getElementById('adminAuthDefaultNotice').hidden).toBe(false);
  });

  it('clicking the seg mirrors the hidden select and moves the accent', () => {
    const win = access(`_adminAuthState = ${JSON.stringify(D)}; adminRenderAuth(${JSON.stringify(D)});
      document.querySelector('#adminAuthSeg button[data-mode="trusted_cidr"]')
        .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));`);
    const d = win.document;
    expect(d.getElementById('adminAuthMode').value).toBe('trusted_cidr');
    expect(d.querySelector('#adminAuthModes .ac-mode.on b').textContent).toBe('Trusted network');
    expect(d.querySelector('#adminAuthModes .ac-mode.on').textContent).toContain('192.168.1.0/24');
    expect(d.querySelector('#adminAuthModes .ac-mode.on').textContent).toContain('operator');
  });

  it('a pinned policy says a restart is required', () => {
    const win = access(`adminRenderAuth(${JSON.stringify({ ...D, policy: 'required', instant: false })});`);
    expect(win.document.getElementById('adminAuthMeta').textContent).toContain('restart required');
  });

  it('user rows carry the you tag, status pill, hand-built stamp and the row menu', () => {
    const users = [
      { username: 'llmadmin', role: 'admin', disabled: false, locked: false, last_login: '2026-09-02T13:18:00' },
      { username: 'ops-bot', role: 'operator', disabled: false, locked: true, last_login: '2026-08-28T07:40:00',
        failed_count: 5, lock_minutes_left: 12 },
      { username: 'off', role: 'operator', disabled: true, locked: false, last_login: null },
    ];
    const win = access(`_adminAuthState = ${JSON.stringify(D)};
      document.getElementById('adminUsersTbody').innerHTML =
        ${JSON.stringify(users)}.map(_adminUserRow).join('');`);
    const rows = [...win.document.querySelectorAll('#adminUsersTbody tr')];
    expect(rows[0].querySelector('.you').textContent).toBe('you');
    expect(rows[0].querySelector('.t').innerHTML).toBe('Sep 2 · <b>1:18 PM</b>');
    expect(rows[0].querySelector('.pill.ok').textContent).toBe('active');
    expect(rows[1].querySelector('.pill.crit').textContent).toBe('locked');
    expect(rows[1].textContent).toContain('5 failed · 12 min left');
    expect(rows[1].querySelector('[data-uact="unlock"]')).toBeTruthy();
    expect(rows[0].querySelector('[data-uact="unlock"]')).toBeNull();
    expect(rows[2].querySelector('.pill.dim').textContent).toBe('disabled');
    expect(rows[2].querySelector('[data-uact="disable"]').textContent).toBe('▸');
    expect(rows[0].querySelector('[data-uact="disable"]').textContent).toBe('‖');
    expect(rows[0].querySelector('.mc-menu [data-uact="role"]').textContent).toContain('Make operator');
    expect(rows[0].querySelector('.mc-menu [data-uact="delete"]')).toBeTruthy();
    // Icons are inline SVG, never emoji.
    expect(rows[0].querySelector('[data-uact="resetpw"] svg')).toBeTruthy();
    expect(rows[0].querySelector('.t').textContent).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u);
  });

  it('a missing last sign-in renders an em dash', () => {
    const win = access(`window.__s = _adminStamp(null);`);
    expect(win.__s).toBe('—');
  });

  it('the row menu opens fixed-positioned so the table cannot clip it', () => {
    const users = [{ username: 'a', role: 'admin', disabled: false, locked: false, last_login: null }];
    const win = access(`_adminUsersBindOnce();
      document.getElementById('adminUsersTbody').innerHTML =
        ${JSON.stringify(users)}.map(_adminUserRow).join('');
      document.querySelector('[data-menu]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));`);
    const menu = win.document.querySelector('#adminUsersTbody .mc-menu');
    expect(menu.classList.contains('open')).toBe(true);
    expect(menu.style.position).toBe('fixed');
  });

  it('+ Add user reveals the add row and Cancel hides it again', () => {
    const win = access(`_adminUsersBindOnce();
      document.getElementById('adminUserAddBtn').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      window.__shown = !document.getElementById('adminUserAddRow').hidden;
      document.getElementById('adminUserCancelBtn').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      window.__hidden = document.getElementById('adminUserAddRow').hidden;`);
    expect(win.__shown).toBe(true);
    expect(win.__hidden).toBe(true);
  });
});
