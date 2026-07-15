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

// ── Host-toggle render (no primaries/pools → the only "checked" is the toggle) ──
const BOOT_CAPS = `
  _adminProviders = [{ name:'llama', label:'llama.cpp', capability_key:'llama', sub_tab:'llamacpp' }];
  _adminPoolProviders = [];
  _adminGlobal = {};
  const HOST    = { agent_id:'agent-H', status:'approved', is_host_agent:true,  capabilities:{ llama:true } };
  const OTHER   = { agent_id:'agent-O', status:'approved', is_host_agent:false, capabilities:{ llama:true } };
  const PENDING = { agent_id:'agent-P', status:'pending',  is_host_agent:false, capabilities:{ llama:true } };
  window.__T = {
    host:    _adminCapsAndPrimary(HOST),
    other:   _adminCapsAndPrimary(OTHER),
    pending: _adminCapsAndPrimary(PENDING),
  };
`;

describe('#412 manager host toggle — render', () => {
  const T = runHarness(BOOT_CAPS).__T;
  test('approved agents get a manager-host checkbox', () => {
    expect(T.host).toContain("adminToggleHostAgent('agent-H',this.checked)");
    expect(T.other).toContain("adminToggleHostAgent('agent-O',this.checked)");
  });
  test('the designated host agent checkbox has the checked attribute', () => {
    // match the `checked` attribute, not `this.checked` in the onchange handler;
    // with no primaries/pools the host toggle is the only checkable input here.
    expect(T.host).toMatch(/type="checkbox" checked/);
  });
  test('a non-host agent checkbox is unchecked', () => {
    expect(T.other).not.toMatch(/type="checkbox" checked/);
  });
  test('a pending (unapproved) agent has no host toggle', () => {
    expect(T.pending).not.toContain('adminToggleHostAgent');
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
