// #806: off-view (Admin, Events) the LMS and vLLM header pills still refresh
// on every 5th tick, updating only the pill.
import { describe, test, expect, beforeEach, afterEach, vi } from 'vitest';
import { srcFile, fnSrc, evalGlobal } from './helpers/harness.js';

const lmsSrc = srcFile('js/lmstudio.js');
const vllmSrc = srcFile('js/vllm.js');

function load(src, names) {
  for (const name of names) {
    const fn = fnSrc(src, name);
    expect(fn, `${name} not found`).toBeTruthy();
    evalGlobal(fn + `\nwindow.${name} = ${name};`);
  }
}

beforeEach(() => {
  document.body.innerHTML = `
    <div id="lmsStateBanner" class="state-banner state-unknown"><span id="lmsStateText">LMS · —</span></div>
    <div id="vllmStateBanner" class="state-banner state-unknown"><span id="vllmStateText">VLLM · —</span></div>`;
  window._agentClaimKey = (k) => k;
  window._claims = [];
  window._claim = (k) => { window._claims.push(k); return true; };
  window._release = () => {};
  window._selectedAgent = undefined;
  window._pillHidden = (id) => document.getElementById(id).style.display === 'none';
});
afterEach(() => vi.restoreAllMocks());

describe('LMS header pill off-view', () => {
  beforeEach(() => {
    window._lmsPillTick = 0;
    window._lmsMetricsViewActive = () => false;
    window._fetchT = vi.fn(() => Promise.resolve({ json: async () => ({
      agent_online: true, ps: [{ model: 'nvidia/nemotron-3-nano-4b', status: 'IDLE' }], models: ['x'] }) }));
    load(lmsSrc, ['_updateLmsHeaderPill', 'fetchLMStudioMetrics']);
  });

  test('four off-view ticks fetch nothing; the fifth refreshes only the pill', async () => {
    for (let i = 0; i < 4; i++) await window.fetchLMStudioMetrics();
    expect(window._fetchT).not.toHaveBeenCalled();
    await window.fetchLMStudioMetrics();
    expect(window._fetchT).toHaveBeenCalledTimes(1);
    expect(window._fetchT.mock.calls[0][0]).toBe('/api/lmstudio/metrics?light=1');
    expect(window._claims).toEqual(['fetchLMStudioMetrics:pill']);
    expect(document.getElementById('lmsStateText').textContent).toBe('LMS · Idle · nvidia/nemotron-3-nano-4b');
    expect(document.getElementById('lmsStateBanner').className).toBe('state-banner state-sleeping');
    expect(window._lmsMetrics).toBeUndefined();
  });

  test('a hidden pill (no LMS agent) never fetches off-view', async () => {
    document.getElementById('lmsStateBanner').style.display = 'none';
    for (let i = 0; i < 10; i++) await window.fetchLMStudioMetrics();
    expect(window._fetchT).not.toHaveBeenCalled();
  });
});

describe('vLLM header pill off-view', () => {
  beforeEach(() => {
    window._vllmPillTick = 0;
    window._vllmMetricsViewActive = () => false;
    window._fetchT = vi.fn(() => Promise.resolve({ json: async () => ({
      agent_online: true, vllm: { state: 'running', model: 'org/mistral-small-24b' } }) }));
    load(vllmSrc, ['_updateVllmHeaderPill', 'fetchVllmMetrics']);
  });

  test('four off-view ticks fetch nothing; the fifth refreshes only the pill', async () => {
    for (let i = 0; i < 4; i++) await window.fetchVllmMetrics();
    expect(window._fetchT).not.toHaveBeenCalled();
    await window.fetchVllmMetrics();
    expect(window._fetchT).toHaveBeenCalledTimes(1);
    expect(window._fetchT.mock.calls[0][0]).toBe('/api/vllm/metrics?light=1');
    expect(window._claims).toEqual(['fetchVllmMetrics:pill']);
    expect(document.getElementById('vllmStateText').textContent).toBe('VLLM · Active · mistral-small-24b');
    expect(document.getElementById('vllmStateBanner').className).toBe('state-banner state-awake');
    expect(window._vllmMetrics).toBeUndefined();
  });
});
