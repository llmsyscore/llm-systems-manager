import test from 'node:test';
import assert from 'node:assert/strict';
import { load } from './load.mjs';

const s = load(['demo-fleet.js', 'demo-series.js']);
const { DemoFleet: F, DemoSeries: S } = s;
const METRICS = ['tok_s', 'gpu_util', 'vram_used_gb', 'power_w', 'cpu_pct', 'temp_c'];
const T0 = 1_800_000_000;

test('fleet shape, sample addresses and hostnames', () => {
  assert.deepEqual(Array.from(F.machines.map((m) => m.hostname)), ['atlas.example', 'forge.example', 'studio.example', 'quill.example']);
  for (const m of F.machines) {
    assert.match(m.ip, /^192\.0\.2\.\d+$/);
    assert.ok(m.hostname.endsWith('.example'));
    assert.equal(F.byId(m.id), m);
  }
  assert.deepEqual(Array.from(F.users.map((u) => u.username)), ['alex', 'sam']);
  assert.equal(F.byId('nope'), null);
});

test('same inputs give same output', () => {
  for (const m of F.machines) for (const k of METRICS) {
    assert.equal(S.value(m.id, k, T0 + 123), S.value(m.id, k, T0 + 123));
  }
  assert.deepEqual(S.range('atlas', 'tok_s', T0, T0 + 3600), S.range('atlas', 'tok_s', T0, T0 + 3600));
});

test('values stay in range and move smoothly over 24 h', () => {
  for (const m of F.machines) for (const k of METRICS) {
    const r = S.ranges(m.id, k);
    const span = r.max - r.min;
    for (let t = T0; t < T0 + 86400; t += 60) {
      const v = S.value(m.id, k, t);
      assert.ok(v >= r.min && v <= r.max, `${m.id} ${k} ${v}`);
      assert.ok(Math.abs(v - S.value(m.id, k, t + 1)) <= 0.15 * span + 1e-9, `${m.id} ${k} step at ${t}`);
    }
  }
});

test('busy cycle and busy/idle levels', () => {
  const DAY0 = Math.floor(T0 / 86400) * 86400;
  for (const m of F.machines) {
    let day = 0, night = 0, dayN = 0, nightN = 0;
    for (let d = 0; d < 7; d++) {
      for (let t = DAY0 + d * 86400; t < DAY0 + (d + 1) * 86400; t += 20) {
        const dt = new Date(t * 1000), h = dt.getHours() + dt.getMinutes() / 60;
        if (h >= 9 && h < 23) { dayN++; if (S.busy(m.id, t)) day++; }
        else if (h < 6) { nightN++; if (S.busy(m.id, t)) night++; }
      }
    }
    assert.ok(day / dayN > 0.25 && day / dayN < 0.7, `day ${day / dayN}`);
    assert.ok(night / nightN < 0.2 && night / nightN < day / dayN / 2, `night ${night / nightN}`);
  }
  for (const m of F.machines) {
    const lens = [];
    let run = 0;
    for (let t = DAY0; t < DAY0 + 2 * 86400; t++) {
      if (S.busy(m.id, t)) run++; else if (run) { lens.push(run); run = 0; }
    }
    assert.ok(lens.length > 8 && new Set(lens).size > 6, 'bursts vary in length');
    assert.ok(Math.min(...lens) >= 1 && Math.max(...lens) <= 900);
  }
  for (const m of F.machines) {
    let idle = 0, mid = 0;
    for (let t = DAY0; t < DAY0 + 86400; t++) {
      if (!S.busy(m.id, t)) { assert.equal(S.value(m.id, 'tok_s', t), 0); idle++; }
      else if (S.busy(m.id, t - 40) && S.busy(m.id, t + 40) && S.busy(m.id, t - 1)) {
        assert.ok(S.value(m.id, 'tok_s', t) >= 35); mid++;
      }
    }
    assert.ok(idle > 0 && mid > 0);
  }
  let hi = 0, lo = 1e9;
  for (let x = DAY0 + 10 * 3600; x < DAY0 + 14 * 3600; x++) {
    const v = S.value('forge', 'gpu_util', x);
    hi = Math.max(hi, v); lo = Math.min(lo, v);
  }
  assert.ok(hi >= 70 && lo <= 8);
});

test('range() is bounded for any span', () => {
  const r = S.range('atlas', 'power_w', T0 - 30 * 86400, T0);
  assert.ok(r.length <= 300 && r.length > 1);
  assert.ok(S.range('atlas', 'power_w', T0, T0 + 5, 300).length <= 6);
  assert.ok(S.range('atlas', 'power_w', T0, T0 + 1e9, 50).length <= 50);
  assert.equal(S.range('atlas', 'power_w', T0 + 10, T0).length, 0);
  assert.equal(S.range('atlas', 'power_w', T0, T0 + 100, 1).length, 1);
  const a = S.range('atlas', 'power_w', T0, T0 + 3600), b = S.range('atlas', 'power_w', T0 + 5, T0 + 3605);
  assert.equal(a[a.length - 1][0], T0 + 3600);
  const bm = new Map(b.slice(0, -1).map(([t, v]) => [t, v]));
  const shared = a.slice(0, -1).filter(([t]) => bm.has(t));
  assert.ok(shared.length > a.length - 5);
  for (const [t, v] of shared) assert.equal(bm.get(t), v);
});

test('value() with a missing or non-finite time gives the metric minimum', () => {
  for (const m of F.machines) for (const k of METRICS) {
    const r = S.ranges(m.id, k);
    for (const t of [NaN, undefined, Infinity, null, 'x']) assert.equal(S.value(m.id, k, t), r.min, `${m.id} ${k} ${t}`);
  }
});

test('bursts run across half-hour slot edges without a dip there', () => {
  const DAY0 = Math.floor(T0 / 86400) * 86400;
  let crossing = 0;
  for (const m of F.machines) {
    for (let edge = DAY0; edge < DAY0 + 3 * 86400; edge += 1800) {
      if (S.busy(m.id, edge - 60) && S.busy(m.id, edge + 60) && S.busy(m.id, edge)) {
        crossing++;
        assert.ok(S.value(m.id, 'tok_s', edge) > 0, `${m.id} dips at ${edge}`);
      }
    }
  }
  assert.ok(crossing > 5, `only ${crossing} bursts cross a slot edge`);
});

test('forge runs hot when busy (80-84 °C) and cooler when idle', () => {
  const DAY0 = Math.floor(T0 / 86400) * 86400;
  let busyN = 0;
  for (let t = DAY0; t < DAY0 + 86400; t += 7) {
    const v = S.value('forge', 'temp_c', t);
    if (S.busy('forge', t - 40) && S.busy('forge', t) && S.busy('forge', t + 40)) { assert.ok(v >= 80 && v <= 84, `${v}`); busyN++; }
    else if (!S.busy('forge', t)) assert.ok(v < 60, `${v}`);
  }
  assert.ok(busyN > 100);
});
