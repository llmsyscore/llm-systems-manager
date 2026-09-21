// #1031: pure display helpers for the Forecast sub-tab.
import { describe, it, expect } from 'vitest';
import FC from '../js/lib/forecast.js';

// Local-zone fixtures keep every date assertion runner-timezone independent.
const NOW = new Date(2026, 8, 18, 9, 30).getTime();
const sec = (y, m, d, h, mi) => new Date(y, m, d, h, mi || 0).getTime() / 1000;

const TODAY_03 = sec(2026, 8, 18, 3);
const TOMORROW_03 = sec(2026, 8, 19, 3);
const YESTERDAY_03 = sec(2026, 8, 17, 3);
const SEP_27_03 = sec(2026, 8, 27, 3);
const SEP_09 = sec(2026, 8, 9, 14);
const SEP_16 = sec(2026, 8, 16, 11);

const FINDING = {
  id: 'a1', check: 'disk_fill', title: 'Disk fill', host: 'rig-4090',
  subject: '/models', severity: 'warning', summary: '/models full in 9 days',
  detail: 'Disk use on /models has grown steadily. Likely cause: nightly autotune downloads',
  suggested_action: 'Remove unused quants.', since: SEP_09, predicted_at: SEP_27_03,
  rate: 18.64, unit: 'GB/day', confidence: 'high', graph: null,
  verified: 'tower+code', status: 'open', thread_id: null, dismissed_by: null,
};

describe('sevLabel', () => {
  it('maps the three severities', () => {
    expect(FC.sevLabel('critical')).toEqual({ text: 'Critical', short: 'crit', cls: 'crit', icon: '▲' });
    expect(FC.sevLabel('warning')).toEqual({ text: 'Warning', short: 'warn', cls: 'warn', icon: '●' });
    expect(FC.sevLabel('info')).toEqual({ text: 'Info', short: 'info', cls: 'info', icon: '' });
  });
  it('falls back to info for junk', () => {
    expect(FC.sevLabel(null).cls).toBe('info');
    expect(FC.sevLabel('nonsense').text).toBe('Info');
  });
});

describe('verifiedLabel', () => {
  it('names the three verification levels', () => {
    expect(FC.verifiedLabel('code')).toBe('Code');
    expect(FC.verifiedLabel('tower+code')).toBe('Tower + code');
    expect(FC.verifiedLabel('model')).toBe('Not verified');
  });
  it('treats unknown values as unverified', () => {
    expect(FC.verifiedLabel(null)).toBe('Not verified');
  });
});

describe('sortFindings / topThree', () => {
  const rows = [
    { id: 'i1', severity: 'info', predicted_at: TODAY_03, host: 'a' },
    { id: 'w2', severity: 'warning', predicted_at: null, host: 'b' },
    { id: 'c1', severity: 'critical', predicted_at: SEP_27_03, host: 'z' },
    { id: 'w1', severity: 'warning', predicted_at: TODAY_03, host: 'b' },
    { id: 'c0', severity: 'critical', predicted_at: TODAY_03, host: 'm' },
  ];
  it('orders critical → warning → info, soonest first, nulls last', () => {
    expect(FC.sortFindings(rows).map(r => r.id)).toEqual(['c0', 'c1', 'w1', 'w2', 'i1']);
  });
  it('breaks ties on host, with null hosts last', () => {
    const tied = [
      { id: 'x', severity: 'warning', predicted_at: TODAY_03, host: null },
      { id: 'y', severity: 'warning', predicted_at: TODAY_03, host: 'node-2' },
      { id: 'z', severity: 'warning', predicted_at: TODAY_03, host: 'mac' },
    ];
    expect(FC.sortFindings(tied).map(r => r.id)).toEqual(['z', 'y', 'x']);
  });
  it('does not mutate the input and tolerates junk', () => {
    const copy = rows.slice();
    FC.sortFindings(rows);
    expect(rows).toEqual(copy);
    expect(FC.sortFindings(null)).toEqual([]);
  });
  it('degrades on a truthy non-array without throwing', () => {
    expect(FC.sortFindings('oops')).toEqual([]);
    expect(FC.sortFindings({})).toEqual([]);
    expect(FC.sortFindings(7)).toEqual([]);
    expect(FC.topThree('oops')).toEqual([]);
    expect(FC.topThree({})).toEqual([]);
    expect(FC.topThree(42)).toEqual([]);
  });
  it('drops null and non-object entries', () => {
    const mixed = [null, { id: 'k', severity: 'info', predicted_at: null, host: 'a' }, 'x', 5];
    expect(FC.sortFindings(mixed).map(r => r.id)).toEqual(['k']);
  });
  it('topThree takes the first three in sorted order', () => {
    expect(FC.topThree(rows).map(r => r.id)).toEqual(['c0', 'c1', 'w1']);
    expect(FC.topThree([])).toEqual([]);
  });
});

