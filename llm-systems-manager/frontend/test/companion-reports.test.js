// Companion Settings → Reports: three switches and a PUT per toggle.
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const html = fs.readFileSync(path.join(root, 'companion.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'js/companion.js'), 'utf8');

describe('companion reports card', () => {
  it('has the container and the settings code targets it', () => {
    expect(html).toContain('id="settingsReports"');
    expect(js).toMatch(/\$\('settingsReports'\)/);
  });
  it('toggles PUT the allowed keys only', () => {
    expect(js).toMatch(/jpost\('\/api\/admin\/telemetry', \{ \[key\]: next \}, 'PUT'\)/);
    expect(js).toMatch(/data-tele="\$\{key\}"/);
    for (const k of ['install_report', 'crash_reports', 'heartbeat']) expect(js).toContain(`'${k}'`);
  });
});

describe('companion reports card (jsdom)', () => {
  it('clicking the heartbeat switch PUTs {heartbeat: true}', async () => {
    const { JSDOM } = await import('jsdom');
    const body = html.replace(/<script\b[^>]*src=[^>]*><\/script>/g, '');
    const dom = new JSDOM(body, { runScripts: 'dangerously', url: 'http://localhost/companion?tab=settings' });
    const w = dom.window;
    const calls = [];
    const TELE = { ok: true, install_report: true, crash_reports: true, heartbeat: false, pending: false, last: null };
    w.matchMedia = w.matchMedia || (() => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
    w.fetch = (url, opts = {}) => {
      calls.push({ url: String(url), method: opts.method || 'GET', body: opts.body });
      let data = {};
      if (url === '/api/me') data = { user: 'a', role: 'admin', admin_access: true };
      else if (url === '/api/admin/telemetry') data = TELE;
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(data))) });
    };
    const inject = (p) => {
      const s = w.document.createElement('script');
      s.textContent = fs.readFileSync(path.join(root, p), 'utf8');
      w.document.body.appendChild(s);
    };
    ['js/lib/companion-view.js', 'js/companion.js'].forEach(inject);
    w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
    const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0)); };
    await settle();
    const sw = w.document.querySelector('#settingsReports [data-tele="heartbeat"]');
    expect(sw).not.toBeNull();
    expect(sw.disabled).toBe(false);
    sw.click();
    await settle();
    const put = calls.find((c) => c.url === '/api/admin/telemetry' && c.method === 'PUT');
    expect(put).toBeTruthy();
    expect(JSON.parse(put.body)).toEqual({ heartbeat: true });
    w.close();
  });
});
