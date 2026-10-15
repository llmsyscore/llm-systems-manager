// #1273: System Health right-hand column — Recent view, budget bar, slider, warnings precedence.
import { describe, test, expect } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

const adminSrc = srcFile('js/admin.js');
const healthSrc = srcFile('js/admin-health.js');
const indexSrc = srcFile('index.html');
const CARD = indexSrc.slice(indexSrc.indexOf('<div id="adminHealthCard">'),
                            indexSrc.indexOf('<!-- Sub-tabs underneath System Health -->'));

function card(d, extra) {
  const win = runHarness({
    sources: [adminSrc, healthSrc],
    bodyHtml: `<div id="adminTab">${CARD}</div>`,
    bootstrap: `window.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve([]) });
      HealthView.render(${JSON.stringify(d)}, null); ${extra || ''}`,
  });
  return win;
}
const NOON = (() => { const d = new Date(); d.setHours(12, 0, 0, 0); return d.getTime(); })();
const iso = minsAgo => new Date(NOON - minsAgo * 60000).toISOString();

const HEALTHY = {
  overall: 'ok',
  manager: { ok: true, version: 'v1', uptime_s: 100, streams: { active: 0, limit: 40, peak: 1, refusals: 0 },
             connections: { browsers: 1, agents: 8, worker_threads: 128, worker_threads_busy: 0 },
             process: { cpu_pct: 3, rss_mb: 736, ram_pct: 9 }, hostname: 'mgr', ram_total_mb: 8000 },
  services: [
    { name: 'alarm_engine', ok: true, version: 'v1', uptime_s: 50, tls: { enabled: true, active: true },
      process: { cpu_pct: 6, rss_mb: 631, ram_pct: 8 } },
    { name: 'influxdb', ok: true, state: 'connected', version: 'v2.9.1' },
  ],
  agents: [], data_flow: {},
  flow: { agent_pushes_per_s: 1, ae_ingest_points_per_s: 70, influx_writes_per_s: 80, history_req_per_s: 2,
          anon_req_per_min: 0, anon_refused_per_min: 0, anon_budget_per_min: 600 },
  agent_update: { latest: 'v1', outdated: 0, hostnames: [] },
  warnings: [],
  recent: [
    { ts: iso(5), k: 'ok', title: '8 agents updated', detail: '', actor: 'llmadmin' },
    { ts: iso(40), k: 'ok', title: 'Alarm Engine restarted', detail: '', actor: 'llmadmin' },
    { ts: iso(41), k: 'ok', title: 'Manager restarted', detail: 'v2026.10.14-8', actor: 'llmadmin' },
    { ts: iso(90), k: 'warn', title: 'Request flood on the open routes', detail: '3210/min, 2610 refused', actor: '' },
    { ts: iso(200), k: 'ok', title: 'Backup ran', detail: '', actor: 'llmadmin' },
    { ts: iso(30 * 60), k: 'ok', title: 'Forecast run', detail: '', actor: '' },
  ],
};

describe('Recent view (default)', () => {
  test('label, six rows newest first, grouped title, actor, warn dot, All events link', () => {
    const doc = card(HEALTHY).document;
    expect(doc.getElementById('adminHealthColLabel').textContent).toBe('Recent');
    expect(doc.getElementById('adminHealthColEm').textContent).toBe('last 24 h');
    const rows = [...doc.querySelectorAll('.hc-evr')];
    expect(rows.length).toBe(6);
    expect(rows[0].querySelector('.t b').textContent).toBe('8 agents updated');
    expect(rows[0].querySelector('.by').textContent).toContain('llmadmin');
    expect(rows[3].querySelector('.dot').className).toContain('warn');
    expect(rows[3].querySelector('.m').textContent).toBe('3210/min, 2610 refused');
    expect(doc.querySelector('.hc-ev-all').textContent).toBe('All events');
    expect(doc.getElementById('adminHealthView').hidden).toBe(false);
  });
  test('a row from an earlier calendar day carries its date under the time', () => {
    const doc = card(HEALTHY).document;
    const last = doc.querySelectorAll('.hc-evr')[5];
    expect(last.querySelector('time small')).not.toBeNull();
    expect(doc.querySelectorAll('.hc-evr')[0].querySelector('time small')).toBeNull();
  });
  test('empty recent says so', () => {
    const doc = card({ ...HEALTHY, recent: [] }).document;
    expect(doc.querySelector('.hc-ev-none').textContent).toBe('Nothing in the last 24 h');
  });
  test('view is recent on a fresh load and nothing is stored', () => {
    const win = card(HEALTHY);
    expect(win.HealthView.view()).toBe('recent');
    expect(win.localStorage.length).toBe(0);
  });
});

