// #374: admin primary sliders, open-control shortcuts, and the
// provider->sub-tab jump map are registry-driven, not llama/lms/vllm-hardcoded.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');
const agentsSrc = readFileSync(join(here, '..', 'js', 'admin-agents.js'), 'utf8');
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
  inject(agentsSrc);
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
  window.__T.capsA = AgentsView.capsHtml(A) + AgentsView.drawerHtml(A);
  window.__T.capsB = AgentsView.capsHtml(B) + AgentsView.drawerHtml(B);
  _jumpToDashboard('agent-A', 'tgi');
  _jumpToDashboard('agent-A', 'vllm');
  _jumpToDashboard('agent-A', 'unknownprov');
`;

describe('#374 registry-driven admin provider UI — source', () => {
  test('no hardcoded per-provider primary vars remain', () => {
    expect(adminSrc).not.toMatch(/isPrimaryLlama|isPrimaryLms|isPrimaryVllm/);
    expect(adminSrc).not.toMatch(/llamaDisabled|lmsDisabled|vllmDisabled/);
  });
  test('shortcuts no longer hardcode the provider array', () => {
    expect(agentsSrc).not.toMatch(/\['llama',\s*'lms',\s*'vllm'\]\.filter/);
    expect(agentsSrc).toMatch(/c\.providers\.filter\(p => caps\[p\.capability_key\]\)/);
  });
  test('_jumpToDashboard derives sub-tab from the registry, not a ternary', () => {
    expect(adminSrc).not.toMatch(/provider === 'lms' \? 'lmstudio'/);
    expect(adminSrc).toMatch(/_adminProviders\.find\(p => p\.name === provider\)/);
  });
  test('dead adminPrimaryCell function is gone', () => {
    expect(adminSrc).not.toContain('function adminPrimaryCell');
  });
  test('primary sliders loop over the provider registry', () => {
    expect(agentsSrc).toMatch(/for \(const p of c\.providers\)/);
    expect(adminSrc).not.toContain('_adminCapsAndPrimary');
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

  test('a new provider (tgi) gets its primary slider automatically', () => {
    expect(T.capsA).toContain('data-act="primary" data-prov="tgi" data-aid="agent-A"');
    expect(T.capsA).toContain('Primary tgi');
  });
  test('a primary slider renders for each capability the agent has', () => {
    for (const p of ['llama', 'vllm', 'tgi']) {
      expect(T.capsA).toContain(`data-act="primary" data-prov="${p}" data-aid="agent-A"`);
    }
  });
  test('primary provider (tgi) slider is on', () => {
    expect(T.capsA).toMatch(/class="mc-toggle on" data-act="primary" data-prov="tgi"/);
  });
  test('a capability the agent lacks (lms) renders no primary slider', () => {
    expect(T.capsA).not.toContain('data-act="primary" data-prov="lms"');
  });
  test('primary llama capability chip gets the star; non-primary vllm does not', () => {
    expect(T.capsA).toMatch(/<span>llama<\/span><span class="star">★<\/span>/);
    expect(T.capsA).not.toMatch(/<span>vllm<\/span><span class="star">/);
  });
  test('a new provider (tgi) gets its open-control shortcut automatically', () => {
    expect(T.capsA).toContain('data-act="open" data-prov="tgi" data-aid="agent-A"');
  });
  test('open-control shortcuts only render for advertised capabilities', () => {
    expect(T.capsA).toContain('data-act="open" data-prov="vllm" data-aid="agent-A"');
    expect(T.capsA).not.toContain('data-act="open" data-prov="lms"');
  });
  test('unapproved agent renders no primary sliders or open-control shortcuts', () => {
    expect(T.capsB).not.toContain('data-act="primary"');
    expect(T.capsB).not.toContain('data-act="open"');
    expect(T.capsB).toContain('<span>llama</span>'); // capability chip still shows
  });
  test('_jumpToDashboard routes each provider to its registry sub_tab', () => {
    expect(T.jump).toContainEqual(['dashboard', 'tgi']);
    expect(T.jump).toContainEqual(['dashboard', 'vllm']);
  });
  test('_jumpToDashboard falls back to the provider name for unknown providers', () => {
    expect(T.jump).toContainEqual(['dashboard', 'unknownprov']);
  });
});