describe('countLabel', () => {
  it('counts open findings', () => {
    expect(FC.countLabel({ enabled: true, findings: new Array(6).fill({}) })).toBe('6 ahead');
    expect(FC.countLabel({ enabled: true, findings: [{}] })).toBe('1 ahead');
  });
  it('says all clear on an empty enabled view', () => {
    expect(FC.countLabel({ enabled: true, findings: [] })).toBe('all clear');
  });
  it('says off when disabled or absent', () => {
    expect(FC.countLabel({ enabled: false, findings: [{}] })).toBe('off');
    expect(FC.countLabel(null)).toBe('off');
  });
  it('ignores a findings value that is not a list of rows', () => {
    expect(FC.countLabel({ enabled: true, findings: 'oops' })).toBe('all clear');
    expect(FC.countLabel({ enabled: true, findings: [null, 'x', {}] })).toBe('1 ahead');
    expect(FC.countLabel('oops')).toBe('off');
  });
});

describe('fitPoints with a reference line', () => {
  it('draws past a threshold that is not a limit', () => {
    const now = 1_000_000_000;
    const g = { start: now - 10 * 86400, end: now + 5 * 86400, threshold: 80,
                fit: { slope_per_s: -1 / 86400, intercept: 60 + now / 86400 } };
    expect(FC.fitPoints(g, now * 1000)).toEqual([]);
    const pts = FC.fitPoints({ ...g, limit: false }, now * 1000);
    expect(pts).toHaveLength(2);
    expect(pts[1][0]).toBe((now + 5 * 86400) * 1000);
    expect(pts[1][1]).toBeCloseTo(55, 5);
  });
});

describe('urgent / shortDigest', () => {
  it('keeps critical and warning findings only, most urgent first', () => {
    const rows = [{ severity: 'info', host: 'a' }, { severity: 'warning', host: 'b' },
                  { severity: 'CRITICAL', host: 'c' }, null];
    expect(FC.urgent(rows).map(r => r.host)).toEqual(['c', 'b']);
    expect(FC.urgent('x')).toEqual([]);
  });

  it('returns the first sentence, cut at a word when it is long', () => {
    expect(FC.shortDigest('Memory is the common factor. Look at node-3 first.')).toBe('Memory is the common factor.');
    expect(FC.shortDigest('no full stop here')).toBe('no full stop here');
    expect(FC.shortDigest('alpha beta gamma delta.', 12)).toBe('alpha beta…');
    expect(FC.shortDigest(null)).toBe('');
  });
});