describe('budget bar', () => {
  test('idle at 0, number 0 / 600', () => {
    const doc = card(HEALTHY).document;
    expect(doc.querySelector('.hc-bud .n').textContent).toBe('0 / 600');
    expect(doc.querySelector('.hc-bud .fi').className).toContain('idle');
  });
  test('412 of 600 is amber at 69 %', () => {
    const d = { ...HEALTHY, flow: { ...HEALTHY.flow, anon_req_per_min: 412 } };
    const doc = card(d).document;
    const fi = doc.querySelector('.hc-bud .fi');
    expect(fi.className).toContain('warn');
    expect(fi.style.width).toBe('69%');
    expect(doc.querySelector('.hc-bud .n').className).toContain('warn');
  });
  test('560 of 600 is red', () => {
    const d = { ...HEALTHY, flow: { ...HEALTHY.flow, anon_req_per_min: 560 } };
    expect(card(d).document.querySelector('.hc-bud .fi').className).toContain('crit');
  });
  test('budget off → no bar', () => {
    const d = { ...HEALTHY, flow: { ...HEALTHY.flow, anon_budget_per_min: 0 } };
    expect(card(d).document.querySelector('.hc-bud')).toBeNull();
  });
});

describe('warnings take the column', () => {
  test('a warning row hides the slider and restores the Warnings label; healing brings the view back', () => {
    const warn = { ...HEALTHY, warnings: ['alarm engine unreachable: ConnectTimeout'] };
    const win = card(warn);
    const doc = win.document;
    expect(doc.getElementById('adminHealthColLabel').textContent).toBe('Warnings');
    expect(doc.getElementById('adminHealthView').hidden).toBe(true);
    expect(doc.querySelectorAll('#adminHealthWarnings .w').length).toBe(1);
    expect(doc.querySelector('.hc-evr')).toBeNull();
    win.HealthView.render(HEALTHY, null);
    expect(doc.getElementById('adminHealthColLabel').textContent).toBe('Recent');
    expect(doc.querySelectorAll('.hc-evr').length).toBe(6);
  });
  test('the slider click switches the view and the pressed button', () => {
    const win = card(HEALTHY);
    const doc = win.document;
    doc.querySelector('#adminHealthView [data-view="capacity"]').click();
    expect(win.HealthView.view()).toBe('capacity');
    expect(doc.querySelector('#adminHealthView [data-view="capacity"]').getAttribute('aria-pressed')).toBe('true');
    expect(doc.querySelector('#adminHealthView [data-view="recent"]').getAttribute('aria-pressed')).toBe('false');
    expect(doc.getElementById('adminHealthColLabel').textContent).toBe('Capacity');
  });
});

describe('fmtEventTime', () => {
  test('same day → clock only; earlier day → short date', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const now = new Date(2026, 9, 14, 18, 0).getTime();
    expect(v.fmtEventTime(new Date(2026, 9, 14, 17, 35).toISOString(), now)).toEqual({ clock: '5:35 PM', day: '' });
    expect(v.fmtEventTime(new Date(2026, 9, 13, 21, 24).toISOString(), now)).toEqual({ clock: '9:24 PM', day: 'Oct 13' });
    expect(v.fmtEventTime('garbage', now)).toEqual({ clock: '—', day: '' });
  });
});

