// #854: every hideable Dashboard sub-tab falls back to Manager when its
// provider/proxy goes away, OpenClaw included.
import { describe, it, expect } from 'vitest';
import { srcFile, blockSrc, evalGlobal } from './helpers/harness.js';

const chartsSrc = srcFile('js/charts.js');
const block = blockSrc(chartsSrc, '    // Same for Dashboard sub-tabs', "switchSubTab('dashboard','manager');");

function runFallback({ llamaOn = true, lmsOn = true, vllmOn = true, ocOn = true, remembered }) {
  const calls = [];
  window._subTabState = { dashboard: remembered };
  window.switchSubTab = (...a) => calls.push(a);
  window.__on = [llamaOn, lmsOn, vllmOn, ocOn];
  evalGlobal(`(function(llamaOn, lmsOn, vllmOn, ocOn) {\n${block}\n})(...window.__on);`);
  return calls;
}

describe('checkConfig dashboard sub-tab fallback (#854)', () => {
  it('leaves an OpenClaw sub-tab alone while the proxy is enabled', () => {
    expect(runFallback({ remembered: 'openclaw' })).toEqual([]);
  });

  it('falls back to Manager when the OpenClaw proxy is disabled', () => {
    expect(runFallback({ ocOn: false, remembered: 'openclaw' })).toEqual([['dashboard', 'manager']]);
  });

  it.each([
    ['llamacpp', { llamaOn: false }],
    ['lmstudio', { lmsOn: false }],
    ['vllm', { vllmOn: false }],
  ])('still falls back for %s', (sub, flags) => {
    expect(runFallback({ ...flags, remembered: sub })).toEqual([['dashboard', 'manager']]);
  });

  it('does not touch a sub-tab whose provider is still present', () => {
    expect(runFallback({ ocOn: false, remembered: 'llamacpp' })).toEqual([]);
  });

  it('never re-switches the always-visible sub-tabs', () => {
    const allOff = { llamaOn: false, lmsOn: false, vllmOn: false, ocOn: false };
    expect(runFallback({ ...allOff, remembered: 'manager' })).toEqual([]);
    expect(runFallback({ ...allOff, remembered: 'energy' })).toEqual([]);
  });
});
