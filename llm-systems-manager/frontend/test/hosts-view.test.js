// #1432 phase 4c-1: All-hosts view transforms in js/lib/hosts-view.js.
// History rows carry ISO `ts` strings, as /api/history?agent=<id> returns them.
import { describe, it, expect } from 'vitest';
import HV from '../js/lib/hosts-view.js';
import OV from '../js/lib/overall-view.js';

const NOW = Date.parse('2026-11-16T12:00:30+00:00');
const iso = (msAgo) => new Date(NOW - msAgo).toISOString();
const MIN = 60000;

describe('METRICS and shortName', () => {
  it('lists the five charted metrics in order', () => {
    expect(HV.METRICS.map(m => m.key)).toEqual(['cpu_total', 'ram_percent', 'disk_root_pct', 'net_recv', 'io_write']);
    expect(HV.METRICS[0]).toEqual({ key: 'cpu_total', card: 'ah-cpu', title: 'CPU', unit: '%', ymax: 100 });
    expect(HV.METRICS[4]).toEqual({ key: 'io_write', card: 'ah-io', title: 'Disk writes', unit: 'B/s' });
  });
  it('drops a trailing .local only', () => {
    expect(HV.shortName('tower.local')).toBe('tower');
    expect(HV.shortName('local.box')).toBe('local.box');
    expect(HV.shortName(null)).toBe('');
  });
});

describe('hostList', () => {
  const payload = { hosts: [
    { agent_id: 'a', hostname: 'alpha.local', providers: ['lms', 'llama'], online: true, age_s: 2.5 },
    { agent_id: 'b', hostname: 'beta', providers: [], online: false, age_s: null },
    { hostname: 'ghost', providers: ['llama'], online: true, age_s: 1 },
    { agent_id: 'c', hostname: 'gamma', providers: ['vllm', 'lms'], online: true, age_s: 0 },
    { agent_id: 'd', hostname: 'delta', providers: ['lms'], online: true, age_s: 4 },
  ] };
  it('maps providers to pages and drops entries without agent_id', () => {
    const out = HV.hostList(payload);
    expect(out.map(h => h.id)).toEqual(['a', 'b', 'c', 'd']);
    expect(out[0]).toEqual({ id: 'a', name: 'alpha', hostname: 'alpha.local', providers: ['lms', 'llama'], online: true, ageS: 2.5, page: 'llamacpp' });
    expect(out[1].page).toBeNull();
    expect(out[1].ageS).toBeNull();
    expect(out[2].page).toBe('lmstudio');
    expect(out[3].page).toBe('lmstudio');
  });
  it('returns [] for a missing payload', () => {
    expect(HV.hostList(null)).toEqual([]);
    expect(HV.hostList({})).toEqual([]);
  });
});

describe('nowRows', () => {
  const hosts = HV.hostList({ hosts: [
    { agent_id: 'old', hostname: 'old', providers: [], online: false, age_s: 400 },
    { agent_id: 'gap', hostname: 'gap', providers: ['llama'], online: true, age_s: 1 },
    { agent_id: 'none', hostname: 'none', providers: [], online: true, age_s: 1 },
  ] });
  const hist = {
    old: [{ ts: iso(6 * MIN), cpu_total: 50, ram_percent: 40, disk_root_pct: 30 }],
    gap: [
      { ts: iso(1 * MIN), cpu_total: 12.5, ram_percent: 60, disk_root_pct: 70 },
      { ts: iso(0), cpu_total: null, ram_percent: 61 },
    ],
  };
  it('uses only points from the last 5 minutes, newest non-null per key', () => {
    const rows = HV.nowRows(hosts, hist, NOW);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toEqual({ id: 'old', name: 'old', online: false, page: null, cpu: null, ram: null, disk: null });
    expect(rows[1]).toEqual({ id: 'gap', name: 'gap', online: true, page: 'llamacpp', cpu: 12.5, ram: 61, disk: 70 });
    expect(rows[2]).toEqual({ id: 'none', name: 'none', online: true, page: null, cpu: null, ram: null, disk: null });
  });
  it('ignores points stamped more than 60 s ahead, keeps ones within 60 s', () => {
    const fut = { gap: [
      { ts: iso(-2 * MIN), cpu_total: 99, ram_percent: 99, disk_root_pct: 99 },
      { ts: iso(-30000), cpu_total: 7 },
      { ts: iso(1 * MIN), cpu_total: 5, ram_percent: 50, disk_root_pct: 60 },
    ] };
    const row = HV.nowRows(hosts, fut, NOW)[1];
    expect([row.cpu, row.ram, row.disk]).toEqual([7, 50, 60]);
  });
});

