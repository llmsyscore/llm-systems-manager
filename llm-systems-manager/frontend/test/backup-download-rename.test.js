// #848: archive rows get a download link served by the admin endpoint, and the
// sub-tab's Export/Import wording becomes Backup/Restore.
import { describe, it, expect } from 'vitest';
import { srcFile, blockSrc, runHarness } from './helpers/harness.js';

const indexSrc = srcFile('index.html');
const adminSrc = srcFile('js/admin.js');
const dashSrc = srcFile('js/dashboard-manager.js');

const backupPanel = blockSrc(indexSrc, '<div id="admin-backup"', '<!-- Access Control sub-tab', { includeEnd: false });

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

describe('archive download control (#848)', () => {
  it('gives each row a download button naming its archive', () => {
    const rows = [...render().querySelectorAll('#adminSchedBackupTbody tr')];
    expect(rows).toHaveLength(2);
    expect(rows[0].querySelector('[data-bk-dl]').dataset.bkDl)
      .toBe('lsm-auto-manager-h-20260904-010000.lsmenc');
  });

  // A plain <a download> saves the response body whatever the status, so a
  // 401/404/500 lands on disk under the archive's own name.
  it('uses no anchor that would save an error response as the archive', () => {
    expect(render().querySelector('#adminSchedBackupTbody a[download]')).toBeNull();
  });

  it('keeps the empty state spanning every column, download included', () => {
    const doc = render({ ...STATUS, backups: [] });
    const cell = doc.querySelector('#adminSchedBackupTbody td');
    const cols = doc.querySelectorAll('#adminSchedBackupCard thead th').length;
    expect(Number(cell.getAttribute('colspan'))).toBe(cols);
  });
});

describe('adminDownloadArchive (#848)', () => {
  function harness(reply) {
    const win = runHarness({
      sources: [dashSrc, adminSrc],
      bootstrap: `_adminBackupData = ${JSON.stringify(STATUS)};
                  window.SettingsFields = null;
                  window.__fetched = [];
                  window.__reloaded = 0;
                  adminLoadBackupStatus = () => { window.__reloaded++; };
                  window.fetch = (url) => { window.__fetched.push(url);
                    return Promise.resolve(${reply}); };
                  adminRenderBackup();`,
      bodyHtml: backupPanel,
    });
    return win;
  }
  const okReply = `{ ok: true, status: 200, blob: () => Promise.resolve({ size: 42 }) }`;
  const errReply = (status, body) =>
    `{ ok: false, status: ${status}, text: () => Promise.resolve(${JSON.stringify(body)}) }`;

  it('percent-encodes the file name in the request URL', async () => {
    const win = harness(okReply);
    win.document.createElement('a').click = () => {};
    win.URL.createObjectURL = () => 'blob:x';
    win.URL.revokeObjectURL = () => {};
    await win.adminDownloadArchive('a b&c.lsmenc');
    expect(win.__fetched).toEqual(['/api/admin/backup-archive/a%20b%26c.lsmenc']);
  });

  it('reports a failure in the card log and saves nothing', async () => {
    const win = harness(errReply(500, '{"ok":false,"error":"internal server error"}'));
    let saved = 0;
    win.URL.createObjectURL = () => { saved++; return 'blob:x'; };
    await win.adminDownloadArchive('x.lsmenc');
    expect(saved).toBe(0);
    expect(win.document.getElementById('adminBackupResult').textContent)
      .toContain('internal server error');
  });

  it('refreshes the stale ledger when the archive is already pruned', async () => {
    const win = harness(errReply(404, '{"ok":false,"error":"no such archive"}'));
    await win.adminDownloadArchive('x.lsmenc');
    expect(win.__reloaded).toBe(1);
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
