// #887: llama card descriptor carries the tune chip from /api/llm/autotune/status.
import { describe, it, expect } from 'vitest';
import { srcFile, fnSrc, runHarness, flush } from './helpers/harness.js';

const SRC = srcFile('js/llmcontrol-models.js');

function boot(status) {
  const pre = `
    window._llmConfig = { 'org/m:Q4': { 'ctx-size': '8192' } }; window._benchData = {}; window._llmProfiles = {};
    window._llamaActiveSlots = 0; window.aliasOrShort = id => id; window._llamaFresh = () => null;
    window._llamaProfileHtml = () => ''; window._llamaPerfSeed = () => ({ gen: '—', ppt: '—', ts: '' });
    window.MC = { busyOf: () => null, isOpen: () => false, esc: s => String(s), age: () => '' };
    window.__fetches = [];
    window.fetch = (u) => { window.__fetches.push(String(u)); return Promise.resolve({ json: () => Promise.resolve(${JSON.stringify(status)}) }); };
    window._llamaTuneStatus = {}; window._llamaTuneStatusTs = 0;
  `;
  const fns = ['_llamaTuneFor', '_loadTuneStatus', '_llamaDescriptor'].map(n => {
    const s = fnSrc(SRC, n); if (!s) throw new Error(n + ' missing'); return s;
  }).join('\n');
  return runHarness({ sources: [pre, fns], bodyHtml: '<div></div>' });
}

describe('tune chip descriptor', () => {
  it('maps a stale status row to a warning chip with the reverify action', async () => {
    const win = boot({ ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true, ts: '2026-09-02T00:00:00Z', summary: { ctx_size: 8192 } }] });
    await win._loadTuneStatus(true); await flush();
    const d = win._llamaDescriptor('org/m:Q4', {});
    expect(d.tune).toEqual({ stale: true, label: 'tuned · stale', act: 'reverify',
      title: 'Autotuned on llama.cpp b100 · host now runs b120 — click to re-verify' });
  });
  it('maps a fresh status row to a muted chip and no row to null', async () => {
    const win = boot({ ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b120', current_build: 'b120', stale: false, ts: '2026-09-02T00:00:00Z', summary: {} }] });
    await win._loadTuneStatus(true); await flush();
    const d = win._llamaDescriptor('org/m:Q4', {});
    expect(d.tune).toEqual({ stale: false, label: 'tuned', act: 'autotune', title: 'Autotuned on llama.cpp b120 (2026-09-02)' });
    expect((d.menu || []).some(i => i && i.act === 'reverify')).toBe(false);
    expect(win._llamaDescriptor('org/other:Q4', {}).tune).toBeNull();
  });
  it('throttles the status fetch to once per minute unless forced', async () => {
    const win = boot({ ok: true, items: [] });
    await win._loadTuneStatus(); await win._loadTuneStatus(); await flush();
    expect(win.__fetches.length).toBe(1);
    await win._loadTuneStatus(true); await flush();
    expect(win.__fetches.length).toBe(2);
  });
  it('keeps the newest ts across two agents tuning the same model, regardless of array order', async () => {
    const win = boot({ ok: true, items: [
      { agent_id: 'newer', model_id: 'org/m:Q4', llama_build: 'b120', current_build: 'b130', stale: true, ts: '2026-09-05T00:00:00Z', summary: {} },
      { agent_id: 'older', model_id: 'org/m:Q4', llama_build: 'b90', current_build: 'b90', stale: false, ts: '2026-08-01T00:00:00Z', summary: {} },
    ] });
    await win._loadTuneStatus(true); await flush();
    expect(win._llamaDescriptor('org/m:Q4', {}).tune).toEqual({ stale: true, label: 'tuned · stale', act: 'reverify',
      title: 'Autotuned on llama.cpp b120 · host now runs b130 — click to re-verify' });
  });
  it('names a regressed re-verify rather than a build change', async () => {
    const win = boot({ ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b120', current_build: 'b120', stale: true, ts: '2026-09-02T00:00:00Z', summary: { regressed: true } }] });
    await win._loadTuneStatus(true); await flush();
    const d = win._llamaDescriptor('org/m:Q4', {});
    expect(d.tune.label).toBe('tuned · stale');
    expect(d.tune.title).toMatch(/ran slower than the tune/);
    expect(d.tune.title).not.toMatch(/host now runs/);
  });
  it('maps a null (unknown build) status row to a muted chip that can still re-verify', async () => {
    const win = boot({ ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: null, current_build: null, stale: null, ts: '2026-09-02T00:00:00Z', summary: {} }] });
    await win._loadTuneStatus(true); await flush();
    const d = win._llamaDescriptor('org/m:Q4', {});
    expect(d.tune).toEqual({ stale: false, label: 'tuned', act: 'reverify',
      title: 'Autotuned (2026-09-02) · llama.cpp build unknown — re-verify to confirm' });
    expect((d.menu || []).some(i => i && i.act === 'reverify')).toBe(true);
  });
  it('offers Re-verify tune in the menu for a stale row', async () => {
    const win = boot({ ok: true, items: [{ agent_id: 'a1', model_id: 'org/m:Q4', llama_build: 'b100', current_build: 'b120', stale: true, ts: '2026-09-02T00:00:00Z', summary: {} }] });
    await win._loadTuneStatus(true); await flush();
    const d = win._llamaDescriptor('org/m:Q4', {});
    expect((d.menu || []).some(i => i && i.act === 'reverify' && i.label === 'Re-verify tune')).toBe(true);
  });
});
