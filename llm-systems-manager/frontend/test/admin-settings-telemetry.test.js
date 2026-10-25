// Telemetry & analytics group: status row after the heartbeat toggle, View opens the payload, nothing on System Health.
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
  ok: true, groups: [{ key: 'telemetry', title: 'Telemetry & analytics' }],
  entries: [E('manager.telemetry.install_report', 'Install and update reports'), E('manager.telemetry.crash_reports', 'Crash reports'),
            E('manager.telemetry.checkin', 'Daily check-in'),
            E('manager.telemetry.heartbeat', 'Daily diagnostic report')],
  values: { 'manager.telemetry.install_report': true, 'manager.telemetry.crash_reports': true,
            'manager.telemetry.checkin': false, 'manager.telemetry.heartbeat': false },
  defaults: {}, secrets: {}, drift: {}, restart_pending: [], ae_sync_pending: [], ae_sync_retry_s: 30,
  topology: { split: false, ae_config_reachable: true },
});
const STATUS = { ok: true, install_report: true, crash_reports: true, checkin: false, do_not_track: false, heartbeat: false, pending: false,
                 last: { event: 'install', sent_at: new Date(Date.now() - 7200e3).toISOString(),
                         payload: { event: 'install', os: 'debian' } } };

async function boot(status = STATUS) {
  const dom = new JSDOM(
    `<!doctype html><html><body><div id="adminTab">${PANEL}</div></body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const w = dom.window;
  w.__posts = [];
  w.__toasts = [];
  w._toastErr = (m) => w.__toasts.push(m);
  w.fetch = (url, opts) => {
    if (opts && opts.method === 'POST') {
      w.__posts.push(url);
      const st = w.__postStatus || 200;
      return Promise.resolve({ ok: st === 200, status: st, json: () => Promise.resolve(st === 200 ? { ok: true } : { ok: false, error: 'RuntimeError' }) });
    }
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

describe('Telemetry & analytics group', () => {
  test('status row follows the heartbeat toggle and names the last report', async () => {
    const w = await boot();
    const rows = [...w.document.querySelectorAll('.st-rows > .settings-row')];
    const hb = rows.findIndex((r) => r.dataset.path === 'manager.telemetry.heartbeat');
    expect(hb).toBeGreaterThanOrEqual(0);
    expect(rows[hb + 1].id).toBe('stTelemetrySending');
    expect(rows[hb + 2].id).toBe('stTelemetryStatus');
    expect(rows[hb + 2].textContent).toMatch(/install/);
    expect(rows[hb + 2].textContent).toMatch(/2h ago/);
    const order = rows.map((r) => r.dataset.path).filter((x) => x && x.startsWith('manager.telemetry.'));
    expect(order.indexOf('manager.telemetry.checkin')).toBe(order.indexOf('manager.telemetry.crash_reports') + 1);
    expect(order.indexOf('manager.telemetry.heartbeat')).toBe(order.indexOf('manager.telemetry.checkin') + 1);
    expect(w.document.body.innerHTML).not.toContain('example.invalid');
  });
  test('View opens the themed modal with the escaped payload', async () => {
    const w = await boot();
    w.document.querySelector('#stTelemetryView').click();
    expect(w.__alert.bodyHtml).toContain('&quot;os&quot;: &quot;debian&quot;');
  });
  test('Send now is always rendered and POSTs, then the row reloads', async () => {
    const w = await boot({ ...STATUS, last: null });
    const btn = w.document.querySelector('#stTelemetrySend');
    expect(btn.textContent).toBe('Send now');
    btn.click();
    await flush(); await flush();
    expect(w.__posts).toEqual(['/api/admin/telemetry/send']);
    expect(w.document.querySelector('#stTelemetrySend').disabled).toBe(false);
  });
  test('a failed send shows a plain toast, never the error class, and re-enables the button', async () => {
    const w = await boot({ ...STATUS, last: null });
    w.__postStatus = 502;
    w.document.querySelector('#stTelemetrySend').click();
    await flush(); await flush();
    expect(w.__toasts).toEqual(['The report could not be sent']);
    expect(w.document.body.innerHTML).not.toContain('RuntimeError');
    expect(w.document.querySelector('#stTelemetrySend').disabled).toBe(false);
    w.__postStatus = 409;
    w.document.querySelector('#stTelemetrySend').click();
    await flush(); await flush();
    expect(w.__toasts[1]).toBe('A report is already being sent');
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
  test('sending row names the enabled reports and links the field list', async () => {
    const w = await boot();
    const row = w.document.querySelector('#stTelemetrySending');
    expect(row.textContent).toContain('install and update reports, crash reports');
    expect(row.textContent).not.toContain('daily check-in');
    const a = row.querySelector('a');
    expect(a.textContent).toBe('Field list');
    expect(a.getAttribute('href')).toBe('https://github.com/llmsyscore/llm-systems-manager/blob/main/docs/DEPLOYMENT.md#anonymous-reports');
    const w2 = await boot({ ...STATUS, install_report: false, crash_reports: false });
    expect(w2.document.querySelector('#stTelemetrySending').textContent).toContain('Nothing is being sent');
    const w3 = await boot({ ...STATUS, checkin: true, heartbeat: true });
    expect(w3.document.querySelector('#stTelemetrySending').textContent)
      .toContain('install and update reports, crash reports, daily diagnostic report');
    expect(w3.document.querySelector('#stTelemetrySending').textContent).not.toContain('daily check-in');
    const w4 = await boot({ ...STATUS, checkin: true, heartbeat: false });
    expect(w4.document.querySelector('#stTelemetrySending').textContent).toMatch(/crash reports, daily check-in/);
  });
  test('DO_NOT_TRACK says nothing is sent and disables Send now', async () => {
    const w = await boot({ ...STATUS, do_not_track: true });
    expect(w.document.querySelector('#stTelemetrySending').textContent)
      .toContain('Nothing is sent: DO_NOT_TRACK is set on this machine');
    expect(w.document.querySelector('#stTelemetrySend').disabled).toBe(true);
  });
  test('a 403 on send toasts the DO_NOT_TRACK message', async () => {
    const w = await boot({ ...STATUS, last: null });
    w.__postStatus = 403;
    w.document.querySelector('#stTelemetrySend').click();
    await flush(); await flush();
    expect(w.__toasts).toEqual(['Nothing is sent while DO_NOT_TRACK is set']);
  });
  test('sending row reads Status unavailable when the fetch fails', async () => {
    const w = await boot('fail');
    expect(w.document.querySelector('#stTelemetrySending').textContent).toMatch(/Status unavailable/);
  });
  test('System Health has no telemetry row', () => {
    expect(healthSrc).not.toMatch(/telemetry|Telemetry/i);
  });
});
