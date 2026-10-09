// #1235 / #1233: System Health services column — status pills and the detail line.
// Loads the real admin-health.js in jsdom and renders svcRows through svcRowHtml.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'js', 'admin-health.js'), 'utf8');

function view(payload, fn) {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(src);
  inject(`window.__T = HealthView.${fn}(${JSON.stringify(payload)});`);
  return dom.window.__T;
}

function rows(payload) {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(src);
  inject(`window.__T = HealthView.svcRows(${JSON.stringify(payload)}).map(r => [r, HealthView.svcRowHtml(r)]);`);
  const div = dom.window.document.createElement('div');
  return dom.window.__T.map(([r, html]) => {
    div.innerHTML = html;
    const pill = div.querySelector('.up .pill');
    const d = div.querySelector('.d');
    return { name: r.n, pill: pill.textContent, pillCls: pill.className, detail: d ? d.textContent : null,
      detailCls: d ? d.className : null };
  });
}

const base = { manager: { version: 'v1', uptime_s: 240 }, restart_pending: [] };

describe('services column pills', () => {
  test('healthy: uptime pills, green connected for InfluxDB, no detail lines', () => {
    const r = rows({ ...base, services: [
      { name: 'alarm_engine', ok: true, state: 'ok', version: 'v2', uptime_s: 240 },
      { name: 'influxdb', ok: true, state: 'connected', version: '2.7' },
    ] });
    expect(r.map(x => x.pill)).toEqual(['up4m', 'up4m', 'connected']);
    expect(r[2].pillCls).toBe('pill ok');
    expect(r.every(x => x.detail === null)).toBe(true);
  });

  test('InfluxDB down: short red pill, full error on the detail line, engine degraded (amber)', () => {
    const r = rows({ ...base, services: [
      { name: 'alarm_engine', ok: true, state: 'degraded', version: 'v2', uptime_s: 240 },
      { name: 'influxdb', ok: false, state: 'unreachable: ConnectionError' },
    ] });
    expect(r[1].pill).toBe('degraded');
    expect(r[1].pillCls).toBe('pill warn');
    expect(r[1].detail).toMatch(/history is not being stored/);
    expect(r[2].pill).toBe('unreachable');
    expect(r[2].pillCls).toBe('pill crit');
    expect(r[2].detail).toBe('unreachable: ConnectionError');
    expect(r[2].detailCls).toBe('d crit');
  });

  test('auth_failed state reads as words with no detail line', () => {
    const r = rows({ ...base, services: [
      { name: 'alarm_engine', ok: true, state: 'ok', version: 'v2', uptime_s: 10 },
      { name: 'influxdb', ok: false, state: 'auth_failed' },
    ] });
    expect(r[2].pill).toBe('auth failed');
    expect(r[2].detail).toBeNull();
  });

  test('engine unreachable: red pills for both, probe error on the engine detail line', () => {
    const r = rows({ ...base, services: [
      { name: 'alarm_engine', ok: false, error: 'ConnectionError' },
      { name: 'influxdb', ok: false, via: 'alarm_engine (unreachable)' },
    ] });
    expect(r[1].pill).toBe('unreachable');
    expect(r[1].detail).toBe('ConnectionError');
    expect(r[2].pill).toBe('unreachable');
    expect(r[2].pillCls).toBe('pill crit');
  });
});

describe('data-flow during an InfluxDB outage', () => {
  const outage = { ...base, flow: { influx_writes_per_s: 0 }, services: [
    { name: 'alarm_engine', ok: true, state: 'degraded', version: 'v2', uptime_s: 240 },
    { name: 'influxdb', ok: false, state: 'unreachable: ConnectionError' },
  ] };

  test('engine up but InfluxDB down: the write edge is crit, not off', () => {
    const e = view(outage, 'edgeStates');
    expect(e.eAeIn).toMatchObject({ state: 'crit', label: 'no writes' });
  });

  test('the InfluxDB node shows the short state word', () => {
    expect(view(outage, 'nodeSubs').nIn).toBe('unreachable');
    expect(view({ ...base, services: [
      { name: 'alarm_engine', ok: false, error: 'ConnectionError' },
      { name: 'influxdb', ok: false, via: 'alarm_engine (unreachable)' },
    ] }, 'nodeSubs').nIn).toBe('unreachable');
  });
});
