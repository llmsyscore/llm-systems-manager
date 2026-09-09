// #885: a Benchmark "Add to Report Card" deep link pre-fills the model filter.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const code = readFileSync(resolve(ROOT, 'js/report-card.js'), 'utf8');

const tick = () => new Promise(r => setTimeout(r, 0));

function mountDom() {
  document.body.innerHTML = `
    <select id="rcProvider"><option value="llama" selected>llama.cpp</option></select>
    <select id="rcAgent"><option value="${'a'.repeat(32)}" selected>host</option></select>
    <select id="rcMode"><option value="standard" selected>Standard</option>
      <option value="custom">Custom</option></select>
    <div id="rcModelKeyField"><select id="rcModelKey">
      <option value="small" selected>small</option></select></div>
    <div id="rcCustomModelField"><input id="rcCustomModel" list="rcModelOptions">
      <datalist id="rcModelOptions"></datalist></div>
    <input id="rcPrice" value="0.15">
    <button id="rcRunBtn">▶ Run report card</button>
    <button id="rcCancelBtn" style="display:none;">✕ Cancel</button>
    <div id="rcNote"></div><div id="rcStatus" style="display:none;"></div>
    <div id="rcProgress" style="display:none;"></div>
    <div id="rcCardHost"></div>
    <div id="rcActions" style="display:none;">
      <span id="rcSubmitWrap" style="display:none;"><button id="rcSubmitBtn" disabled></button></span></div>
    <div id="rcDownload" style="display:none;"><div id="rcDownloadMsg"></div></div>
    <div id="rcCleanup" style="display:none;"><div id="rcCleanupMsg"></div>
      <button id="rcCleanupDeleteBtn"></button></div>
    <div id="rcConfirm" style="display:none;">
      <b id="rcConfirmServed"></b><span id="rcConfirmRef"></span></div>
    <div id="rcTrends" style="display:none;"><canvas id="rcTrendChart"></canvas></div>`;
}

function loadModule() {
  vi.stubGlobal('EventSource', class { constructor() {} close() {} });
  vi.stubGlobal('SG', { open: () => ({ close() {} }) });
  vi.stubGlobal('RC', {
    PROVIDER_LABEL: { llama: 'llama.cpp' },
    buildCard: () => document.createDocumentFragment(),
    submitUrl: () => '',
    trendSeries: () => ({ labels: [], gen: [], prefill: [], tpj: [] }),
  });
  const fetchMock = vi.fn(async (url) => ({
    ok: true,
    json: async () => {
      const u = String(url);
      if (u.startsWith('/api/reportcard/latest')) return { card: null };
      if (u.startsWith('/api/agents/list-by-provider')) {
        return { llama: [{ agent_id: 'a'.repeat(32), hostname: 'host', is_default: true }] };
      }
      return { ok: true };
    },
  }));
  vi.stubGlobal('fetch', fetchMock);
  const fn = new Function(code + `
    ;return { initReportCard, rcLoadLatest, rcOnModeChange, rcOnAgentChange };`);
  return { api: fn(), fetchMock };
}

beforeEach(() => {
  vi.unstubAllGlobals();
  mountDom();
});

describe('Report Card deep-link model filter (#885)', () => {
  it('pre-fills Custom mode and the model field, and filters rcLoadLatest', async () => {
    const { api, fetchMock } = loadModule();
    api.initReportCard('org/m:Q4');
    await tick(); await tick();
    expect(document.getElementById('rcMode').value).toBe('custom');
    expect(document.getElementById('rcCustomModel').value).toBe('org/m:Q4');
    const call = fetchMock.mock.calls.find(c => String(c[0]).startsWith('/api/reportcard/latest'));
    expect(call).toBeTruthy();
    expect(String(call[0])).toContain('&model=org%2Fm%3AQ4');
  });

  it('clears the filter on a plain re-init', async () => {
    const { api, fetchMock } = loadModule();
    api.initReportCard('org/m:Q4');
    await tick(); await tick();
    fetchMock.mockClear();
    api.initReportCard();
    await tick(); await tick();
    const call = fetchMock.mock.calls.find(c => String(c[0]).startsWith('/api/reportcard/latest'));
    expect(call).toBeTruthy();
    expect(String(call[0])).not.toContain('&model=');
    expect(document.getElementById('rcMode').value).toBe('standard');
    expect(document.getElementById('rcCustomModel').value).toBe('');
  });
});
