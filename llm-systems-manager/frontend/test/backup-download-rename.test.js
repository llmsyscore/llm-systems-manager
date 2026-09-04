// #848: archive rows get a download link served by the admin endpoint, and the
// sub-tab's Export/Import wording becomes Backup/Restore.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

const indexSrc = srcFile('index.html');
const adminSrc = srcFile('js/admin.js');
const dashSrc = srcFile('js/dashboard-manager.js');

const backupPanel = indexSrc.slice(indexSrc.indexOf('<div id="admin-backup"'),
                                   indexSrc.indexOf('<!-- Access Control sub-tab'));

const STATUS = {
  ok: true, enabled: true, scheduler_running: true, interval_hours: 24, keep_last: 3,
  encrypted: false, mirror_dir: '', last: { ok: true, ts: 1000, file: 'a.lsmenc', bytes: 10, files: 3 },
  last_export: { manager: null, alarm_engine: null }, next_due_ts: 2000, folder_bytes: 30,
  backups: [
    { file: 'lsm-auto-manager-h-20260904-010000.lsmenc', bytes: 20, mtime: 1000, mirrored: null },
    { file: 'lsm-auto-manager-h-20260903-010000.lsmenc', bytes: 10, mtime: 900, mirrored: null },
  ],
};

function render(status = STATUS) {
  const win = runHarness({
    sources: [dashSrc, adminSrc],
    bootstrap: `_adminBackupData = ${JSON.stringify(status)};
                window.SettingsFields = null;
                adminRenderBackup();`,
    bodyHtml: backupPanel,
  });
  return win.document;
}

describe('archive download links (#848)', () => {
  it('gives each row a link to the admin backup-archive endpoint', () => {
    const rows = [...render().querySelectorAll('#adminSchedBackupTbody tr')];
    expect(rows).toHaveLength(2);
    const a = rows[0].querySelector('a[download]');
    expect(a.getAttribute('href'))
      .toBe('/api/admin/backup-archive/lsm-auto-manager-h-20260904-010000.lsmenc');
    expect(a.getAttribute('download')).toBe('lsm-auto-manager-h-20260904-010000.lsmenc');
  });

  it('percent-encodes the file name it puts in the URL', () => {
    const doc = render({ ...STATUS, backups: [{ file: 'a b&c.lsmenc', bytes: 1, mtime: 1, mirrored: null }] });
    expect(doc.querySelector('#adminSchedBackupTbody a[download]').getAttribute('href'))
      .toBe('/api/admin/backup-archive/a%20b%26c.lsmenc');
  });

  it('keeps the empty state spanning every column, download included', () => {
    const doc = render({ ...STATUS, backups: [] });
    const cell = doc.querySelector('#adminSchedBackupTbody td');
    const cols = doc.querySelectorAll('#adminSchedBackupCard thead th').length;
    expect(Number(cell.getAttribute('colspan'))).toBe(cols);
  });
});

describe('Backup/Restore wording (#848)', () => {
  it('leaves no export/import wording in the sub-tab text', () => {
    const text = render().getElementById('admin-backup').textContent;
    expect(text).not.toMatch(/export/i);
    expect(text).not.toMatch(/import/i);
  });

  it('labels the manual actions Backup and Restore', () => {
    const doc = render();
    const labels = [...doc.querySelectorAll('#adminBackupCard .acts button')].map(b => b.textContent.trim());
    expect(labels).toEqual(['Backup…', 'Restore…', 'Backup…', 'Restore…']);
  });

  it('renders "last backup", not "last export"', () => {
    const doc = render({ ...STATUS, last_export: { manager: { ts: 1000, bytes: 4096 }, alarm_engine: null } });
    expect(doc.getElementById('bkLastExport_manager').textContent).toMatch(/^last backup/);
    expect(doc.getElementById('bkLastExport_alarm_engine').textContent).toBe('last backup never');
  });
});