describe('Capacity view', () => {
  const EXTRA = { influx_bytes: 2.8e9, influx_rss_mb: 841, influx_host: 'mgr', disk_pct: 56, influx_ram_total_mb: 8000 };
  test('rows: manager and AE CPU/memory, InfluxDB disk/memory, limits; no open-route row', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const rows = v.capacityRows(HEALTHY, EXTRA);
    expect(rows.map(r => `${r.group}/${r.label}`)).toEqual([
      'Manager/CPU', 'Manager/Memory', 'Alarm Engine/CPU', 'Alarm Engine/Memory',
      'InfluxDB/On disk', 'InfluxDB/Memory', 'Limits/Live streams', 'Limits/Worker threads busy']);
    expect(rows[0].num).toBe('3 %'); expect(rows[0].pct).toBe(3);
    expect(rows[1].num).toBe('736 MB · 9 % of RAM');
    expect(rows[4].num).toBe('2.8 GB · disk 56 % used'); expect(rows[4].pct).toBe(56);
    expect(rows[5].num).toBe('841 MB · 11 % of RAM'); expect(rows[5].pct).toBe(11);
    expect(rows[6].num).toBe('0 / 40'); expect(rows[7].num).toBe('0 / 128');
    expect(rows.some(r => /open-route/i.test(r.label))).toBe(false);
  });
  test('thresholds: 60 % warn, 90 % crit', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const d = { ...HEALTHY, manager: { ...HEALTHY.manager, process: { cpu_pct: 61, rss_mb: 1, ram_pct: 95 } } };
    const rows = v.capacityRows(d, EXTRA);
    expect(rows[0].cls).toBe('warn'); expect(rows[1].cls).toBe('crit');
  });
  test('AE without process → dashes', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const d = { ...HEALTHY, services: [{ name: 'alarm_engine', ok: true }, HEALTHY.services[1]] };
    const rows = v.capacityRows(d, null);
    expect(rows[2].num).toBe('—'); expect(rows[2].pct).toBe(0); expect(rows[2].cls).toBe('idle');
    expect(rows[4].num).toBe('—'); expect(rows[5].num).toBe('—');
    expect(v.capacityHtml(d, null)).not.toMatch(/NaN/);
  });
  test('null metrics take the dash / idle path, never 0', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const d = { ...HEALTHY, manager: { ...HEALTHY.manager, process: { cpu_pct: null, rss_mb: null, ram_pct: null } } };
    const rows = v.capacityRows(d, { influx_bytes: 2.8e9, influx_rss_mb: null, influx_host: 'mgr', disk_pct: null });
    expect(rows[0].num).toBe('—'); expect(rows[0].cls).toBe('idle');
    expect(rows[1].num).toBe('—'); expect(rows[1].cls).toBe('idle');
    expect(rows[4].num).toBe('2.8 GB'); expect(rows[4].cls).toBe('idle'); expect(rows[4].pct).toBe(0);
    expect(rows[5].num).toBe('—');
  });
  test('a missing side of a limit shows a dash, never undefined', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const d = { ...HEALTHY, manager: { ...HEALTHY.manager, streams: { limit: 40 }, connections: { worker_threads: 128 } } };
    const rows = v.capacityRows(d, null);
    expect(rows[6].num).toBe('—'); expect(rows[6].cls).toBe('idle');
    expect(rows[7].num).toBe('—'); expect(rows[7].cls).toBe('idle');
    expect(v.capacityHtml(d, null)).not.toMatch(/undefined/);
  });
  test('Influx memory on another host shows the number alone', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const rows = v.capacityRows(HEALTHY, { ...EXTRA, influx_host: 'other', influx_ram_total_mb: null });
    expect(rows[5].num).toBe('841 MB'); expect(rows[5].cls).toBe('idle');
  });
  test("Influx memory uses the Influx host's own RAM total", () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const rows = v.capacityRows(HEALTHY, { ...EXTRA, influx_host: 'other', influx_ram_total_mb: 16000 });
    expect(rows[5].num).toBe('841 MB · 5 % of RAM'); expect(rows[5].pct).toBe(5);
  });
  test('renders groups and the footer', () => {
    const doc = card(HEALTHY, 'HealthView.setView("capacity")').document;
    expect([...doc.querySelectorAll('.hc-cg')].map(e => e.textContent)).toEqual(['Manager', 'Alarm Engine', 'InfluxDB', 'Limits']);
    expect(doc.querySelector('.hc-cap-foot').textContent).toMatch(/^Amber at 60 %/);
  });
});