describe('list helpers', () => {
  const now = Date.UTC(2026, 8, 21, 12);
  const at = d => now / 1000 + d * 86400;
  const rows = [
    { id: 1, severity: 'critical', host: 'b', title: 'Disk fill', check: 'disk_fill', summary: 'full soon', predicted_at: at(2.2), confidence: 'high' },
    { id: 2, severity: 'info', host: 'a', title: 'Idle waste', check: 'idle_waste', summary: 'idle', confidence: 'low' },
    { id: 3, severity: 'warning', host: 'a', title: 'Slot pressure', check: 'slot_pressure', subject: 'streams', summary: 'slots', predicted_at: at(40) },
  ];

  it('facets and filters', () => {
    expect(FC.facets(rows)).toEqual({ hosts: ['a', 'b'], checks: ['Disk fill', 'Idle waste', 'Slot pressure'],
                                      sev: { critical: 1, warning: 1, info: 1 } });
    expect(FC.filterFindings(rows, { sev: ['info', 'warning'], host: 'a' }).map(r => r.id)).toEqual([2, 3]);
    expect(FC.filterFindings(rows, { q: ' FULL ' }).map(r => r.id)).toEqual([1]);
    expect(FC.filterFindings(rows, { check: 'Idle waste' }).map(r => r.id)).toEqual([2]);
    expect(FC.filterFindings('x', null)).toEqual([]);
  });

  it('sorts by each key and reverses', () => {
    expect(FC.sortBy(rows, 'urgency').map(r => r.id)).toEqual([1, 3, 2]);
    expect(FC.sortBy(rows, 'host').map(r => r.id)).toEqual([3, 2, 1]);
    expect(FC.sortBy(rows, 'when').map(r => r.id)).toEqual([1, 3, 2]);
    expect(FC.sortBy(rows, 'conf', 'desc').map(r => r.id)).toEqual([3, 2, 1]);
    expect(FC.sortBy(rows, 'nonsense').map(r => r.id)).toEqual([1, 3, 2]);
  });

  it('pages and clamps the page number', () => {
    expect(FC.paginate([1, 2, 3, 4, 5], 9, 2)).toEqual({ rows: [5], page: 3, pages: 3, from: 5, to: 5, total: 5 });
    expect(FC.paginate([], 1, 8)).toEqual({ rows: [], page: 1, pages: 1, from: 0, to: 0, total: 0 });
    expect(FC.paginate([1, 2, 3], 'x', 0).rows).toEqual([1, 2, 3]);
  });

  it('words the time to a prediction', () => {
    expect(FC.whenText(rows[0], now)).toBe('In 2.2 days');
    expect(FC.whenText({ predicted_at: at(0.5) }, now)).toBe('In 12 hours');
    expect(FC.whenText({ predicted_at: at(12.4) }, now)).toBe('In 12 days');
    expect(FC.whenText({ predicted_at: at(-1) }, now)).toBe('Overdue');
    expect(FC.whenText(rows[1], now)).toBe('Ongoing');
  });

  it('places dated findings on the horizon and counts the rest', () => {
    const h = FC.horizon(rows, now, 30);
    expect(h.ongoing).toBe(1);
    expect(h.dated.map(d => [d.id, d.sev, d.beyond])).toEqual([[1, 'critical', false], [3, 'warning', true]]);
    expect(h.dated[0].days).toBeCloseTo(2.2, 5);
    expect(h.dated[1].days).toBe(30);
  });

  it('groups rows for the briefing view and rolls up their severities', () => {
    const g = FC.groupRows(rows, 'host');
    expect(g.map(x => [x.name, x.rows.map(r => r.id)])).toEqual([['b', [1]], ['a', [2, 3]]]);
    expect(g[1].sev).toEqual({ critical: 0, warning: 1, info: 1 });
    expect(FC.groupRows(rows, 'check').map(x => x.name)).toEqual(['Disk fill', 'Idle waste', 'Slot pressure']);
    expect(FC.groupRows(rows, '')).toHaveLength(1);
    expect(FC.sevLabel('critical').short).toBe('crit');
  });

  it('splits resolved rows by how they ended', () => {
    const r = FC.resolvedTabs([{ id: 1, status: 'cleared' }, { id: 2, status: 'dismissed' }, null]);
    expect(r.cleared.map(x => x.id)).toEqual([1]);
    expect(r.dismissed.map(x => x.id)).toEqual([2]);
  });

  it('offers a destination only where one exists and the viewer may open it', () => {
    expect(FC.goTarget(rows[0], false)).toBeNull();
    expect(FC.goTarget(rows[0], true)).toEqual({ label: 'Open model maintenance', tab: 'admin', sub: 'routing',
                                                 anchor: 'adminStoresCard', host: 'b' });
    expect(FC.goTarget(rows[2], false).label).toBe('Open Manager dashboard');
    expect(FC.goTarget({ check: 'weekly_digest' }, true)).toBeNull();
    expect(FC.goTarget(null, true)).toBeNull();
  });

  it('writes the Ask Tower questions', () => {
    expect(FC.askText({ host: 'a', summary: 'Disk fills.', detail: 'It grows. Likely cause: downloads', suggested_action: 'Prune.' }))
      .toBe('Help me with this Forecast finding on a: Disk fills. It grows. Forecast suggests: Prune. '
        + 'What should I check first, and how do I confirm the cause?');
    expect(FC.askRunText({ findings: [{}, {}] })).toContain('2 findings are open');
    expect(FC.askRunText({ findings: [{}] })).toContain('1 finding is open');
  });
});