describe('compare', () => {
  const ids = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'h7', 'hnull'];
  const hosts = HV.hostList({ hosts: ids.map(id => ({ agent_id: id, hostname: id, providers: [], online: true, age_s: 1 })) });
  const means = { h1: 10, h2: 70, h3: 30, h4: 90, h5: 50, h6: 20, h7: 80 };
  const hist = { hnull: [{ ts: iso(2 * MIN), cpu_total: null }, { ts: iso(3 * MIN), cpu_total: null }] };
  Object.entries(means).forEach(([id, m]) => {
    hist[id] = [{ ts: iso(10 * MIN), cpu_total: m - 5 }, { ts: iso(20 * MIN), cpu_total: m + 5 }];
  });

  it('builds one series per host with data and picks the 5 busiest', () => {
    const out = HV.compare(hosts, hist, 'cpu_total', 6, NOW);
    expect(out.series).toHaveLength(7);
    expect(out.series.map(s => s.id)).toEqual(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'h7']);
    expect(out.busiest).toEqual(['h4', 'h7', 'h2', 'h5', 'h3']);
    const slot = Object.fromEntries(out.series.map(s => [s.id, s.slot]));
    expect([slot.h1, slot.h2, slot.h3, slot.h4, slot.h5, slot.h6, slot.h7]).toEqual([1, 2, 3, 4, 5, 1, 2]);
    out.series.forEach(s => expect(s.values).toHaveLength(360));
  });
  it('takes each host\'s slot from slotOf, whatever its rank', () => {
    const fixed = { h1: 3, h2: 5, h3: 1, h4: 2, h5: 4, h6: 9, h7: 1 };
    const out = HV.compare(hosts, hist, 'cpu_total', 6, NOW, (id) => fixed[id]);
    const slot = Object.fromEntries(out.series.map(s => [s.id, s.slot]));
    expect([slot.h1, slot.h2, slot.h3, slot.h4, slot.h5, slot.h7]).toEqual([3, 5, 1, 2, 4, 1]);
    expect(slot.h6).toBe(1);
    const busier = { ...hist, h1: hist.h1.map(r => ({ ...r, cpu_total: 99 })) };
    expect(HV.compare(hosts, busier, 'cpu_total', 6, NOW, (id) => fixed[id]).series.find(s => s.id === 'h1').slot).toBe(3);
  });
  it('puts each point in its bucket and leaves empty buckets null', () => {
    const out = HV.compare(hosts, hist, 'cpu_total', 6, NOW);
    const h4 = out.series.find(s => s.id === 'h4');
    expect(h4.name).toBe('h4');
    expect(h4.values[359 - 10]).toBe(85);
    expect(h4.values[359 - 20]).toBe(95);
    expect(h4.values[359]).toBeNull();
    expect(h4.values.filter(v => v != null)).toHaveLength(2);
  });
  it('averages points sharing a bucket', () => {
    const h = [{ ts: iso(0), cpu_total: 10 }, { ts: iso(10000), cpu_total: 20 }];
    const out = HV.compare(hosts.slice(0, 1), { h1: h }, 'cpu_total', 6, NOW);
    expect(out.series[0].values[359]).toBe(15);
  });
  it('uses the Overall bucket width and labels for the range', () => {
    const six = HV.compare(hosts, hist, 'cpu_total', 6, NOW);
    const day = HV.compare(hosts, hist, 'cpu_total', 24, NOW);
    expect(six.labels).toHaveLength(360);
    expect(day.labels).toHaveLength(288);
    const b = OV.rangeBucketMs(24), end = Math.floor(NOW / b) * b;
    expect(day.labels).toEqual(OV.bucketLabels(end - 287 * b, 288, b));
    day.series.forEach(s => expect(s.values).toHaveLength(288));
  });
  it('treats an unknown range as 24 h for both span and bucket width', () => {
    const odd = HV.compare(hosts, hist, 'cpu_total', 7, NOW);
    expect(odd).toEqual(HV.compare(hosts, hist, 'cpu_total', 24, NOW));
    expect(odd.labels).toHaveLength(288);
  });
  it('ignores points outside the range and handles no hosts', () => {
    const out = HV.compare(hosts.slice(0, 1), { h1: [{ ts: iso(7 * 60 * MIN), cpu_total: 50 }] }, 'cpu_total', 6, NOW);
    expect(out.series).toEqual([]);
    expect(out.busiest).toEqual([]);
    expect(HV.compare([], {}, 'cpu_total', 6, NOW).labels).toHaveLength(360);
  });
});

