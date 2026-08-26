// #477: Authentication + Users consolidated into one access-control sub-tab.
import { describe, it, expect, beforeEach } from 'vitest';
import { srcFile, loadSwitchSubTab } from './helpers/harness.js';

const bootSrc = srcFile('js/boot.js');
const indexSrc = srcFile('index.html');

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