describe('summary', () => {
  it('counts open findings per severity and picks the soonest dated one', () => {
    const s = FC.summary({ enabled: true, findings: [
      { severity: 'critical', predicted_at: 300, host: 'a' },
      { severity: 'warning', predicted_at: 100, host: 'b' },
      { severity: 'warning', predicted_at: null },
      { severity: 'odd' }, null, 'x',
    ] });
    expect(s.critical).toBe(1);
    expect(s.warning).toBe(2);
    expect(s.info).toBe(1);
    expect(s.next.host).toBe('b');
  });

  it('is all zero when Forecast is off or the view is garbage', () => {
    for (const v of [null, 'x', { enabled: false, findings: [{ severity: 'critical' }] }, { enabled: true, findings: 7 }]) {
      expect(FC.summary(v)).toEqual({ critical: 0, warning: 0, info: 0, next: null });
    }
  });
});

describe('checkState', () => {
  it('ok shows the found count only when there is one', () => {
    expect(FC.checkState({ state: 'ok', found: 3 })).toEqual({ cls: 'ok', note: '3' });
    expect(FC.checkState({ state: 'ok', found: 0 })).toEqual({ cls: 'ok', note: '' });
  });
  it('collecting reports progress towards the minimum window', () => {
    expect(FC.checkState({ state: 'collecting', have_days: 3.0, min_days: 7.0 }))
      .toEqual({ cls: 'collect', note: '3 of 7 days' });
    expect(FC.checkState({ state: 'collecting', have_days: 0.0625, min_days: 7.0 }).note)
      .toBe('0.1 of 7 days');
    expect(FC.checkState({ state: 'collecting', have_days: null, min_days: 3.0 }).note)
      .toBe('0 of 3 days');
  });
  it('covers failed, off and pending', () => {
    expect(FC.checkState({ state: 'failed' })).toEqual({ cls: 'failed', note: 'could not run' });
    expect(FC.checkState({ state: 'off' })).toEqual({ cls: 'off', note: '' });
    expect(FC.checkState({ state: 'pending' })).toEqual({ cls: 'pending', note: 'not run yet' });
  });
  it('treats an unknown state as pending without a note', () => {
    expect(FC.checkState({ state: 'weird' })).toEqual({ cls: 'pending', note: '' });
    expect(FC.checkState(null)).toEqual({ cls: 'pending', note: '' });
  });
});

describe('whenLabel / dateLabel', () => {
  it('names today, tomorrow and yesterday', () => {
    expect(FC.whenLabel(TODAY_03, NOW)).toBe('Today 03:00');
    expect(FC.whenLabel(TOMORROW_03, NOW)).toBe('Tomorrow 03:00');
    expect(FC.whenLabel(YESTERDAY_03, NOW)).toBe('Yesterday 03:00');
  });
  it('falls back to a dated label further out', () => {
    expect(FC.whenLabel(SEP_27_03, NOW)).toBe('Sep 27 03:00');
    expect(FC.whenLabel(sec(2026, 7, 2, 18, 5), NOW)).toBe('Aug 2 18:05');
  });
  it('is blank for a null timestamp', () => {
    expect(FC.whenLabel(null, NOW)).toBe('');
    expect(FC.dateLabel(null)).toBe('');
  });
  it('dateLabel drops the time', () => {
    expect(FC.dateLabel(SEP_16)).toBe('Sep 16');
  });
  it('is blank for a non-finite or non-numeric timestamp', () => {
    [undefined, NaN, Infinity, -Infinity, 'soon', '', {}, [], true].forEach(bad => {
      expect(FC.whenLabel(bad, NOW)).toBe('');
      expect(FC.dateLabel(bad)).toBe('');
    });
  });
  it('accepts a numeric string timestamp', () => {
    expect(FC.whenLabel(String(TODAY_03), NOW)).toBe('Today 03:00');
  });
});

