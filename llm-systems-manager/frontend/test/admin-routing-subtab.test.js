// #476: Pools & Pins + Autopilot consolidated into one fleet-routing sub-tab,
// Admin sub-tabs alphabetized, Agents/Audit tables column-sortable.
import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const bootSrc = readFileSync(join(root, 'js/boot.js'), 'utf8');
const adminSrc = readFileSync(join(root, 'js/admin.js'), 'utf8');
const apSrc = readFileSync(join(root, 'js/autopilot.js'), 'utf8');
const indexSrc = readFileSync(join(root, 'index.html'), 'utf8');

describe('fleet-routing sub-tab consolidation (#476)', () => {
  beforeAll(() => {
    document.documentElement.innerHTML = indexSrc;
  });

  it('index.html has one admin-routing panel, no admin-pool/admin-autopilot panels', () => {
    expect(document.getElementById('admin-routing')).toBeTruthy();
    expect(document.getElementById('admin-pool')).toBeNull();
    expect(document.getElementById('admin-autopilot')).toBeNull();
  });

  it('merged panel contains pool, pins, and both autopilot cards', () => {
    expect(document.querySelector('#admin-routing #adminPoolCard')).toBeTruthy();
    expect(document.querySelector('#admin-routing #adminPinsCard')).toBeTruthy();
    expect(document.querySelector('#admin-routing #apEntriesBody')).toBeTruthy();
    expect(document.querySelector('#admin-routing #apProposalsBody')).toBeTruthy();
  });

  it('nav has a single Fleet Routing button wired to the routing sub-tab', () => {
    const btn = document.getElementById('subTabBtnAdminRouting');
    expect(btn).toBeTruthy();
    expect(btn.textContent).toBe('Fleet Routing');
    expect(btn.getAttribute('onclick')).toContain("switchSubTab('admin','routing')");
    expect(document.getElementById('subTabBtnAdminPool')).toBeNull();
    expect(document.getElementById('subTabBtnAdminAutopilot')).toBeNull();
  });

  it('admin sub-tab buttons are ordered alphabetically', () => {
    const nav = document.querySelector('#adminTab .sub-tab-nav');
    const labels = [...nav.querySelectorAll('.sub-tab-btn')].map(b => b.textContent.trim());
    expect(labels.length).toBeGreaterThanOrEqual(5);
    expect(labels).toEqual([...labels].sort((a, b) => a.localeCompare(b)));
  });

  it('Agents stays the default admin sub-tab', () => {
    expect(bootSrc).toMatch(/admin:\s*'agents'/);
    const agentsBtn = document.getElementById('subTabBtnAdminAgents');
    expect(agentsBtn.classList.contains('active')).toBe(true);
    expect(document.querySelector('#admin-agents.sub-tab-panel.active')).toBeTruthy();
  });

  it('boot.js admin subs list swaps pool+autopilot for routing', () => {
    const subsLine = bootSrc.match(/admin:\s*\{[^\n]*subs:\s*\[([^\]]*)\]/);
    expect(subsLine).toBeTruthy();
    expect(subsLine[1]).toContain("'routing'");
    expect(subsLine[1]).not.toContain("'pool'");
    expect(subsLine[1]).not.toContain("'autopilot'");
  });

  it('boot.js initializes the autopilot editor on routing sub-tab entry', () => {
    expect(bootSrc).toMatch(/sub === 'routing'[\s\S]{0,120}AP\.init/);
  });

  it('autopilot.js visibility gate keys on the routing sub-tab', () => {
    expect(apSrc).toContain("_subTabState.admin === 'routing'");
    expect(apSrc).not.toContain("_subTabState.admin === 'autopilot'");
  });
});

describe('autopilot-managed badges (#476)', () => {
  it('pin rows can carry an autopilot-managed badge', () => {
    expect(adminSrc).toMatch(/adminRenderPins[\s\S]+?ap-managed-badge/);
  });
  it('pool card has the autopilot-managed membership badge element', () => {
    expect(document.getElementById('adminPoolApBadge')).toBeTruthy();
    expect(adminSrc).toMatch(/adminPoolApBadge/);
  });
  it('managed = single-replica entries pin, replicated entries pool', () => {
    expect(adminSrc).toMatch(/max_replicas[^\n]*<=\s*1/);
    expect(adminSrc).toMatch(/max_replicas[^\n]*>\s*1/);
  });
});

describe('sortable table columns (#476 scope)', () => {
  beforeAll(() => {
    document.documentElement.innerHTML = indexSrc;
  });

  it('agents table headers are sort-wired except Actions', () => {
    const ths = [...document.querySelectorAll('#adminAgentsTable thead th')];
    const sortable = ths.filter(th => th.classList.contains('adm-th-sort'));
    expect(sortable.length).toBe(4);
    sortable.forEach(th => {
      expect(th.dataset.key).toBeTruthy();
      expect(th.getAttribute('onclick')).toContain('adminSortAgents(this)');
    });
    expect(ths[ths.length - 1].classList.contains('adm-th-sort')).toBe(false);
  });

  it('audit table headers are all sort-wired', () => {
    const ths = [...document.querySelectorAll('#adminAuditTable thead th')];
    expect(ths.length).toBe(6);
    ths.forEach(th => {
      expect(th.classList.contains('adm-th-sort')).toBe(true);
      expect(th.dataset.key).toBeTruthy();
      expect(th.getAttribute('onclick')).toContain('adminSortAudit(this)');
    });
  });

  it('admin.js implements the sort handlers and re-renders from caches', () => {
    expect(adminSrc).toMatch(/function adminSortAgents/);
    expect(adminSrc).toMatch(/function adminSortAudit/);
    expect(adminSrc).toMatch(/_adminRenderAgentsTable/);
    expect(adminSrc).toMatch(/_adminRenderAuditTable/);
  });

  it('sort arrows apply before the empty-table early returns', () => {
    // Arrows render even with zero rows.
    expect(adminSrc).toMatch(
      /function _adminRenderAgentsTable\(\) \{[\s\S]*?_adminSortArrows[\s\S]*?_adminAgentsCache\.length/);
    expect(adminSrc).toMatch(
      /function _adminRenderAuditTable\(\) \{[\s\S]*?_adminSortArrows[\s\S]*?_adminAuditEntries\.length/);
  });

  it('zero registered agents no longer skips the pins/pool render', () => {
    // Pins/pool/badges render on agent-less installs too.
    expect(adminSrc).not.toMatch(/!agents\.length\)\s*return/);
  });

  it('a failed audit load clears the cached page before the error message', () => {
    expect(adminSrc).toMatch(
      /catch \(e\) \{[\s\S]{0,220}_adminAuditEntries = \[\];[\s\S]{0,220}Failed to load audit log/);
  });

  it('admin.css styles the sortable headers and direction arrows', () => {
    const css = readFileSync(join(root, 'css/admin.css'), 'utf8');
    expect(css).toMatch(/\.adm-th-sort/);
    expect(css).toMatch(/data-dir/);
  });
});
