// Usage reports group: status row after the heartbeat toggle, View opens the payload, nothing on System Health.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile, flush } from './helpers/harness.js';

const foundationSrc = srcFile('js/foundation.js');
const settingsSrc = srcFile('js/admin-settings.js');
const healthSrc = srcFile('js/admin-health.js');
const indexSrc = srcFile('index.html');
const _stAt = indexSrc.indexOf('<div id="admin-settings" class="sub-tab-panel">');
const PANEL = indexSrc.slice(_stAt, indexSrc.indexOf('\n\n    </div>', _stAt));

const E = (path, label) => ({ path, label, help: 'h', group: 'telemetry', service: 'manager', type: 'bool', hot: true });
const payload = () => ({
  ok: true, groups: [{ key: 'telemetry', title: 'Usage reports' }],
  entries: [E('manager.telemetry.install_report', 'Install report'), E('manager.telemetry.crash_reports', 'Crash reports'),
            E('manager.telemetry.heartbeat', 'Daily usage report')],
  values: { 'manager.telemetry.install_report': true, 'manager.telemetry.crash_reports': true,
            'manager.telemetry.heartbeat': false },
  defaults: {}, secrets: {}, drift: {}, restart_pending: [], ae_sync_pending: [], ae_sync_retry_s: 30,
  topology: { split: false, ae_config_reachable: true },
});
const STATUS = { ok: true, install_report: true, crash_reports: true, heartbeat: false, pending: false,
                 endpoint: 'https://example.invalid/r',
                 last: { event: 'install', sent_at: new Date(Date.now() - 7200e3).toISOString(),
                         payload: { event: 'install', os: 'debian' } } };

async function boot(status = STATUS) {
  const dom = new JSDOM(
    `<!doctype html><html><body><div id="adminTab">${PANEL}</div></body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const w = dom.window;
  w.fetch = (url) => {
    if (url === '/api/admin/telemetry' && status === 'fail') return Promise.reject(new Error('offline'));
    return Promise.resolve({ ok: true, status: 200,
      json: () => Promise.resolve(JSON.parse(JSON.stringify(url === '/api/admin/telemetry' ? status : payload()))) });
  };
  w._themedAlert = (o) => { w.__alert = o; return Promise.resolve(); };
  const inject = (code) => {
    const s = w.document.createElement('script');
    s.textContent = code;
    w.document.body.appendChild(s);
  };
  inject(`if (!window.CSS) window.CSS = { escape: s => String(s).replace(/([^a-zA-Z0-9_-])/g, '\\\\$1') };`);
  inject(foundationSrc);
  inject(srcFile('js/lib/tower-view.js'));
  inject(settingsSrc);
  await w.adminSettingsLoad();
  w.adminSettingsOpenGroup('telemetry');
  await flush(); await flush();
  return w;
}

describe('Usage reports group', () => {
  test('status row follows the heartbeat toggle and names the last report', async () => {
    const w = await boot();
    const rows = [...w.document.querySelectorAll('.st-rows > .settings-row')];
    const hb = rows.findIndex((r) => r.dataset.path === 'manager.telemetry.heartbeat');
    expect(hb).toBeGreaterThanOrEqual(0);
    expect(rows[hb + 1].id).toBe('stTelemetryStatus');
    expect(rows[hb + 1].textContent).toMatch(/install/);
    expect(rows[hb + 1].textContent).toMatch(/2h ago/);
    expect(w.document.body.innerHTML).not.toContain('example.invalid');
  });
  test('View opens the themed modal with the escaped payload', async () => {
    const w = await boot();
    w.document.querySelector('#stTelemetryView').click();
    expect(w.__alert.bodyHtml).toContain('&quot;os&quot;: &quot;debian&quot;');
  });
  test('reads "No report sent yet" when nothing was sent', async () => {
    const w = await boot({ ...STATUS, last: null });
    const row = w.document.querySelector('#stTelemetryStatus');
    expect(row.textContent).toMatch(/No report sent yet/);
    expect(w.document.querySelector('#stTelemetryView')).toBeNull();
  });
  test('a failing status fetch reads "Status unavailable"', async () => {
    const w = await boot('fail');
    expect(w.document.querySelector('#stTelemetryStatus').textContent).toMatch(/Status unavailable/);
  });
  test('a zoneless sent_at is read as UTC', async () => {
    const tz = process.env.TZ;
    process.env.TZ = 'America/New_York';
    try {
      const naive = new Date(Date.now() - 7200e3).toISOString().replace('Z', '');
      const w = await boot({ ...STATUS, last: { ...STATUS.last, sent_at: naive } });
      expect(w.document.querySelector('#stTelemetryStatus').textContent).toMatch(/2h ago/);
    } finally {
      if (tz === undefined) delete process.env.TZ; else process.env.TZ = tz;
    }
  });
  test('System Health has no telemetry row', () => {
    expect(healthSrc).not.toMatch(/telemetry|Usage report/i);
  });
});
