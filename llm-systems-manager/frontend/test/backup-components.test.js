// #855: the Scheduled backups card shows which components a run covers, the
// ledger names each archive's component, and a missing AE archive reads as partial.
import { describe, it, expect } from 'vitest';
import { srcFile, blockSrc, runHarness } from './helpers/harness.js';

const indexSrc = srcFile('index.html');
const adminSrc = srcFile('js/admin.js');
const dashSrc = srcFile('js/dashboard-manager.js');

const backupPanel = blockSrc(indexSrc, '<div id="admin-backup"', '<!-- Access Control sub-tab', { includeEnd: false });

const RUN = '20260904-010000';
const MGR = { file: `lsm-auto-manager-h-${RUN}.lsmenc`, bytes: 20, mtime: 1000, mirrored: null, component: 'manager', run: RUN };
const AE = { file: `lsm-auto-ae-h-${RUN}.lsmenc`, bytes: 8, mtime: 1000, mirrored: null, component: 'alarm_engine', run: RUN };

const FULL = {
  ok: true, enabled: true, scheduler_running: true, interval_hours: 24, keep_last: 3,
  encrypted: false, mirror_dir: '', not_covered: {},
  last: {
    ok: true, partial: false, ts: 1000, file: MGR.file, bytes: 20, files: 3,
    components: { manager: { ok: true, file: MGR.file, bytes: 20, files: 3, error: null },
                  alarm_engine: { ok: true, file: AE.file, bytes: 8, error: null, remedy: null } },
  },
  last_export: { manager: null, alarm_engine: null }, next_due_ts: 2000, folder_bytes: 28,
  backups: [MGR, AE],
};

const PARTIAL = {
  ...FULL, folder_bytes: 20, backups: [MGR],
  last: { ...FULL.last, partial: true,
    components: { ...FULL.last.components,
      alarm_engine: { ok: false, file: null, bytes: 0, error: 'unauthorized — HTTP 403', remedy: 'Set the same management_token on both hosts.' } } },
};

const UNCOVERED = {
  ...FULL, not_covered: { alarm_engine: 'no [manager].alarm_engine_url is configured, so scheduled runs cover the manager only' },
  folder_bytes: 20, backups: [MGR],
  last: { ...FULL.last, partial: true,
    components: { ...FULL.last.components,
      alarm_engine: { ok: false, file: null, bytes: 0, error: 'unconfigured — no alarm engine URL', remedy: 'Set [manager].alarm_engine_url.' } } },
};

function render(status) {
  const win = runHarness({
    sources: [dashSrc, adminSrc],
    bootstrap: `window.SettingsFields = null;
                window.fetch = () => Promise.resolve(null);
                window.__render = (s) => { _adminBackupData = s; adminRenderBackup(); };
                adminLoadBackupStatus = () => {};`,
    bodyHtml: backupPanel,
  });
  win.__render(status);
  return win.document;
}

const text = (doc, id) => doc.getElementById(id).textContent.replace(/\s+/g, ' ').trim();

describe('scheduled backup coverage (#855)', () => {
  it('names both components in the card meta when the AE is covered', () => {
    expect(text(render(FULL), 'adminSchedBackupMeta')).toContain('Manager + Alarm Engine');
  });

  it('says manager only, with the reason, when the AE is outside the schedule', () => {
    const doc = render(UNCOVERED);
    expect(text(doc, 'adminSchedBackupMeta')).toContain('Manager only');
    expect(text(doc, 'adminSchedBackupBody')).toContain('alarm_engine_url');
    expect(doc.getElementById('adminSchedBackupPill').textContent).toBe('manager only');
  });

  it('shows on schedule when both archives landed', () => {
    const doc = render(FULL);
    expect(doc.getElementById('adminSchedBackupPill').textContent).toBe('on schedule');
    expect(doc.getElementById('adminSchedBackupPill').className).toContain('ok');
    const body = text(doc, 'adminSchedBackupBody');
    expect(body).toContain(MGR.file);
    expect(body).toContain(AE.file);
  });

  it('flags a run without the AE archive as partial and shows the remedy', () => {
    const doc = render(PARTIAL);
    const pill = doc.getElementById('adminSchedBackupPill');
    expect(pill.textContent).toBe('partial');
    expect(pill.className).toContain('warn');
    const body = text(doc, 'adminSchedBackupBody');
    expect(body).toContain('unauthorized — HTTP 403');
    expect(body).toContain('Set the same management_token on both hosts.');
  });

  it('keeps failed as the verdict when the manager archive itself failed', () => {
    const doc = render({ ...PARTIAL, last: { ...PARTIAL.last, ok: false, error: 'OSError: disk full' } });
    expect(doc.getElementById('adminSchedBackupPill').textContent).toBe('failed');
  });
});

describe('status written by an older build (#855)', () => {
  it('reads a pre-#855 last_backup.json without inventing an AE failure', () => {
    const { components, partial, ...legacy } = FULL.last;
    const doc = render({ ...FULL, last: legacy, backups: [{ ...MGR, component: 'manager' }] });
    expect(doc.getElementById('adminSchedBackupPill').textContent).toBe('on schedule');
    expect(text(doc, 'adminSchedBackupBody')).toContain('next run adds it');
  });
});

describe('archive ledger component column (#855)', () => {
  it('labels each row with its component', () => {
    const rows = [...render(FULL).querySelectorAll('#adminSchedBackupTbody tr')];
    expect(rows.map(r => r.querySelector('.comp').textContent.trim())).toEqual(['Manager', 'Alarm Engine']);
    expect(rows.map(r => r.querySelector('[data-bk-dl]').dataset.bkDl)).toEqual([MGR.file, AE.file]);
  });

  it('has a Component header and the empty state spans every column', () => {
    const doc = render({ ...FULL, backups: [] });
    const heads = [...doc.querySelectorAll('#adminSchedBackupCard thead th')].map(t => t.textContent.trim());
    expect(heads).toContain('Component');
    expect(Number(doc.querySelector('#adminSchedBackupTbody td').getAttribute('colspan'))).toBe(heads.length);
  });

  it('counts runs, not files, in the summary', () => {
    expect(text(render(FULL), 'bkSummary')).toContain('1 run kept');
  });
});

describe('mirror pill per archive (#855)', () => {
  it('flags only the archive whose copy failed', () => {
    const status = { ...FULL, mirror_dir: '/mnt/m',
      backups: [{ ...MGR, mirrored: true }, { ...AE, mirrored: false }],
      last: { ...FULL.last, mirrored: false, mirror_failed: [AE.file] } };
    const pills = [...render(status).querySelectorAll('#adminSchedBackupTbody tr')]
      .map(r => r.children[4].textContent.trim());
    expect(pills).toEqual(['copied', 'copy failed']);
  });

  it('falls back to the newest-archive rule for a status without mirror_failed', () => {
    const status = { ...FULL, mirror_dir: '/mnt/m',
      backups: [{ ...MGR, mirrored: false }],
      last: { ...FULL.last, mirrored: false } };
    const pill = render(status).querySelector('#adminSchedBackupTbody tr').children[4].textContent.trim();
    expect(pill).toBe('copy failed');
  });
});
