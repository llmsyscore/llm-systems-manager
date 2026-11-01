// #1393: an open trend panel follows the shown card (new run, agent switch).
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const code = readFileSync(resolve(ROOT, 'js/report-card.js'), 'utf8');

const tick = () => new Promise(r => setTimeout(r, 0));
const card = (model, ts) => ({ provider: 'llama', ts, result: { model } });

function mountDom() {
  document.body.innerHTML = `
    <select id="rcProvider"><option value="llama" selected>llama.cpp</option></select>
    <select id="rcAgent"><option value="agentA" selected>A</option>
      <option value="agentB">B</option></select>
    <div id="rcNote"></div>
    <div id="rcCardHost"></div><div id="rcCompareHost"></div>
    <div id="rcActions" style="display:none;"></div>
    <div id="rcTrends" style="display:none;"><canvas id="rcTrendChart"></canvas></div>`;
}

function loadModule() {
  const charts = [];
  class FakeChart {
    constructor(_ctx, cfg) { this.labels = cfg.data.labels; this.destroyed = false; charts.push(this); }
    destroy() { this.destroyed = true; }
  }
  vi.stubGlobal('Chart', FakeChart);
  vi.stubGlobal('cssVar', () => '#000');
  vi.stubGlobal('RC', {
    buildCard: () => document.createDocumentFragment(),
    submitUrl: () => '',
    trendSeries: cards => ({ labels: cards.map(c => c.ts), gen: [], prefill: [], tpj: [] }),
  });
  HTMLCanvasElement.prototype.getContext = () => ({});
  const pending = [];
  vi.stubGlobal('fetch', vi.fn(url => new Promise(res => pending.push({ url, res }))));
  const fn = new Function(code + `
    ;return { rcRenderCard, rcToggleTrends, rcLoadLatest };`);
  const reply = (match, body) => {
    const i = pending.findIndex(p => p.url.includes(match));
    const [p] = pending.splice(i, 1);
    p.res({ ok: true, json: async () => body });
  };
  return { api: fn(), charts, pending, reply };
}

beforeEach(() => {
  vi.unstubAllGlobals();
  mountDom();
});

describe('report card trends', () => {
  it('reloads an open trend when a new card is shown', async () => {
    const { api, charts, pending, reply } = loadModule();
    api.rcRenderCard(card('m1', 1));
    pending.length = 0;
    api.rcToggleTrends();
    reply('history', { cards: [{ ts: 1 }] });
    await tick(); await tick();
    expect(charts.at(-1).labels).toEqual([1]);
    api.rcRenderCard(card('m1', 2));
    expect(pending.some(p => p.url.includes('/api/reportcard/history'))).toBe(true);
    pending.filter(p => p.url.includes('history')).forEach(() => reply('history', { cards: [{ ts: 1 }, { ts: 2 }] }));
    await tick(); await tick();
    expect(charts.at(-1).labels).toEqual([1, 2]);
  });

  it('does not load the trend for a new card while the panel is closed', () => {
    const { api, pending } = loadModule();
    api.rcRenderCard(card('m1', 1));
    expect(pending.filter(p => p.url.includes('history')).length).toBe(0);
  });

  it('clears an open trend when the picked agent has no card', async () => {
    const { api, charts, pending, reply } = loadModule();
    api.rcRenderCard(card('m1', 1));
    pending.length = 0;
    api.rcToggleTrends();
    reply('history', { cards: [{ ts: 1 }] });
    await tick(); await tick();
    document.getElementById('rcAgent').value = 'agentB';
    api.rcLoadLatest();
    reply('latest', { card: null });
    await tick(); await tick();
    expect(charts.at(-1).destroyed).toBe(true);
  });

  it('ignores a trend reply that arrives after the shown card changed', async () => {
    const { api, charts, pending } = loadModule();
    api.rcRenderCard(card('m1', 1));
    pending.length = 0;
    api.rcToggleTrends();
    const stale = pending.find(p => p.url.includes('history'));
    document.getElementById('rcAgent').value = 'agentB';
    api.rcRenderCard(card('m2', 5));
    stale.res({ ok: true, json: async () => ({ cards: [{ ts: 1 }] }) });
    await tick(); await tick();
    expect(charts.length).toBe(0);
  });
});
