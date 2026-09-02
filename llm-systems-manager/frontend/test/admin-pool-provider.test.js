// #359: admin pool/pins cards are provider-parameterized, not llama-hardcoded.
// Runs the real js/admin.js functions in jsdom (full-inject harness, same
// pattern as admin-dangling-refs.test.js) instead of grepping source text.
import { describe, it, expect } from 'vitest';
import { srcFile, runHarness as sharedRunHarness } from './helpers/harness.js';

const adminSrc = srcFile('js/admin.js');
const indexSrc = srcFile('index.html');

// Elements adminLoadAgents/adminRenderPoolOrder/adminRenderPins/
// adminLoadProviderModels touch.
const BODY = `
  <ul id="adminPoolOrderList"></ul>
  <div id="adminPoolResult"></div>
  <datalist id="adminProviderModels"></datalist>
  <div id="adminPinsResult"></div>
  <input id="adminPinModelInput">
  <select id="adminPinAgentSelect"><option value="agent-A">agent-A</option></select>
`;

function runHarness(bootstrap, bodyHtml = BODY) {
  return sharedRunHarness({ sources: [adminSrc], bootstrap, bodyHtml });
}

// Fetch stub: records every call, always resolves ok with empty JSON so
// mutation-triggered refresh cascades don't throw.
const FETCH_STUB = `
  window.__calls = [];
  window.fetch = (url) => {
    window.__calls.push(String(url));
    return Promise.resolve({ ok: true, json: async () => ({}) });
  };
`;

describe('provider-parameterized pool/pins admin UI', () => {
  it('stores pool_providers from /api/agents', async () => {
    const boot = `
      window.fetch = (url) => {
        if (String(url) === '/api/agents') {
          return Promise.resolve({ ok: true, json: async () => ({
            global: {},
            pool_providers: [
              { name: 'llama', label: 'llama.cpp', pin_key: 'llama_model_pins' },
              { name: 'vllm', label: 'vLLM', pin_key: 'vllm_model_pins' },
            ],
            agents: [],
          }) });
        }
        return Promise.resolve({ ok: true, json: async () => ({}) });
      };
      window.__done = adminLoadAgents().then(() => { window.__result = _adminPoolProviders; });
    `;
    const win = runHarness(boot);
    await win.__done;
    expect(win.__result.map(p => p.name)).toEqual(['llama', 'vllm']);
  });

  it('pool add/remove endpoint (adminTogglePool) uses the selected provider', async () => {
    const boot = `
      ${FETCH_STUB}
      window.__done = adminTogglePool('vllm', 'agent-9', true);
    `;
    const win = runHarness(boot);
    await win.__done;
    expect(win.__calls).toContain('/api/agents/agent-9/vllm-pool');
    expect(win.__calls.some(u => /llama-pool/.test(u))).toBe(false);
  });

  it('pool reorder commit (adminPoolReorderCommit) uses the selected provider', async () => {
    const boot = `
      ${FETCH_STUB}
      _adminPoolSel = 'vllm';
      _adminGlobal = { vllm_pool: ['agent-A', 'agent-B'] };
      document.getElementById('adminPoolOrderList').innerHTML =
        '<li data-agent-id="agent-B"></li><li data-agent-id="agent-A"></li>';
      window.__done = adminPoolReorderCommit();
    `;
    const win = runHarness(boot);
    await win.__done;
    expect(win.__calls).toContain('/api/agents/agent-B/vllm-pool');
    expect(win.__calls).toContain('/api/agents/agent-A/vllm-pool');
    expect(win.__calls.some(u => /llama-pool/.test(u))).toBe(false);
  });

  it('pin add/clear endpoints (adminAddPin, adminClearPin) use the selected provider', async () => {
    const boot = `
      ${FETCH_STUB}
      _adminPinsSel = 'vllm';
      document.getElementById('adminPinModelInput').value = 'some-model';
      document.getElementById('adminPinAgentSelect').value = 'agent-A';
      window.__done = Promise.all([adminAddPin(), adminClearPin('some-model')]);
    `;
    const win = runHarness(boot);
    await win.__done;
    expect(win.__calls).toContain('/api/admin/vllm-pins');
    expect(win.__calls.some(u => /llama-pins/.test(u))).toBe(false);
  });

  it('provider models endpoint (adminLoadProviderModels) uses the selected provider', async () => {
    const boot = `
      ${FETCH_STUB}
      _adminPinsSel = 'vllm';
      window.__done = adminLoadProviderModels();
    `;
    const win = runHarness(boot);
    await win.__done;
    expect(win.__calls).toContain('/api/admin/vllm-models');
    expect(win.__calls.some(u => /llama-models/.test(u))).toBe(false);
  });

  it('index.html provider chip containers render real per-provider chips via adminRenderProviderChips', () => {
    // Pull the actual container markup out of index.html rather than
    // hand-typing it, so a renamed/removed id fails the extraction itself.
    const poolChips = indexSrc.match(/<div id="adminPoolProviderChips"[^>]*><\/div>/);
    const pinsChips = indexSrc.match(/<div id="adminPinsProviderChips"[^>]*><\/div>/);
    expect(poolChips, 'adminPoolProviderChips container not found in index.html').toBeTruthy();
    expect(pinsChips, 'adminPinsProviderChips container not found in index.html').toBeTruthy();

    const boot = `
      _adminPoolProviders = [
        { name: 'llama', label: 'llama.cpp', pin_key: 'llama_model_pins' },
        { name: 'vllm', label: 'vLLM', pin_key: 'vllm_model_pins' },
      ];
      _adminPoolSel = 'llama';
      _adminPinsSel = 'vllm';
      adminRenderProviderChips('pool');
      adminRenderProviderChips('pins');
      window.__poolHtml = document.getElementById('adminPoolProviderChips').innerHTML;
      window.__pinsHtml = document.getElementById('adminPinsProviderChips').innerHTML;
    `;
    const win = runHarness(boot, poolChips[0] + pinsChips[0]);
    // Both providers rendered as chips in the pool row, current selection marked primary.
    expect(win.__poolHtml).toContain("adminSelectProvider('pool','llama')");
    expect(win.__poolHtml).toContain("adminSelectProvider('pool','vllm')");
    expect(win.__poolHtml).toMatch(/class="adm-chip primary"[^>]*>\s*llama\.cpp/);
    // Pins row selection follows _adminPinsSel independently of the pool row.
    expect(win.__pinsHtml).toContain("adminSelectProvider('pins','vllm')");
    expect(win.__pinsHtml).toMatch(/class="adm-chip primary"[^>]*>\s*vLLM/);
  });

  // wiring (unexecutable): source-text check of the cache-bust query string.
  it('admin.js has a cache-bust query in index.html', () => {
    expect(indexSrc).toMatch(/js\/admin\.js\?v=[\w.-]+/);
  });
});

describe('no hardcoded llama-only endpoints remain', () => {
  it('admin.js source is free of llama-pool/-pins/-models literals', () => {
    // wiring (unexecutable): absence must hold in paths no harness drives.
    expect(adminSrc).not.toMatch(/llama-pool|llama-pins|llama-models/);
  });
});
