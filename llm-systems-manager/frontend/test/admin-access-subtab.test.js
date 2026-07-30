// #477: Authentication + Users consolidated into one access-control sub-tab.
import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const bootSrc = readFileSync(join(root, 'js/boot.js'), 'utf8');
const indexSrc = readFileSync(join(root, 'index.html'), 'utf8');

describe('access-control sub-tab consolidation (#477)', () => {
  beforeAll(() => {
    document.documentElement.innerHTML = indexSrc;
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

  it('boot.js admin subs list swaps auth+users for access', () => {
    const subsLine = bootSrc.match(/admin:\s*\{[^\n]*subs:\s*\[([^\]]*)\]/);
    expect(subsLine).toBeTruthy();
    expect(subsLine[1]).toContain("'access'");
    expect(subsLine[1]).not.toContain("'auth'");
    expect(subsLine[1]).not.toContain("'users'");
  });

  it('boot.js loads users on access sub-tab entry', () => {
    expect(bootSrc).toMatch(/sub === 'access'[\s\S]{0,120}adminUsersLoad/);
  });
});