describe('rateLabel', () => {
  it('keeps the sign, one decimal and the unit', () => {
    expect(FC.rateLabel(18.64, 'GB/day')).toBe('+18.6 GB/day');
    expect(FC.rateLabel(-14.089, 'tok/s')).toBe('-14.1 tok/s');
    expect(FC.rateLabel(4, '')).toBe('+4.0');
  });
  it('is blank without a usable rate', () => {
    expect(FC.rateLabel(null, 'GB/day')).toBe('');
    expect(FC.rateLabel(undefined, 'GB/day')).toBe('');
    expect(FC.rateLabel(NaN, 'GB/day')).toBe('');
    expect(FC.rateLabel(Infinity, 'GB/day')).toBe('');
    expect(FC.rateLabel(-Infinity, 'GB/day')).toBe('');
    expect(FC.rateLabel('fast', 'GB/day')).toBe('');
  });
});

describe('splitCause', () => {
  it('splits the cause sentence off the detail', () => {
    const r = FC.splitCause('Disk grew steadily. Likely cause: nightly autotune downloads');
    expect(r.detail).toBe('Disk grew steadily.');
    expect(r.cause).toBe('nightly autotune downloads');
  });
  it('splits at the LAST marker when there are two', () => {
    const r = FC.splitCause('A. Likely cause: first one. Likely cause: second one');
    expect(r.detail).toBe('A. Likely cause: first one.');
    expect(r.cause).toBe('second one');
  });
  it('leaves a detail without the marker alone', () => {
    const r = FC.splitCause('  Just a detail.  ');
    expect(r).toEqual({ detail: 'Just a detail.', cause: null });
  });
  it('handles null and an empty cause', () => {
    expect(FC.splitCause(null)).toEqual({ detail: '', cause: null });
    expect(FC.splitCause('Detail. Likely cause: ')).toEqual({ detail: 'Detail.', cause: null });
  });
});

describe('towerLine / digestNote', () => {
  it('reports the tower effort reason', () => {
    expect(FC.towerLine({ tier: 'standard', model: 'qwen3.5-9b', reason: 'Standard — 9B model' }))
      .toBe('Standard — 9B model');
  });
  it('is Off without tower', () => {
    expect(FC.towerLine(null)).toBe('Off');
  });
  it('falls back to On when the reason is missing', () => {
    expect(FC.towerLine({ tier: 'standard', model: 'm' })).toBe('On');
  });
  it('falls back to On when the reason is present but empty', () => {
    expect(FC.towerLine({ tier: 'standard', model: 'm', reason: '' })).toBe('On');
  });
  it('credits the digest model', () => {
    expect(FC.digestNote({ model: 'qwen3.5-9b@q6_k' }))
      .toBe('written by qwen3.5-9b@q6_k · figures always come from code');
  });
  it('has no note without a model', () => {
    expect(FC.digestNote(null)).toBe('');
    expect(FC.digestNote({ tier: 'standard' })).toBe('');
  });
});

describe('facts', () => {
  it('lists since, rate, predicted, confidence and who checked it', () => {
    expect(FC.facts(FINDING, NOW)).toEqual([
      ['Since', 'Sep 9'],
      ['Rate', '+18.6 GB/day'],
      ['Predicted', 'Sep 27 03:00'],
      ['Confidence', 'High'],
      ['Checked by', 'Tower + code'],
    ]);
  });
  it('omits a null fact, and says so when a finding has no date', () => {
    const out = FC.facts({ ...FINDING, predicted_at: null, rate: null, since: null }, NOW);
    expect(out).toEqual([['Predicted', 'No date, ongoing'], ['Confidence', 'High'], ['Checked by', 'Tower + code']]);
  });
  it('is empty for a bare finding or a non-object', () => {
    expect(FC.facts(null, NOW)).toEqual([]);
    expect(FC.facts({}, NOW)).toEqual([]);
    expect(FC.facts('oops', NOW)).toEqual([]);
    expect(FC.facts([FINDING], NOW)).toEqual([]);
  });
  it('omits facts whose values cannot be formatted', () => {
    const out = FC.facts({ ...FINDING, since: 'soon', rate: NaN, predicted_at: Infinity }, NOW);
    expect(out).toEqual([['Predicted', 'No date, ongoing'], ['Confidence', 'High'], ['Checked by', 'Tower + code']]);
  });
});

