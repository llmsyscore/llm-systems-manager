// #412: manager host-agent designation + containerized AE restart button.
// Co-loads the real admin.js in jsdom and drives the functions for real.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');

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
  return dom.window;
}

// Render one agent row's caps/checkbox HTML with explicit board state.
// Only one checkbox can be "checked" in each scenario below, so a bare
// /type="checkbox" checked/ match unambiguously identifies the held box.
function render(globals, autoDetected, agent) {
  const boot = `
    _adminProviders = [
      { name:'llama', label:'llama.cpp', capability_key:'llama', sub_tab:'llamacpp' },
      { name:'vllm',  label:'vLLM',      capability_key:'vllm',  sub_tab:'vllm' },
    ];
    _adminPoolProviders = [{ name:'llama', label:'llama.cpp', pin_key:'llama_model_pins' }];
    _adminGlobal = ${JSON.stringify(globals)};
    _adminHostAutoDetected = ${autoDetected};
    window.__T = { html: _adminCapsAndPrimary(${JSON.stringify(agent)}) };
  `;
  return runHarness(boot).__T.html;
}

const approvedAgent = (id, extra) =>
  ({ agent_id: id, status: 'approved', is_host_agent: false, capabilities: { llama: true }, ...extra });

describe('#412 manager host toggle — exclusive visibility', () => {
  test('unheld + not auto-detected: shown (unchecked) on an approved agent', () => {
    const html = render({}, false, approvedAgent('agent-O'));
    expect(html).toContain("adminToggleHostAgent('agent-O',this.checked)");
    expect(html).not.toMatch(/type="checkbox" checked/);
  });
  test('designated: shown checked on the holder', () => {
    const html = render({ host_agent_id: 'agent-H' }, false,
                        approvedAgent('agent-H', { is_host_agent: true }));
    expect(html).toContain("adminToggleHostAgent('agent-H',this.checked)");
    expect(html).toMatch(/type="checkbox" checked/);
  });
  test('designated: hidden on every other agent', () => {
    const html = render({ host_agent_id: 'agent-H' }, false, approvedAgent('agent-O'));
    expect(html).not.toContain('adminToggleHostAgent');
  });
  test('auto-detected: hidden on all agents', () => {
    const html = render({}, true, approvedAgent('agent-O'));
    expect(html).not.toContain('adminToggleHostAgent');
  });
  test('pending (unapproved) agent: no host toggle', () => {
    const html = render({}, false, { agent_id: 'agent-P', status: 'pending',
                                     is_host_agent: false, capabilities: { llama: true } });
    expect(html).not.toContain('adminToggleHostAgent');
  });
});

describe('#412 primary checkbox — exclusive visibility', () => {
  test('unheld: shown on every capable agent', () => {
    const html = render({}, false, approvedAgent('agent-O', { capabilities: { llama: true, vllm: true } }));
    expect(html).toContain("adminTogglePrimary('agent-O','llama',this.checked)");
    expect(html).toContain("adminTogglePrimary('agent-O','vllm',this.checked)");
  });
  test('held: checked on the primary, hidden on others', () => {
    const holder = render({ primary_llama_id: 'agent-H' }, false, approvedAgent('agent-H'));
    const other = render({ primary_llama_id: 'agent-H' }, false, approvedAgent('agent-O'));
    expect(holder).toContain("adminTogglePrimary('agent-H','llama',this.checked)");
    expect(holder).toMatch(/type="checkbox" checked/);
    expect(other).not.toContain("adminTogglePrimary('agent-O','llama'");
  });
  test('a capability the agent lacks renders no primary checkbox', () => {
    const html = render({}, false, approvedAgent('agent-O'));   // llama only
    expect(html).not.toContain("adminTogglePrimary('agent-O','vllm'");
  });
});

describe('#412 pool checkbox stays multi-select', () => {
  test('offered on other pool-capable agents even when one is already a member', () => {
    const member = render({ llama_pool: ['agent-H'] }, false, approvedAgent('agent-H'));
    const other = render({ llama_pool: ['agent-H'] }, false, approvedAgent('agent-O'));
    expect(member).toContain("adminTogglePool('llama','agent-H',this.checked)");
    expect(member).toMatch(/type="checkbox" checked/);           // the member box
    expect(other).toContain("adminTogglePool('llama','agent-O',this.checked)");
    expect(other).not.toMatch(/type="checkbox" checked/);        // still offerable, unchecked
  });
});

// ── adminToggleHostAgent behavior (fetch + __MGR_AGENT update) ──
const BOOT_TOGGLE = `
  window.__T = { calls: [] };
  window.__MGR_AGENT = null;
  adminLoadAgents = function () { window.__T.reloaded = true; };
  _adminLog = function (m) { window.__T.log = m; };
  window.fetch = function (url, opts) {
    window.__T.calls.push({ url, opts });
    return Promise.resolve({ ok:true, json: () => Promise.resolve({ ok:true, host_agent_id:'agent-H' }) });
  };
  window.__T.run = adminToggleHostAgent('agent-H', true);
`;

describe('#412 adminToggleHostAgent — behavior', () => {
  test('posts {set:true} to host-role and repoints __MGR_AGENT', async () => {
    const win = runHarness(BOOT_TOGGLE);
    await win.__T.run;
    const call = win.__T.calls[0];
    expect(call.url).toBe('/api/agents/agent-H/host-role');
    expect(call.opts.method).toBe('POST');
    expect(JSON.parse(call.opts.body)).toEqual({ set: true });
    expect(win.__MGR_AGENT).toBe('agent-H');
    expect(win.__T.reloaded).toBe(true);
  });
});

// ── AE restart button gating in the system-health card ──
const BOOT_HEALTH = `
  _fmtUptime = function (s) { return String(s) + 's'; };  // lives in dashboard-manager.js
  ['adminHealthOverall','adminHealthRefresh','adminHealthServices','adminHealthDataFlow','adminHealthWarnings']
    .forEach(id => { const d = document.createElement('div'); d.id = id; document.body.appendChild(d); });
  const base = { overall:'ok', manager:{ uptime_s:10 },
                 services:[{ name:'alarm_engine', ok:true, latency_ms:5 }],
                 data_flow:{}, warnings:[] };
  const svcHtml = () => document.getElementById('adminHealthServices').innerHTML;
  window.__T = {};
  _renderSystemHealth({ ...base, ae_local:false, containerized:false }); window.__T.noBtn        = svcHtml();
  _renderSystemHealth({ ...base, ae_local:false, containerized:true  }); window.__T.containerBtn = svcHtml();
  _renderSystemHealth({ ...base, ae_local:true,  containerized:false }); window.__T.localBtn     = svcHtml();
`;

describe('#412 AE restart button gating', () => {
  const T = runHarness(BOOT_HEALTH).__T;
  test('no AE restart button when neither ae_local nor containerized', () => {
    expect(T.noBtn).not.toContain('data-restart-svc="alarm_engine"');
  });
  test('AE restart button shows under a containerized control plane', () => {
    expect(T.containerBtn).toContain('data-restart-svc="alarm_engine"');
  });
  test('AE restart button still shows for a local bare-metal AE unit', () => {
    expect(T.localBtn).toContain('data-restart-svc="alarm_engine"');
  });
});
