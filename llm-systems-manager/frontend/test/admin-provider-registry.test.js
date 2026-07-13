// #374: admin primary checkboxes, view-dashboard buttons, and the
// provider->sub-tab jump map are registry-driven, not llama/lms/vllm-hardcoded.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');
const backendSrc = (f) => readFileSync(join(here, '..', '..', 'backend', f), 'utf8');

// Co-load admin.js + a bootstrap script in one window so the bootstrap can
// reassign admin.js's lexical state (_adminProviders/_adminGlobal) and invoke
// the render functions for real — catches logic bugs source regex can't.
function runHarness(bootstrap) {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>',
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(adminSrc);
  inject(bootstrap);
  return dom.window.__T;
}

const BOOTSTRAP = `
  _adminProviders = [
    { name: 'llama', label: 'llama.cpp', capability_key: 'llama', sub_tab: 'llamacpp' },
    { name: 'lms',   label: 'LM Studio', capability_key: 'lms',   sub_tab: 'lmstudio' },
    { name: 'vllm',  label: 'vLLM',      capability_key: 'vllm',  sub_tab: 'vllm' },
    { name: 'tgi',   label: 'TGI',       capability_key: 'tgi',   sub_tab: 'tgi' },
  ];
  _adminPoolProviders = [
    { name: 'llama', label: 'llama.cpp', pin_key: 'llama_model_pins' },
    { name: 'vllm',  label: 'vLLM',      pin_key: 'vllm_model_pins' },
  ];
  _adminGlobal = {
    primary_llama_id: 'agent-A', primary_tgi_id: 'agent-A',
    llama_pool: ['agent-A'], vllm_pool: [],
  };
  const A = { agent_id: 'agent-A', status: 'approved',
              capabilities: { llama: true, vllm: true, tgi: true } };
  const B = { agent_id: 'agent-B', status: 'pending',
              capabilities: { llama: true } };
  window.__T = { jump: [] };
  window.switchTab = function () {};
  window._selectAgent = function () {};
  window.switchSubTab = function (tab, sub) { window.__T.jump.push([tab, sub]); };
  window.__T.capsA = _adminCapsAndPrimary(A);
  window.__T.capsB = _adminCapsAndPrimary(B);
  _jumpToDashboard('agent-A', 'tgi');
  _jumpToDashboard('agent-A', 'vllm');
  _jumpToDashboard('agent-A', 'unknownprov');
`;

describe('#374 registry-driven admin provider UI — source', () => {
  test('no hardcoded per-provider primary vars remain', () => {
    expect(adminSrc).not.toMatch(/isPrimaryLlama|isPrimaryLms|isPrimaryVllm/);
    expect(adminSrc).not.toMatch(/llamaDisabled|lmsDisabled|vllmDisabled/);
  });
  test('viewBtns no longer hardcodes the provider array', () => {
    expect(adminSrc).not.toMatch(/\['llama',\s*'lms',\s*'vllm'\]\.filter/);
    expect(adminSrc).toMatch(/_adminProviders\.filter\(p => caps\[p\.capability_key\]\)/);
  });
  test('_jumpToDashboard derives sub-tab from the registry, not a ternary', () => {
    expect(adminSrc).not.toMatch(/provider === 'lms' \? 'lmstudio'/);
    expect(adminSrc).toMatch(/_adminProviders\.find\(p => p\.name === provider\)/);
  });
  test('dead adminPrimaryCell function is gone', () => {
    expect(adminSrc).not.toContain('function adminPrimaryCell');
  });
  test('primary checkboxes loop over _adminProviders', () => {
    expect(adminSrc).toMatch(/const primaryChecks = _adminProviders\.map/);
  });
});

describe('#374 backend /api/agents payload — source', () => {
  const src = backendSrc('agent_registry.py');
  test('emits a registry-driven providers array', () => {
    expect(src).toMatch(/"providers":/);
    expect(src).toContain('for n in providers.names()');
    expect(src).toContain('"capability_key": spec.capability_key');
    expect(src).toContain('spec.sub_tab_keys[0]');
  });
});

describe('#374 registry-driven admin provider UI — behavior', () => {
  const T = runHarness(BOOTSTRAP);

  test('a new provider (tgi) gets its primary checkbox automatically', () => {
    expect(T.capsA).toContain("adminTogglePrimary('agent-A','tgi',this.checked)");
    expect(T.capsA).toContain('primary tgi');
  });
  test('all four providers render a primary checkbox', () => {
    for (const p of ['llama', 'lms', 'vllm', 'tgi']) {
      expect(T.capsA).toContain(`adminTogglePrimary('agent-A','${p}',this.checked)`);
    }
  });
  test('primary provider (tgi) checkbox is checked + enabled', () => {
    expect(T.capsA).toContain('currently primary tgi host — uncheck to clear');
  });
  test('a capability the agent lacks (lms) is present but disabled', () => {
    expect(T.capsA).toMatch(/class="disabled"[^>]*title="agent has no lms capability"/);
  });
  test('primary llama capability chip gets the star; non-primary vllm does not', () => {
    expect(T.capsA).toContain('llama ★');
    expect(T.capsA).not.toContain('vllm ★');
  });
  test('a new provider (tgi) gets its view-dashboard button automatically', () => {
    expect(T.capsA).toContain("_jumpToDashboard('agent-A','tgi')");
  });
  test('view buttons only render for advertised capabilities', () => {
    expect(T.capsA).toContain("_jumpToDashboard('agent-A','vllm')");
    expect(T.capsA).not.toContain("_jumpToDashboard('agent-A','lms')");
  });
  test('unapproved agent renders no primary checkboxes or view buttons', () => {
    expect(T.capsB).not.toContain('adminTogglePrimary');
    expect(T.capsB).not.toContain('_jumpToDashboard');
    expect(T.capsB).toContain('llama'); // capability chip still shows
  });
  test('_jumpToDashboard routes each provider to its registry sub_tab', () => {
    expect(T.jump).toContainEqual(['dashboard', 'tgi']);
    expect(T.jump).toContainEqual(['dashboard', 'vllm']);
  });
  test('_jumpToDashboard falls back to the provider name for unknown providers', () => {
    expect(T.jump).toContainEqual(['dashboard', 'unknownprov']);
  });
});