describe('mergeRows', () => {
  it('de-dupes equal ts with fresh winning, sorts ascending, drops below cutoff', () => {
    const old = [
      { ts: iso(3 * MIN), cpu_total: 1 },
      { ts: iso(10 * MIN), cpu_total: 0 },
      { ts: iso(2 * MIN), cpu_total: 2 },
    ];
    const fresh = [{ ts: iso(2 * MIN), cpu_total: 22 }, { ts: iso(1 * MIN), cpu_total: 3 }];
    const out = HV.mergeRows(old, fresh, NOW - 5 * MIN);
    expect(out.map(r => r.cpu_total)).toEqual([1, 22, 3]);
    expect(out.map(r => r.ts)).toEqual([iso(3 * MIN), iso(2 * MIN), iso(1 * MIN)]);
  });
  it('treats missing lists as empty', () => {
    expect(HV.mergeRows(null, undefined, 0)).toEqual([]);
  });
});

describe('formatters', () => {
  it('fmtPct', () => {
    expect(HV.fmtPct(null)).toBe('—');
    expect(HV.fmtPct(5.25)).toBe('5.3%');
    expect(HV.fmtPct(42.4)).toBe('42%');
    expect(HV.fmtPct(9.96)).toBe('10%');
  });
  it('fmtRate', () => {
    expect(HV.fmtRate(null)).toBe('—');
    expect(HV.fmtRate(1536)).toBe('1.5 KB/s');
    expect(HV.fmtRate(512)).toBe('512 B/s');
    expect(HV.fmtRate(3 * 1024 * 1024)).toBe('3.0 MB/s');
    expect(HV.fmtRate(2.5 * 1024 ** 3)).toBe('2.5 GB/s');
    expect(HV.fmtRate(1023.7)).toBe('1.0 KB/s');
    expect(HV.fmtRate(1048575)).toBe('1.0 MB/s');
  });
});

describe('compare fills short gaps', () => {
  const hosts = HV.hostList({ hosts: [{ agent_id: 'a', hostname: 'a', providers: [], online: true, age_s: 1 }] });
  // One point every stepMin minutes over the last spanMin minutes, value 40 + its index.
  const every = (stepMin, spanMin) => {
    const rows = [];
    for (let m = spanMin; m >= 0; m -= stepMin) rows.push({ ts: iso(m * MIN), ram_percent: 40 + m });
    return rows;
  };
  const inner = (values) => {
    const first = values.findIndex(v => v != null), last = values.length - 1 - [...values].reverse().findIndex(v => v != null);
    return values.slice(first, last + 1);
  };

  it('24 h: 10-minute points in 5-minute buckets draw without zero dips', () => {
    const v = HV.compare(hosts, { a: every(10, 600) }, 'ram_percent', 24, NOW).series[0].values;
    const mid = inner(v);
    expect(mid.length).toBeGreaterThan(100);
    expect(mid.every(x => typeof x === 'number' && x > 0)).toBe(true);
  });

  it('6 h: 2-minute points in 1-minute buckets draw without zero dips', () => {
    const v = HV.compare(hosts, { a: every(2, 300) }, 'ram_percent', 6, NOW).series[0].values;
    const mid = inner(v);
    expect(mid.length).toBeGreaterThan(250);
    expect(mid.every(x => typeof x === 'number' && x > 0)).toBe(true);
  });

  it('a filled bucket holds the previous value', () => {
    const rows = [{ ts: iso(4 * MIN), ram_percent: 10 }, { ts: iso(0), ram_percent: 20 }];
    const v = HV.compare(hosts, { a: rows }, 'ram_percent', 6, NOW).series[0].values;
    expect(v.slice(-5)).toEqual([10, 10, 10, 10, 20]);
  });

  it('up to 3 empty buckets at either end take the nearest value', () => {
    const all = Array.from({ length: 360 }, (_, k) => ({ ts: iso((359 - k) * MIN), ram_percent: 50 + (k % 2) }));
    const rows = all.slice(3, 358);
    const v = HV.compare(hosts, { a: rows }, 'ram_percent', 6, NOW).series[0].values;
    expect(v.length).toBe(360);
    expect(v.slice(0, 4)).toEqual([51, 51, 51, 51]);
    expect(v.slice(-3)).toEqual([51, 51, 51]);
    expect(v.every(x => typeof x === 'number')).toBe(true);
  });

  it('a 10-bucket gap, and leading and trailing gaps longer than 3, stay empty', () => {
    const rows = [{ ts: iso(30 * MIN), ram_percent: 10 }, { ts: iso(19 * MIN), ram_percent: 20 }, { ts: iso(5 * MIN), ram_percent: 30 }];
    const v = HV.compare(hosts, { a: rows }, 'ram_percent', 6, NOW).series[0].values;
    const n = v.length;
    expect(v.slice(n - 30, n - 20)).toEqual([null, null, null, null, null, null, null, null, null, null]);
    expect(v[n - 31]).toBe(10);
    expect(v[n - 20]).toBe(20);
    expect(v.slice(0, n - 31).every(x => x === null)).toBe(true);
    expect(v.slice(n - 5).every(x => x === null)).toBe(true);
  });
});