describe('Trends view', () => {
  const pt = (i, v) => ({ timestamp: new Date(Date.now() - (60 - i) * 60000).toISOString(), value: v });
  const RAW = {
    anon: Array.from({ length: 60 }, (_, i) => pt(i, i === 44 ? 3210 : 6)),
    refused: Array.from({ length: 60 }, (_, i) => pt(i, i === 44 ? 2610 : 0)),
    api: Array.from({ length: 60 }, (_, i) => pt(i, 2)),
    ing: Array.from({ length: 60 }, (_, i) => pt(i, 1.5)),
    influx: Array.from({ length: 60 }, (_, i) => pt(i, 10)),
    rule: Array.from({ length: 60 }, (_, i) => pt(i, 6)),
  };
  test('five sparklines, the amber segment and the peak label', () => {
    const win = runHarness({ sources: [adminSrc, healthSrc], bodyHtml: `<div id="adminTab">${CARD}</div>` });
    const v = win.HealthView;
    const html = v.trendsHtml(v.trendSeries(RAW));
    win.document.getElementById('adminHealthWarnings').innerHTML = html;
    const doc = win.document;
    expect(doc.querySelectorAll('.hc-sp').length).toBe(5);
    expect(doc.querySelectorAll('svg[data-sp] path.line').length).toBe(5);
    expect(doc.querySelector('svg[data-sp="anon"] path.seg').getAttribute('d'))
      .toMatch(/^M[\d.]+,[\d.]+ L[\d.]+,[\d.]+ L[\d.]+,[\d.]+$/);
    expect(doc.querySelector('.hc-sp .n.warn').textContent).toMatch(/^peak 3210 at \d+:\d\d [AP]M$/);
    expect(doc.querySelectorAll('.hc-sp .h .l')[1].textContent).toBe('Manager API latency');
    expect(doc.querySelectorAll('.hc-sp .n')[1].textContent).toBe('2.0 ms now');
  });
  test('refused minutes are matched by timestamp, not by array position', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const html = v.trendsHtml(v.trendSeries({ ...RAW, refused: RAW.refused.slice(10) }));
    const seg = html.match(/<svg data-sp="anon"[\s\S]*?class="seg" d="([^"]+)"/)[1];
    const line = html.match(/<svg data-sp="anon"[\s\S]*?class="line" d="([^"]+)"/)[1];
    const peakX = line.split(' ')[44].slice(1).split(',')[0];
    expect(seg.split(' ').length).toBe(3);
    expect(seg.split(' ')[1]).toBe(`L${peakX},${line.split(' ')[44].split(',')[1]}`);
  });
  test('a failed load is cached: six requests, none on the next render', async () => {
    const win = card(HEALTHY, 'window.__n = 0; window.fetch = () => { window.__n++; return Promise.reject(new Error("down")); }; HealthView.setView("trends");');
    await new Promise(r => setTimeout(r, 0));
    expect(win.__n).toBe(6);
    expect(win.document.getElementById('adminHealthWarnings').textContent).toContain('Not available');
    win.HealthView.render(HEALTHY, null);
    await new Promise(r => setTimeout(r, 0));
    expect(win.__n).toBe(6);
  });
  test('sparkPath scales to the box and ends at the right edge', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    const d = v.sparkPath([0, 5, 10], 240, 30);
    expect(d.startsWith('M0,')).toBe(true);
    expect(d).toMatch(/L240,/);
  });
  test('no series yet → Loading…; failed load → Not available', () => {
    const v = runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
    expect(v.trendsHtml(null)).toContain('Loading…');
    expect(v.trendsHtml({ error: true })).toContain('Not available');
  });
  test('picking Trends fetches the six series once', async () => {
    const win = card(HEALTHY, 'window.__urls = []; window.fetch = u => { window.__urls.push(String(u)); return Promise.resolve({ ok: true, json: () => Promise.resolve([]) }); }; HealthView.setView("trends");');
    await new Promise(r => setTimeout(r, 0));
    const urls = win.__urls;
    expect(urls.filter(u => u.includes('/api/alarm/metrics/manager_self_monitor/')).length).toBe(6);
    expect(urls.some(u => u.includes('rule_eval_cycle_ms'))).toBe(true);
    expect(urls.some(u => u.includes('system/cpu_total'))).toBe(false);
    win.HealthView.setView('recent'); win.HealthView.setView('trends');
    await new Promise(r => setTimeout(r, 0));
    expect(win.__urls.length).toBe(6);
  });
});