describe('clearedLabel', () => {
  it('labels a cleared row with its resolved date', () => {
    expect(FC.clearedLabel({ status: 'cleared', resolved: SEP_16 })).toBe('cleared Sep 16');
  });
  it('names the operator who dismissed a row', () => {
    expect(FC.clearedLabel({ status: 'dismissed', dismissed_by: 'alice', resolved: SEP_16 }))
      .toBe('dismissed by alice');
  });
  it('degrades without a date or a user', () => {
    expect(FC.clearedLabel({ status: 'cleared', resolved: null })).toBe('cleared');
    expect(FC.clearedLabel({ status: 'dismissed', dismissed_by: null })).toBe('dismissed');
    expect(FC.clearedLabel(null)).toBe('');
  });
});

describe('fitPoints', () => {
  const nowSec = NOW / 1000;
  // 90 now, +0.001/s: 100 at now+10000 s, 110 at the graph end.
  const fit = { slope_per_s: 0.001, intercept: 90 - 0.001 * nowSec };
  const graph = { start: nowSec - 86400, end: nowSec + 20000, threshold: 100, fit };

  it('projects from now to the graph end', () => {
    const pts = FC.fitPoints({ ...graph, threshold: null }, NOW);
    expect(pts).toHaveLength(2);
    expect(pts[0][0]).toBe(NOW);
    expect(pts[0][1]).toBeCloseTo(90, 6);
    expect(pts[1][0]).toBe(NOW + 20000 * 1000);
    expect(pts[1][1]).toBeCloseTo(110, 6);
  });
  it('clips the projection at the threshold', () => {
    const pts = FC.fitPoints(graph, NOW);
    expect(pts[1][0]).toBe(NOW + 10000 * 1000);
    expect(pts[1][1]).toBeCloseTo(100, 6);
  });
  it('clips a falling line at its threshold too', () => {
    const down = {
      start: nowSec - 86400, end: nowSec + 20000, threshold: 80,
      fit: { slope_per_s: -0.001, intercept: 90 + 0.001 * nowSec },
    };
    const pts = FC.fitPoints(down, NOW);
    expect(pts[1][0]).toBe(NOW + 10000 * 1000);
    expect(pts[1][1]).toBeCloseTo(80, 6);
  });
  it('leaves the line alone when the threshold is never reached', () => {
    const pts = FC.fitPoints({ ...graph, threshold: 500 }, NOW);
    expect(pts[1][0]).toBe(NOW + 20000 * 1000);
    expect(pts[1][1]).toBeCloseTo(110, 6);
  });
  it('has nothing to project without a graph, a fit or a future end', () => {
    expect(FC.fitPoints(null, NOW)).toEqual([]);
    expect(FC.fitPoints({ ...graph, fit: null }, NOW)).toEqual([]);
    expect(FC.fitPoints({ ...graph, end: nowSec }, NOW)).toEqual([]);
    expect(FC.fitPoints({ ...graph, end: nowSec - 60 }, NOW)).toEqual([]);
  });
  it('refuses a graph or fit whose numbers are not usable', () => {
    expect(FC.fitPoints('oops', NOW)).toEqual([]);
    expect(FC.fitPoints({ ...graph, end: 'later' }, NOW)).toEqual([]);
    expect(FC.fitPoints({ ...graph, fit: { slope_per_s: null, intercept: 1 } }, NOW)).toEqual([]);
    expect(FC.fitPoints({ ...graph, fit: { slope_per_s: 0.001, intercept: NaN } }, NOW)).toEqual([]);
    expect(FC.fitPoints({ ...graph, fit: 'linear' }, NOW)).toEqual([]);
  });
  it('has nothing to project once the line already sits past the threshold', () => {
    expect(FC.fitPoints({ ...graph, threshold: 90 }, NOW)).toEqual([]);
    expect(FC.fitPoints({ ...graph, threshold: 20 }, NOW)).toEqual([]);
  });
  it('has nothing to project for a falling line already under its threshold', () => {
    const down = {
      start: nowSec - 86400, end: nowSec + 20000, threshold: 90,
      fit: { slope_per_s: -0.001, intercept: 90 + 0.001 * nowSec },
    };
    expect(FC.fitPoints(down, NOW)).toEqual([]);
    expect(FC.fitPoints({ ...down, threshold: 95 }, NOW)).toEqual([]);
  });
  it('starts at the window start when it is still ahead of now', () => {
    const later = { ...graph, threshold: null, start: nowSec + 5000 };
    expect(FC.fitPoints(later, NOW)[0][0]).toBe(NOW + 5000 * 1000);
  });
});

