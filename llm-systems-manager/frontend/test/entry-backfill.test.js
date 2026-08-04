// #506: views whose live poll is view-gated must re-backfill their charts on
// entry, otherwise the off-tab interval is drawn as a straight fabricated line.
import { describe, test, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = (f) => readFileSync(join(here, '..', f), 'utf8');
const boot = src('js/boot.js');
const foundation = src('js/foundation.js');
const overall = src('js/overall.js');
const charts = src('js/charts.js');

// Slice a switchSubTab entry branch: from its `if (sub === '<name>')` to the
// end of that block, so assertions can't match a neighbouring provider.
function subTabBranch(name) {
  const start = boot.indexOf(`if (sub === '${name}')`);
  expect(start, `${name} entry branch not found`).toBeGreaterThan(-1);
  return boot.slice(start, start + 500);
}

describe('LM Studio sub-tab entry (#506)', () => {
  test('dashboard entry backfills history before resuming the live poll', () => {
    const branch = subTabBranch('lmstudio');
    expect(branch).toContain('loadLmsHistory().finally');
    expect(branch).toContain('fetchLMStudioMetrics');
  });
  test('backfill is gated to the dashboard parent (llm-tab panel has no charts)', () => {
    expect(subTabBranch('lmstudio')).toContain("parent === 'dashboard'");
  });
});

describe('Manager sub-tab entry (#506)', () => {
  const branch = boot.slice(boot.indexOf("sub === 'manager'"), boot.indexOf("sub === 'manager'") + 600);
  test('re-backfills the perf sparklines on every entry', () => {
    expect(branch).toContain('loadManagerPerfHistory');
  });
  test('the one-shot _mgrPerfBackfilled latch is gone', () => {
    expect(boot).not.toContain('_mgrPerfBackfilled');
  });
});

describe('Overall tab entry (#506)', () => {
  test('switchTab overall backfills before the live fleet fetch', () => {
    const m = foundation.match(/function switchTab\(tab\) \{[\s\S]*?\n\}/);
    expect(m).toBeTruthy();
    expect(m[0]).toContain('loadOverallHistory().finally');
    expect(m[0]).toContain('fetchOverallMetrics');
  });
  test('the one-shot _ovHistoryBackfilled latch is gone', () => {
    expect(overall).not.toContain('_ovHistoryBackfilled');
  });
  test('fetchOverallMetrics no longer backfills on the live path', () => {
    const fn = overall.slice(overall.indexOf('async function fetchOverallMetrics'));
    expect(fn.slice(0, fn.indexOf('\n}'))).not.toContain('loadOverallHistory');
  });
});

describe('_makeHistoryBackfill pre-fetch clear (#507)', () => {
  const factory = charts.slice(charts.indexOf('function _makeHistoryBackfill'));
  const body = factory.slice(0, factory.indexOf('\n}'));
  test('clears before the fetch only when the selected agent changed', () => {
    expect(body).toMatch(/if \(agent !== lastAgent\) resetCharts\(\);/);
  });
  // An unconditional pre-fetch reset blanks the window when the fetch fails.
  test('has no unconditional pre-fetch resetCharts call', () => {
    const preFetch = body.slice(0, body.indexOf('await fetch'));
    expect(preFetch).not.toMatch(/^\s*resetCharts\(\);/m);
  });
  test('still repaints from a clean slate once rows arrive', () => {
    const postFetch = body.slice(body.indexOf('await fetch'));
    expect(postFetch).toContain('resetCharts()');
  });
});

// llama.cpp is excluded because fetchMetrics has no view gate (#129); adding
// one there would require an entry backfill too.
describe('llama.cpp needs no entry backfill (#506)', () => {
  test('fetchMetrics has no view gate before its fetch', () => {
    const fn = charts.slice(charts.indexOf('async function fetchMetrics'));
    const preamble = fn.slice(0, fn.indexOf("_fetchT('/api/metrics'"));
    expect(preamble).not.toMatch(/_activeTab|_subTabState|ViewActive/);
  });
});
