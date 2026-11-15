import { describe, test } from 'vitest';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const KIT_SRC = readFileSync(resolve(HERE, '../../kit/kit.js'), 'utf8');
const KIT_VERSION = readFileSync(resolve(HERE, '../../kit/KIT_VERSION'), 'utf8');
// A small WORLD_DOTS of the cloud worlddots.js shape: 8 dots over DE, FR, JP and US (3 dots for US).
const WORLD_SRC = "var WORLD_DOTS = { w: 60, h: 30, step: 6, codes: ['DE', 'FR', 'JP', 'US'], dots: '1,1,0;2,1,0;3,1,1;7,2,2;8,2,2;5,3,3;6,3,3;6,4,3;' };";

// Loads kit.js into a bare context with no document, as a classic script.
function kit() {
  const ctx = vm.createContext({});
  vm.runInContext(KIT_SRC, ctx);
  return ctx;
}
// Loads the WORLD_DOTS fixture and kit.js into one context, as the cloud page does.
function cloudKit() {
  const ctx = vm.createContext({});
  vm.runInContext(WORLD_SRC, ctx);
  vm.runInContext(KIT_SRC, ctx);
  return ctx;
}
const count = (s, re) => (s.match(re) || []).length;

describe('kit core', () => {
  test('kit.js is a classic script', () => {
    assert.doesNotMatch(KIT_SRC, /^\s*(import|export)\b/m);
    assert.doesNotMatch(KIT_SRC, /module\.exports|require\(/);
    assert.doesNotThrow(() => new vm.Script(KIT_SRC));
  });

  test('version header', () => {
    const first = KIT_SRC.split('\n')[0];
    assert.equal(first, '// Panel kit ' + KIT_VERSION.trim());
    assert.equal(first, '// Panel kit 1.0.0');
  });

  test('coexists with an old copy', () => {
    const ctx = kit();
    assert.doesNotThrow(() => vm.runInContext("function kitEsc(){ return 'old'; } var KIT_MENU_ICON = 'x';", ctx));
  });

  test('kitConfigure', () => {
    const k = kit();
    assert.equal(k.KIT_CONF.prefix, 'kit');
    assert.equal(k.kitKey('rows'), 'kit.rows');
    k.kitConfigure({ prefix: 'dt' });
    assert.equal(k.kitKey('rows'), 'dt.rows');
    k.kitConfigure({ prefix: '' });
    assert.equal(k.KIT_CONF.prefix, 'dt');
    const s = { getItem() { return null; }, setItem() {} };
    assert.equal(k.kitConfigure({ store: s }), k.KIT_CONF);
    assert.equal(k.kitStore(null), s);
  });

  test('prefs', () => {
    const k = kit();
    const m = new Map();
    const store = { getItem: (key) => (m.has(key) ? m.get(key) : null), setItem: (key, v) => { m.set(key, String(v)); } };
    assert.equal(k.kitTheme(store), 'dark');
    for (const [v, want] of [['light', 'light'], ['dark', 'dark'], ['x', 'dark']]) {
      m.set('kit.theme', v);
      assert.equal(k.kitTheme(store), want, v);
    }
    assert.equal(k.kitNav(store), 'pinned');
    for (const [v, want] of [['collapsed', 'collapsed'], ['pinned', 'pinned'], ['x', 'pinned']]) {
      m.set('kit.nav', v);
      assert.equal(k.kitNav(store), want, v);
    }
    assert.equal(k.kitLive(store), true);
    for (const [v, want] of [['off', false], ['on', true], ['x', true]]) {
      m.set('kit.live', v);
      assert.equal(k.kitLive(store), want, v);
    }
    const throwing = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
    assert.equal(k.kitTheme(throwing), 'dark');
    assert.equal(k.kitNav(throwing), 'pinned');
    assert.equal(k.kitLive(throwing), true);
    assert.equal(k.kitTheme(null), 'dark');
    assert.equal(k.kitNav(null), 'pinned');
    assert.equal(k.kitLive(null), true);
  });

  test('kitSavedObj junk', () => {
    const k = kit();
    for (const v of ['[1]', '"x"', '{', null]) {
      const store = { getItem: () => v, setItem() {} };
      assert.equal(JSON.stringify(k.kitSavedObj(store, 'k')), '{}', String(v));
    }
  });

  test('kitDots stays null without WORLD_DOTS, and mapHtml still lists countries', () => {
    const k = kit();
    assert.equal(k.kitDots(), null);
    const out = k.mapHtml([{ code: 'US', v: 5 }], { US: 'United States' });
    assert.ok(!out.includes('map-cv'));
    assert.ok(out.includes('United States'));
  });

  test('kitDots groups dot centres by country index, once', () => {
    const ctx = vm.createContext({});
    vm.runInContext("var WORLD_DOTS = { w: 10, h: 10, step: 2, codes: ['AA', 'BB'], dots: '0,0,0;1,0,0;2,1,1;' };", ctx);
    vm.runInContext(KIT_SRC, ctx);
    const dots = ctx.kitDots();
    assert.equal(JSON.stringify(dots), JSON.stringify({ 0: ['1" cy="1', '3" cy="1'], 1: ['5" cy="3'] }));
    assert.equal(ctx.kitDots(), dots);
  });
});

describe('ported from devtools kit.test.mjs', () => {
  test('kitFmt', () => {
    const k = kit();
    for (const [n, want] of [[null, '–'], [0, '0'], [999, '999'], [1234, '1.23k'], [12345, '12.3k']]) assert.equal(k.kitFmt(n), want, String(n));
  });

  test('kitNiceMax', () => {
    const k = kit();
    assert.equal(k.kitNiceMax(60), 100);
    assert.equal(k.kitNiceMax(3), 4);
  });

  test('kitCsv guards spreadsheet formulas', () => {
    assert.equal(kit().kitCsv([['=x', 1]]), "'=x,1");
  });

  test('panelHead menu items', () => {
    const head = kit().panelHead('a', 'T', { table: true });
    for (const item of ['Show as table', 'Copy data', 'Open full size']) assert.ok(head.includes(item), item);
  });

  test('kit.js uses only the theme tokens', () => {
    const ok = new Set('bg card pop border border-2 text text-2 muted grid track accent hover s1 s2 s3 s4 s5 c5 good warn bad on-accent map-dot'.split(' '));
    const used = [...KIT_SRC.matchAll(/var\(--([a-z0-9-]+)\)/g)].map((m) => m[1]);
    assert.ok(used.length > 0);
    assert.deepEqual(used.filter((t) => !ok.has(t)), []);
  });

  test('kpi delta takes a tone as a class and keeps the arrow of its direction', () => {
    const k = kit();
    const bad = k.kpiBody({ value: 1, delta: { dir: 'up', text: '+11', tone: 'bad' } });
    assert.match(bad, /class="k-delta up bad">↗ \+11/);
    assert.match(k.kpiBody({ value: 1, delta: { dir: 'down', text: '−1', tone: 'good' } }), /class="k-delta down good">↘ /);
    assert.match(k.kpiBody({ value: 1, delta: { dir: 'up', text: '+1' } }), /class="k-delta up">/);
    assert.match(k.kpiBody({ value: 1, delta: { dir: 'up', text: '+1', tone: '"><x' } }), /class="k-delta up">/);
  });

  test('a no-tone up delta has neither tone class', () => {
    const html = kit().kpiBody({ value: 1, delta: { dir: 'up', text: '+1' } });
    const cls = html.match(/class="(k-delta[^"]*)"/)[1].split(' ');
    assert.deepEqual(cls, ['k-delta', 'up']);
    assert.ok(!cls.includes('good') && !cls.includes('bad'));
    assert.match(html, /↗ \+1/);
  });

  const COLOR_SERIES = [{ name: 'Passed', values: [1, 2], slot: 1 }, { name: 'Failed', values: [3, 4], color: 'bad' }, { name: 'Cancelled', values: [1, 1], slot: 5 }];
  test('a series with color bad draws var(--bad), with its legend chip the same', () => {
    const k = kit();
    for (const mode of ['line', 'stack']) {
      const html = k.tsHtml({ id: 'c', labels: ['d1', 'd2'], mode, series: COLOR_SERIES });
      assert.match(html, /<i style="background:var\(--bad\)"><\/i>Failed/, mode);
      assert.match(html, mode === 'stack' ? /fill="var\(--bad\)"/ : /stroke="var\(--bad\)"/, mode);
      assert.doesNotMatch(html, /var\(--s2\)/, mode);
      assert.match(html, /<i style="background:var\(--s1\)"><\/i>Passed/, mode);
      assert.match(html, /<i style="background:var\(--s5\)"><\/i>Cancelled/, mode);
    }
    assert.match(k.tsHtml({ id: 'c', labels: ['d'], series: [{ name: 'N', values: [1], color: 'pink' }] }), /<i style="background:var\(--s1\)">/);
  });

  test('stacked bars use the ts-bar class, and no bare bar class is emitted or selected', () => {
    const html = kit().tsHtml({ id: 'c', labels: ['d1'], mode: 'stack', series: COLOR_SERIES });
    assert.match(html, /<rect class="ts-bar"/);
    assert.match(html, /<path class="ts-bar"/);
    assert.doesNotMatch(html, /class="bar"/);
    assert.doesNotMatch(KIT_SRC, /class="bar"|\.bar(?![\w-])|['"]bar['"]/);
  });

  test('chart markers use --c5, release markers --muted', () => {
    const html = kit().tsHtml({ id: 'c', labels: ['2026-11-01', '2026-11-02'], series: COLOR_SERIES,
      marks: [{ day: '2026-11-01', label: 'note' }, { day: '2026-11-02', label: 'v1', release: true }] });
    const strokes = [...html.matchAll(/<line class="mk"[^>]*stroke="([^"]*)"/g)].map((m) => m[1]);
    assert.deepEqual(strokes, ['var(--c5)', 'var(--muted)']);
    assert.doesNotMatch(KIT_SRC, /var\(--warn\)/);
  });

  test('panelHead has the full-size close button between the menu button and the menu', () => {
    const head = kit().panelHead('a', 'Visits <x>', {});
    const shut = '<button type="button" class="p-close" data-kit-close aria-label="Close full size: Visits &lt;x&gt;" title="Close full size">✕</button>';
    assert.ok(head.includes(shut), head);
    assert.ok(head.indexOf('class="p-menu"') < head.indexOf(shut));
    assert.ok(head.indexOf(shut) < head.indexOf('class="p-pop"'));
  });

  test('kitConfigure takes dblSkip, onLegend, onRefit and holderRows, and ignores junk', () => {
    const k = kit(), f = () => {}, g = () => {}, hr = () => null;
    assert.equal(k.KIT_CONF.dblSkip, '');
    assert.equal(k.KIT_CONF.onLegend, null);
    assert.equal(k.KIT_CONF.onRefit, null);
    assert.equal(k.KIT_CONF.holderRows, null);
    k.kitConfigure({ dblSkip: '.x', onLegend: f, onRefit: g, holderRows: hr });
    assert.equal(k.KIT_CONF.dblSkip, '.x');
    assert.equal(k.KIT_CONF.onLegend, f);
    assert.equal(k.KIT_CONF.onRefit, g);
    assert.equal(k.KIT_CONF.holderRows, hr);
    k.kitConfigure({ dblSkip: 5, onLegend: 'no', onRefit: {}, holderRows: [] });
    assert.equal(k.KIT_CONF.dblSkip, '.x');
    assert.equal(k.KIT_CONF.onLegend, f);
    assert.equal(k.KIT_CONF.onRefit, g);
    assert.equal(k.KIT_CONF.holderRows, hr);
    k.kitConfigure({ prefix: 'dt' });
    assert.equal(k.KIT_CONF.onLegend, f);
    k.kitConfigure({ dblSkip: null, onLegend: null, onRefit: null, holderRows: null });
    assert.equal(k.KIT_CONF.dblSkip, '');
    assert.equal(k.KIT_CONF.onLegend, null);
    assert.equal(k.KIT_CONF.onRefit, null);
    assert.equal(k.KIT_CONF.holderRows, null);
  });

  const LANE_OPTS = {
    id: 'day', now: '13:30', lanes: ['smoketest', 'group: a really long group name'],
    marks: [
      { lane: 'smoketest', at: '00:00', state: 'passed', key: 0, tip: 'first <one>' },
      { lane: 'smoketest', at: '12:15', state: 'failed', key: 1, tip: 'second' },
      { lane: 'group: a really long group name', at: '23:59', state: 'come', key: 2, tip: 'third' },
      { lane: 'group: a really long group name', at: '24:00', state: 'skipped', key: 3, tip: 'fourth' },
    ],
  };
  const markGroups = (html) => [...html.matchAll(/<g class="lm" role="button" tabindex="0" data-mark="([^"]*)"[^>]*><circle class="hit" cx="([\d.]+)" cy="([\d.]+)" r="9"/g)].map((m) => ({ key: m[1], cx: +m[2], cy: +m[3] }));

  test('lanesHtml keeps every mark inside the viewBox at phone and desktop width', () => {
    const k = kit();
    for (const w of [300, 1000]) {
      const html = k.lanesHtml({ ...LANE_OPTS, w });
      const [, vw, vh] = html.match(/viewBox="0 0 (\d+) (\d+)"/).map(Number);
      assert.equal(vw, w);
      const marks = markGroups(html);
      assert.deepEqual(marks.map((m) => m.key), ['0', '1', '2', '3']);
      for (const m of marks) {
        assert.ok(m.cx - 9 >= 0 && m.cx + 9 <= vw, `x ${m.cx} at ${w}`);
        assert.ok(m.cy - 9 >= 0 && m.cy + 9 <= vh, `y ${m.cy} at ${w}`);
      }
    }
  });

  test('lanesHtml gives at most one x label per 90 px', () => {
    const k = kit();
    for (const w of [120, 300, 420, 600, 1000, 1600]) {
      const xs = [...k.lanesHtml({ ...LANE_OPTS, w }).matchAll(/<text x="([\d.]+)"[^>]*class="ax ax-x"/g)].map((m) => +m[1]);
      assert.ok(xs.length >= 1, `labels at ${w}`);
      for (let i = 1; i < xs.length; i++) assert.ok(xs[i] - xs[i - 1] >= 90, `gap at ${w}: ${xs[i] - xs[i - 1]}`);
    }
  });

  test('lanesHtml draws each state and the now line from tokens', () => {
    const html = kit().lanesHtml({ ...LANE_OPTS, w: 600 });
    const group = (key) => html.slice(html.indexOf(`data-mark="${key}"`), html.indexOf('</g>', html.indexOf(`data-mark="${key}"`)));
    assert.match(group(0), /<circle cx="[\d.]+" cy="[\d.]+" r="4" fill="var\(--good\)"/);
    assert.match(group(1), /<path [^>]*stroke="var\(--bad\)"/);
    assert.match(group(2), /<circle cx="[\d.]+" cy="[\d.]+" r="4" fill="none" stroke="var\(--muted\)"/);
    assert.match(group(3), /<line [^>]*stroke="var\(--muted\)"/);
    assert.match(html, /<line class="now-l"[^>]*stroke="var\(--accent\)"/);
    assert.doesNotMatch(kit().lanesHtml({ ...LANE_OPTS, w: 600, now: null }), /now-l/);
    assert.match(html, /data-tip="first &lt;one&gt;"/);
    assert.match(html, /<title>group: a really long group name<\/title>/);
  });

  test('lanesHtml says so when there is nothing to draw', () => {
    const k = kit(), text = 'No scheduled runs today.', empty = '<div class="empty">No scheduled runs today.</div>';
    assert.equal(k.lanesHtml({ lanes: [], marks: [], empty: text }), empty);
    assert.equal(k.lanesHtml({ empty: text }), empty);
    assert.equal(k.lanesHtml({ lanes: ['a'], marks: [], empty: text }), empty);
    assert.equal(k.lanesHtml({ lanes: ['a'], marks: [{ lane: 'b', at: '10:00', state: 'come', key: 0 }, { lane: 'a', at: 'soon', state: 'come', key: 1 }], empty: text }), empty);
  });

  test('lanesHtml has neutral empty text and aria label by default', () => {
    const k = kit();
    assert.equal(k.lanesHtml({}), '<div class="empty">Nothing in this period.</div>');
    assert.equal(k.lanesHtml({ lanes: ['a'], marks: [] }), '<div class="empty">Nothing in this period.</div>');
    const html = k.lanesHtml({ ...LANE_OPTS, w: 600 });
    assert.match(html, /role="group" aria-label="Timeline by hour"/);
    assert.doesNotMatch(html, /Scheduled runs/);
  });

  test('lanesHtml shows a custom empty text and aria label, escaped', () => {
    const k = kit();
    assert.equal(k.lanesHtml({ lanes: [], empty: 'No <runs>' }), '<div class="empty">No &lt;runs&gt;</div>');
    const html = k.lanesHtml({ ...LANE_OPTS, w: 600, label: 'Runs & checks by hour' });
    assert.match(html, /role="group" aria-label="Runs &amp; checks by hour"/);
  });

  test('panelHead offers Show as chart only with the bars option', () => {
    const k = kit();
    assert.match(k.panelHead('a', 'T', { bars: true }), /data-kit-do="bars">Show as chart/);
    assert.doesNotMatch(k.panelHead('a', 'T', {}), /data-kit-do="bars"/);
  });
});

describe('ported from cloud kit.test.mjs', () => {
  const kit = cloudKit;

  test('kitFmt', () => {
    const k = kit();
    for (const [n, want] of [[null, '–'], [0, '0'], [999, '999'], [1234, '1.23k'], [12345, '12.3k']]) assert.equal(k.kitFmt(n), want, String(n));
  });

  test('grainFor boundaries', () => {
    const k = kit();
    assert.equal(k.grainFor('90', '2026-11-08'), 'day');
    assert.equal(k.grainFor('365', '2026-11-08'), 'month');
    assert.equal(k.grainFor('all', '2026-11-08'), 'month');
    assert.equal(k.grainFor('today', '2026-11-08'), 'day');
    assert.equal(k.grainFor('2026-08-10', '2026-11-08'), 'day');
    assert.equal(k.grainFor('2026-08-09', '2026-11-08'), 'month');
  });

  test('tsHtml stacked total and legend', () => {
    const k = kit();
    const out = k.tsHtml({ id: 't1', labels: ['Nov 7', 'Nov 8'], mode: 'stack',
      series: [{ name: 'A', values: [1, 2], slot: 1 }, { name: 'B', values: [3, 4], slot: 2 }] });
    const chips = [...out.matchAll(/<button[^>]*data-kit-series="([^"]*)"[^>]*>([\s\S]*?)<\/button>/g)];
    assert.equal(chips.length, 2);
    assert.match(chips[0][2], /<span class="c-v">3<\/span>/);
    assert.match(chips[1][2], /<span class="c-v">7<\/span>/);
    assert.equal(chips[0][1], 't1|A');
    assert.equal(count(out, /<(path|rect)\b/g), 4, 'one mark per non-zero value');
    assert.ok(out.includes('data-kit-ts="'));
    const json = JSON.parse(out.match(/data-kit-ts="([^"]*)"/)[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#39;/g, "'").replace(/&amp;/g, '&'));
    assert.deepEqual(json.labels, ['Nov 7', 'Nov 8']);
    assert.equal(json.series.length, 2);
  });

  test('tsHtml hides series that are toggled off', () => {
    const k = kit();
    k.KIT_SERIES_OFF['t2|B'] = true;
    const out = k.tsHtml({ id: 't2', labels: ['a', 'b', 'c'], mode: 'line',
      series: [{ name: 'A', values: [1, 2, 3], slot: 1 }, { name: 'B', values: [3, 4, 5], slot: 2 }] });
    assert.match(out, /class="chip off"[^>]*aria-pressed="false"[^>]*>[\s\S]*?hidden<\/span><\/button>/);
    assert.equal(count(out, /stroke="var\(--s2\)"/g), 0);
    assert.equal(count(out, /stroke="var\(--s1\)"/g), 1);
    assert.ok(count(out, /stroke-dasharray/g) >= 3);
  });

  test('tsHtml escapes names', () => {
    const k = kit();
    const out = k.tsHtml({ id: 't3', labels: ['x'], mode: 'line', series: [{ name: '<b>', values: [1], slot: 1 }] });
    assert.ok(out.includes('&lt;b&gt;'));
    assert.ok(!out.includes('<b>'));
  });

  test('tsHtml shows at most 7 x labels', () => {
    const k = kit();
    const labels = Array.from({ length: 30 }, (_, i) => 'd' + i);
    const out = k.tsHtml({ id: 't4', labels, mode: 'line', series: [{ name: 'A', values: labels.map(() => 1), slot: 1 }] });
    assert.ok(count(out, /class="ax ax-x"/g) <= 7);
  });

  test('rankHtml caps and sums the rest', () => {
    const k = kit();
    const rows = Array.from({ length: 10 }, (_, i) => ({ label: 'r' + i, v: 1 }));
    const out = k.rankHtml(rows, { dim: 'Page', key: 'path', n: 7 });
    assert.equal(count(out, /<li\b/g), 7);
    assert.ok(out.includes('3 more · 3'));
  });

  test('rankHtml row actions carry key and value', () => {
    const k = kit();
    const out = k.rankHtml([{ label: 'DE', v: 4 }, { label: 'US', v: 2 }], { dim: 'Country', key: 'country', n: 7 });
    assert.ok(out.includes('data-kit-act="out" data-key="country" data-val="DE"'));
    assert.ok(out.includes('data-kit-act="only" data-key="country" data-val="US"'));
    assert.ok(out.includes('width:50%'));
  });

  test('splitHtml folds to Other', () => {
    const k = kit();
    const rows = [10, 8, 6, 4, 2, 1].map((v, i) => ({ label: 'r' + i, v }));
    const out = k.splitHtml(rows, { unit: 'visits' });
    const chips = [...out.matchAll(/<span class="chip static">([\s\S]*?)<span class="c-pc">/g)].map((m) => m[1]);
    assert.equal(chips.length, 5);
    assert.match(chips[4], /Other/);
    const pcs = [...out.matchAll(/<span class="c-pc">(\d+)%<\/span>/g)].map((m) => +m[1]);
    assert.equal(pcs.length, 5);
    const sum = pcs.reduce((a, b) => a + b, 0);
    assert.ok(Math.abs(sum - 100) <= 1, 'sum ' + sum);
  });

  test('funnelHtml hides percentages above 100', () => {
    const k = kit();
    assert.equal(count(k.funnelHtml([{ label: 'A', v: 10 }, { label: 'B', v: 30 }]), /% of the step above/g), 0);
    assert.ok(k.funnelHtml([{ label: 'A', v: 10 }, { label: 'B', v: 5 }]).includes('50% of the step above'));
  });

  test('panelHtml table option', () => {
    const k = kit();
    const withT = k.panelHtml('p1', 'Pages', '<p>x</p>', { table: { cols: ['Page', 'Visits'], rows: [['/', 1234]] } });
    assert.ok(withT.includes('Show as table'));
    assert.match(withT, /<div class="p-table" hidden><table/);
    assert.ok(withT.includes('data-kit-menu="p1"'));
    assert.ok(withT.includes('aria-label="Panel options for Pages"'));
    assert.ok(withT.includes('Copy data') && withT.includes('Open full size'));
    const without = k.panelHtml('p2', 'Pages', '<p>x</p>', {});
    assert.ok(!without.includes('Show as table'));
    assert.ok(!without.includes('<table'));
  });

  test('mapHtml colours only countries with data', () => {
    const k = kit();
    const out = k.mapHtml([{ code: 'US', v: 5 }], { US: 'United States' });
    assert.ok(out.includes('var(--s1)'));
    assert.ok(out.includes('var(--map-dot)'));
    assert.ok(out.includes('United States'));
    assert.ok(!out.includes('data-kit-act="only"'), 'a map without a key shows Copy only');
    assert.ok(out.includes('data-kit-act="copy"'));
    assert.ok(k.mapHtml([{ code: 'US', v: 5 }], { US: 'United States' }, { key: 'country' }).includes('data-key="country" data-val="US"'));
    assert.ok(!k.mapHtml([], {}).includes('var(--s1)'));
    assert.equal(count(out, /<circle /g), 8);
    assert.equal(count(out.match(/<g fill="var\(--s1\)"[^>]*>(.*?)<\/g>/)[1], /<circle /g), 3);
    assert.match(out, /viewBox="0 0 60 30"/);
  });

  test('panelHead matches panelHtml header', () => {
    const k = kit();
    const head = k.panelHead('a', 'T', {});
    assert.ok(k.panelHtml('a', 'T', '<p>').includes(head));
    assert.ok(!head.includes('class="grip"'));
    const grip = k.panelHead('a', 'T', { grip: true });
    assert.ok(grip.includes('class="grip"'));
    assert.ok(grip.indexOf('class="grip"') < grip.indexOf('class="p-title"'));
    assert.ok(k.panelHead('a', 'T', { table: true, note: 'n' }).includes('Show as table'));
    assert.ok(k.panelHead('a', 'T', { table: true, note: 'n' }).includes('<span class="p-note">n</span>'));
  });

  test('kpiHtml unchanged', () => {
    const k = kit();
    const o = { id: 'k', title: 'Visits', value: 1234, delta: { text: '5%', dir: 'up' }, sub: 'vs before', spark: [1, 2, 3] };
    assert.equal(k.kpiHtml(o), k.panelHtml(o.id, o.title, k.kpiBody(o), { cls: 'kpi' }));
  });

  test('rank without key has copy only', () => {
    const k = kit();
    const rows = [{ label: 'DE', v: 4 }];
    const without = k.rankHtml(rows, { dim: 'Country', n: 7 });
    assert.ok(!without.includes('data-kit-act="only"'));
    assert.ok(!without.includes('data-kit-act="out"'));
    assert.ok(without.includes('data-kit-act="copy"'));
    const withKey = k.rankHtml(rows, { dim: 'Country', key: 'country', n: 7 });
    assert.ok(withKey.includes('data-kit-act="only"'));
    assert.ok(withKey.includes('data-kit-act="out"'));
  });

  test('kitNiceMax steps', () => {
    const k = kit();
    for (const [m, want] of [[23, 50], [21, 50], [120, 200], [3, 4], [10, 10], [250, 500]]) assert.equal(k.kitNiceMax(m), want, String(m));
  });

  test('kitTimeKeys', () => {
    const k = kit();
    const days = [...k.kitTimeKeys('2026-10-30', '2026-11-02', 'day')];
    assert.equal(days.length, 4);
    assert.equal(days[0], '2026-10-30');
    assert.equal(days[3], '2026-11-02');
    assert.deepEqual([...k.kitTimeKeys('2025-11-15', '2026-02-01', 'month')], ['2025-11', '2025-12', '2026-01', '2026-02']);
    assert.deepEqual([...k.kitTimeKeys('2026-11-02', '2026-10-30', 'day')], []);
    assert.deepEqual([...k.kitTimeKeys('2026-11-02', '2026-10-30', 'month')], []);
    assert.equal(k.kitTimeKeys('2000-01-01', '2026-11-08', 'day').length, 4000);
  });

  test('kitBucket', () => {
    const k = kit();
    assert.equal(k.kitBucket('2026-11-08', 'day'), '2026-11-08');
    assert.equal(k.kitBucket('2026-11-08', 'month'), '2026-11');
  });

  test('feedHtml ages and escapes', () => {
    const k = kit();
    const now = Date.parse('2026-11-08T12:00:00Z');
    const out = k.feedHtml([{ at: '2026-11-08T09:00:00Z', kind: 'Website', title: '<i>', detail: 'd' }, { at: '2026-11-05T12:00:00Z', kind: 'Demo', title: 't', detail: 'd' }], now);
    assert.ok(out.includes('3 h ago'));
    assert.ok(out.includes('3 d ago'));
    assert.ok(out.includes('&lt;i&gt;'));
  });

  test('kit output has no hex colours', () => {
    const k = kit();
    const out = k.tsHtml({ id: 'h', labels: ['a', 'b'], mode: 'stack', series: [{ name: 'A', values: [1, 2], slot: 1 }] }) +
      k.kpiHtml({ id: 'k', title: 'Visits', value: 1234, delta: { text: '5%', dir: 'up' }, sub: 'vs before', spark: [1, 2, 3] }) +
      k.splitHtml([{ label: 'a', v: 1 }], {}) + k.mapHtml([{ code: 'DE', v: 1 }], {});
    assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(out));
  });

  // Every attribute value in the HTML is a closed "..." with no raw quote or angle bracket inside.
  function attrsSafe(html) {
    for (const tag of html.match(/<[a-z][^>]*>/g) || []) {
      const rest = tag.replace(/\s[a-zA-Z0-9-]+="[^"<>]*"/g, '').replace(/\shidden\b/g, '');
      assert.match(rest, /^<[a-z0-9]+\s*\/?>$/, 'broken tag: ' + tag);
    }
  }

  test('quotes and angle brackets stay inside attributes', () => {
    const k = kit();
    const bad = 'a"b<c';
    const rank = k.rankHtml([{ label: bad, v: 2 }], { dim: 'Page', key: 'path', n: 7 });
    assert.ok(rank.includes('data-val="a&quot;b&lt;c"'));
    assert.ok(rank.includes('aria-label="Only show a&quot;b&lt;c"'));
    attrsSafe(rank);
    const ts = k.tsHtml({ id: 'q', labels: [bad], mode: 'line', series: [{ name: bad, values: [1], slot: 1 }] });
    assert.ok(ts.includes('data-kit-series="q|a&quot;b&lt;c"'));
    const json = ts.match(/data-kit-ts="([^"]*)"/)[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
    assert.equal(JSON.parse(json).series[0].name, bad);
    attrsSafe(ts);
    const feed = k.feedHtml([{ at: '2026-11-08T09:00:00Z', kind: bad, title: bad, detail: bad }], Date.parse('2026-11-08T12:00:00Z'));
    assert.ok(!feed.includes(bad));
    attrsSafe(feed);
  });

  test('kitCsv quotes fields and neutralises formulas', () => {
    const k = kit();
    assert.equal(k.kitCsv([['a,b', 'say "hi"', 'x\ny'], ['plain', 12, -5]]), '"a,b","say ""hi""","x\ny"\nplain,12,-5');
    assert.equal(k.kitCsv([['=SUM(A1)', '+1', '-1', '@x', '\tt', '\rr']]), "'=SUM(A1),'+1,'-1,'@x,'\tt,\"'\rr\"");
  });

  test('tsHtml draws at the given width', () => {
    const k = kit();
    const o = { id: 'w', labels: ['a', 'b'], series: [{ name: 'A', values: [1, 2], slot: 1 }] };
    assert.match(k.tsHtml({ ...o, w: 1000, height: 260 }), /viewBox="0 0 1000 260"/);
    assert.match(k.tsHtml(o), /viewBox="0 0 600 220"/);
    assert.match(k.tsHtml({ ...o, w: 0 }), /viewBox="0 0 600 /);
    const json = JSON.parse(k.tsHtml({ ...o, w: 1000 }).match(/data-kit-ts="([^"]*)"/)[1].replace(/&quot;/g, '"'));
    assert.equal(json.w, 1000, 'the crosshair uses the same width');
  });

  test('kpiBody shortens a long text value', () => {
    const k = kit();
    const long = k.kpiBody({ value: 'news.ycombinator.com' });
    assert.match(long, /<span class="k-val long" title="news\.ycombinator\.com">news\.ycombinator\.com<\/span>/);
    assert.match(k.kpiBody({ value: 'github.com' }), /<span class="k-val">github\.com<\/span>/);
    assert.doesNotMatch(k.kpiBody({ value: 1234567 }), /long/);
  });

  test('kitTable aligns only number columns right', () => {
    const k = kit();
    const out = k.kitTable(['Kind', 'Value', 'Visits', 'Share', 'Change', 'Note'],
      [['Device', 'desktop', 3, '40%', '+4', 7], ['OS', 'Linux', '', '—', '−1,200.5', 'source']]);
    assert.ok(out.includes('<th>Kind</th><th>Value</th><th class="num">Visits</th><th class="num">Share</th><th class="num">Change</th><th>Note</th>'));
    assert.ok(out.includes('<td>Device</td><td>desktop</td><td class="num" data-v="3">3</td><td class="num">40%</td><td class="num">+4</td><td data-v="7">7</td>'));
    assert.ok(out.includes('<td>OS</td><td>Linux</td><td class="num"></td><td class="num">—</td><td class="num">−1,200.5</td><td>source</td>'));
  });
});