describe('chartScale', () => {
  const pts = [[1000, 80], [2000, 90]];

  it('pads the value range by 5 %', () => {
    const s = FC.chartScale(pts, null, null);
    expect(s.xMin).toBe(1000);
    expect(s.xMax).toBe(2000);
    expect(s.yMin).toBeCloseTo(79.5, 6);
    expect(s.yMax).toBeCloseTo(90.5, 6);
  });
  it('stretches x and y over the projection', () => {
    const s = FC.chartScale(pts, [[2000, 90], [5000, 120]], null);
    expect(s.xMax).toBe(5000);
    expect(s.yMax).toBeGreaterThan(120);
  });
  it('always keeps the threshold in view', () => {
    const s = FC.chartScale(pts, null, 100);
    expect(s.yMax).toBeGreaterThanOrEqual(100);
  });
  it('never drops a non-negative series below zero', () => {
    expect(FC.chartScale([[1000, 0], [2000, 2]], null, null).yMin).toBe(0);
  });
  it('gives a flat series a visible band', () => {
    const s = FC.chartScale([[1000, 50], [2000, 50]], null, null);
    expect(s.yMax).toBeGreaterThan(s.yMin);
  });
  it('is null when there is nothing to draw', () => {
    expect(FC.chartScale([], null, null)).toBeNull();
    expect(FC.chartScale(null, null, null)).toBeNull();
  });
  it('ignores non-array inputs and unusable points', () => {
    expect(FC.chartScale('oops', 'oops', null)).toBeNull();
    expect(FC.chartScale([null, 'x', [1000, null], [2000, NaN]], null, null)).toBeNull();
    const s = FC.chartScale([[1000, 80], null, [2000, 'x']], 'oops', null);
    expect(s.xMax).toBe(1000);
  });
});

describe('live API sample', () => {
  const ROW = {
    check: 'service_health', title: 'Service health', host: 'node-3',
    severity: 'warning', summary: 'Llama-server memory grows 6 % a day',
    detail: 'It holds about 3319 MB today and has climbed every day this week. ' +
            'Likely cause: Unbounded context window retention or lack of session cleanup',
    predicted_at: null, rate: 5.974077701099512, unit: '%/day', confidence: 'low',
    verified: 'tower+code', status: 'open',
    graph: { source: 'processes', name: 'llama-server_rss_mb', host: 'node-3',
             start: 1788579193.5172524, end: 1789788793.5172524, threshold: null,
             fit: { intercept: -4104389.2632913385, slope_per_s: 0.0022950800305992667 } },
  };
  it('splits the model-written cause out of a real detail', () => {
    const r = FC.splitCause(ROW.detail);
    expect(r.detail.endsWith('this week.')).toBe(true);
    expect(r.cause).toBe('Unbounded context window retention or lack of session cleanup');
  });
  it('says there is no date when the backend predicted nothing', () => {
    expect(FC.facts(ROW, NOW)).toContainEqual(['Predicted', 'No date, ongoing']);
    expect(FC.facts(ROW, NOW)).toContainEqual(['Rate', '+6.0 %/day']);
  });
  it('draws no projection for a graph that ends at the run time', () => {
    expect(FC.fitPoints(ROW.graph, ROW.graph.end * 1000)).toEqual([]);
  });
});
