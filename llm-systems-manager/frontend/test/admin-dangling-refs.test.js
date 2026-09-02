// #563: dangling agent-id references must not lock away admin controls —
// a deleted primary holder unhides the slider, unknown pool rows get ✕.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');
const agentsSrc = readFileSync(join(here, '..', 'js', 'admin-agents.js'), 'utf8');

function runHarness(bootstrap, bodyHtml = '') {
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${bodyHtml}</body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(adminSrc);
  inject(agentsSrc);
  inject(bootstrap);
  return dom.window;
}

const approvedAgent = (id, extra) =>
  ({ agent_id: id, status: 'approved', is_host_agent: false, capabilities: { llama: true }, ...extra });

// Render one agent's drawer (role sliders) with an explicit agents cache.
function render(globals, cache, agent) {
  const boot = `
    _adminProviders = [{ name:'llama', label:'llama.cpp', capability_key:'llama', sub_tab:'llamacpp' }];
    _adminPoolProviders = [{ name:'llama', label:'llama.cpp', pin_key:'llama_model_pins' }];
    _adminGlobal = ${JSON.stringify(globals)};
    _adminAgentsCache = ${JSON.stringify(cache)};
    _adminHostAutoDetected = false;
    window.__T = { html: AgentsView.drawerHtml(${JSON.stringify(agent)}) };
  `;
  return runHarness(boot).__T.html;
}

describe('#563 primary slider vs dangling holder', () => {
  test('holder exists in cache: hidden on other agents (unchanged behavior)', () => {
    const html = render({ primary_llama_id: 'agent-H' },
                        [approvedAgent('agent-H'), approvedAgent('agent-O')],
                        approvedAgent('agent-O'));
    expect(html).not.toContain('data-act="primary"');
  });
  test('dangling holder: slider returns on every capable agent', () => {
    const html = render({ primary_llama_id: 'ghost' },
                        [approvedAgent('agent-O')],
                        approvedAgent('agent-O'));
    expect(html).toContain('data-act="primary" data-prov="llama" data-aid="agent-O"');
    expect(html).not.toMatch(/class="mc-toggle on"/);
  });
  test('empty cache (not yet loaded): holder treated as known, still hidden', () => {
    const html = render({ primary_llama_id: 'agent-H' }, [], approvedAgent('agent-O'));
    expect(html).not.toContain('data-act="primary"');
  });
  test('dangling host holder: manager-host toggle returns', () => {
    const html = render({ host_agent_id: 'ghost' },
                        [approvedAgent('agent-O')],
                        approvedAgent('agent-O'));
    expect(html).toContain('data-act="host" data-aid="agent-O"');
  });
});

describe('#563 pool list rows for unknown agents', () => {
  function renderPool(pool, cache) {
    const boot = `
      Sortable = { create: () => ({ destroy(){} }) };
      _adminPoolProviders = [{ name:'llama', label:'llama.cpp', pin_key:'llama_model_pins' }];
      _adminProvSel = 'llama';
      _adminGlobal = ${JSON.stringify({ llama_pool: pool })};
      _adminAgentsCache = ${JSON.stringify(cache)};
      adminRenderPoolOrder();
      window.__T = { html: document.getElementById('adminPoolOrderList').innerHTML };
    `;
    return runHarness(boot, '<ul id="adminPoolOrderList"></ul>').__T.html;
  }

  test('known member renders the dashboard jump, not a remove button', () => {
    const html = renderPool(['agent-K'], [approvedAgent('agent-K', { hostname: 'k1' })]);
    expect(html).toContain('k1');
    expect(html).toContain('Open in Dashboard');
    expect(html).not.toContain('adminTogglePool');
  });
  test('unknown member gets a working remove button instead', () => {
    const html = renderPool(['ghost-1234'], [approvedAgent('agent-K', { hostname: 'k1' })]);
    expect(html).toContain('(unknown agent ghost-12');
    expect(html).toContain("adminTogglePool('llama','ghost-1234',false)");
    expect(html).toContain('Remove this deleted agent from the pool');
    expect(html).not.toContain('Open in Dashboard');
  });
});
