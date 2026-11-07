// #1419: self-managed llama-server banner and downloaded-not-served cards.
import { describe, test, expect } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

const src = srcFile('js/llmcontrol-models.js');
const BLOCK = src.slice(src.indexOf('// ===== Llama mode banner (#1419) begin'),
                        src.indexOf('// ===== Llama mode banner (#1419) end'));
const HELP = 'https://github.com/llmsyscore/llm-systems-manager/blob/main/docs/DEPLOYMENT.md#using-a-llama-server-you-installed-yourself';

function run(mode, extraBootstrap = '') {
  return runHarness({
    sources: [`
      function _esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
      function adminEsc(s) { return _esc(s); }
      function shortName(s) { return String(s).split('/').pop(); }
      var _llmModels = [{ id: 'org/Qwen3' }];
      var _llmConfig = {};
      var __toasts = [], __posts = [], __confirm = true, __alerts = [];
      function _toastErr(m) { __toasts.push(m); }
      function _themedConfirm() { return Promise.resolve(__confirm); }
      function _themedAlert(o) { __alerts.push(o); return Promise.resolve(); }
      async function refreshLLMTab() { window.__refreshed = true; }
      ` + BLOCK],
    bodyHtml: '<div id="c"></div>',
    bootstrap: `renderModeBanner(${JSON.stringify(mode)}, document.getElementById('c'));` + extraBootstrap,
  });
}
const btns = (w) => [...w.document.querySelectorAll('#c button, #c a')].map(b => b.textContent);
const text = (w) => w.document.getElementById('c').textContent;
const flush = () => new Promise(r => setTimeout(r, 0));

describe('renderModeBanner', () => {
  test('renders single-model banner with switch and help buttons', () => {
    const w = run({ mode: 'single', managed: true, unit: 'llama.service' });
    expect(text(w)).toContain('This llama-server serves one fixed model (Qwen3).');
    expect(btns(w)).toEqual(['Switch to multi-model mode', 'Show me how']);
    expect(w.document.querySelector('[data-act="mode-help"]').getAttribute('href')).toBe(HELP);
    expect(w.document.querySelector('[data-act="mode-help"]').getAttribute('target')).toBe('_blank');
    expect(w.document.querySelector('[data-act="mode-help"]').getAttribute('rel')).toBe('noopener');
  });
  test('single model set up through environment variables omits the switch button', () => {
    const w = run({ mode: 'single', managed: true, configured_by: 'environment' });
    expect(btns(w)).toEqual(['Show me how']);
    expect(text(w)).toContain('edit its service file by hand.');
  });
  test('renders no-presets banner with add-presets button', () => {
    const w = run({ mode: 'router', managed: true, presets_file: '' });
    expect(text(w)).toContain('has no presets file');
    expect(btns(w)).toEqual(['Add a presets file']);
  });
  test('renders different-user banner without buttons', () => {
    const w = run({ mode: 'single', managed: true, unit_user: 'bob', agent_user: 'llmsys' });
    expect(text(w)).toContain('llama-server runs as bob; the agent runs as llmsys and cannot see its models');
    expect(text(w)).toContain('serves one fixed model');
    expect(w.document.querySelector('[data-act="mode-switch"]')).toBeNull();
    const r = run({ mode: 'single', managed: true, unit_user: 'root', agent_user: 'llmsys' });
    expect(text(r)).toContain('llama-server runs as root, so the agent (llmsys) cannot change its service. Add User=llmsys');
    expect(text(r)).not.toContain('Run both as the same user');
    expect(btns(w)).toEqual(['Show me how']);
  });
  test('no different-user banner when the agent user is unknown', () => {
    const w = run({ mode: 'router', managed: true, presets_file: '/p.ini', unit_user: 'bob' });
    expect(w.document.getElementById('c').innerHTML).toBe('');
  });
  test('renders detected-settings banner with save button', () => {
    const w = run({ mode: 'router', managed: true, presets_file: '/p.ini', settings_source: 'detected',
                    unit: 'llama.service', api_url: 'http://127.0.0.1:1235' });
    expect(text(w)).toContain('(port 1235, unit llama.service)');
    expect(btns(w)).toEqual(['Save']);
  });
  test('renders not-a-service notice without buttons', () => {
    const w = run({ mode: 'router', managed: false, presets_file: '/p.ini' });
    expect(text(w)).toContain('not a system service');
    expect(btns(w)).toEqual([]);
  });
  test('renders nothing in router mode with saved settings', () => {
    const w = run({ mode: 'router', managed: true, presets_file: '/p.ini', settings_source: 'saved' });
    expect(w.document.getElementById('c').innerHTML).toBe('');
  });
  test('renders nothing for an empty mode', () => {
    expect(run({}).document.getElementById('c').innerHTML).toBe('');
  });
  test('a 409 shows the agent detail and a 500 appends the manual-restart hint', async () => {
    const w = run({}, `
      _llamaMode.setExtra({ mode: { mode: 'single', managed: true }, cached: [] });
      var __resp = { ok: false, status: 409, json: () => Promise.resolve({ reason: 'environment', detail: 'Set by env' }) };
      window.fetch = () => Promise.resolve(__resp);
      document.body.innerHTML = '<div id="cards"></div>';
      _llamaMode.render(document.getElementById('cards'));`);
    const sw = () => w.document.querySelector('#llmModeBanner [data-act="mode-switch"]').click();
    sw(); await flush(); await flush();
    expect(w.__toasts).toEqual(['Set by env']);
    w.eval(`__resp = { ok: false, status: 500, json: () => Promise.resolve({ error: 'boom', restored: false }) }`);
    sw(); await flush(); await flush();
    expect(w.__toasts[1]).toBe('boom — the service may need a manual restart');
  });
});

describe('cached rows', () => {
  const ROWS = [
    { repo: 'a/b', file: 'x.gguf', path: '/m/x.gguf', model_id: 'a/b:Q4_K_M', deployable: true, source: 'hf-hub' },
    { repo: 'c/d', file: 'y.gguf', path: '/m/y.gguf', model_id: 'c/d:Q8_0', deployable: false },
  ];
  test('cached row shows Deploy only when deployable', () => {
    const w = run({}, `document.body.innerHTML = '<div id="cards"></div>';
      _llamaMode.setExtra({ mode: { mode: 'single', managed: true }, cached: ${JSON.stringify(ROWS)} });
      _llamaMode.render(document.getElementById('cards'));`);
    const cards = [...w.document.querySelectorAll('#llmCachedCards .mc-cached')];
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain('Downloaded, not yet served');
    expect(cards[0].querySelector('[data-act="cached-deploy"]')).not.toBeNull();
    expect(cards[1].querySelector('[data-act="cached-deploy"]')).toBeNull();
    expect(cards[1].textContent).toContain('Switch to multi-model mode to deploy');
    expect(cards[1].querySelector('[data-act="cached-hide"]')).not.toBeNull();
  });
  test('Hide removes the card for this browser and Show brings it back', () => {
    const w = run({}, `document.body.innerHTML = '<div id="llmModelCards"></div>';
      _llamaMode.setExtra({ mode: { mode: 'single', managed: true }, cached: ${JSON.stringify(ROWS)} });
      _llamaMode.render(document.getElementById('llmModelCards'));
      document.querySelector('[data-cached="0"] [data-act="cached-hide"]').click();`);
    const cac = () => w.document.querySelector('#llmCachedCards');
    expect(cac().querySelectorAll('.mc-cached')).toHaveLength(1);
    expect(cac().textContent).toContain('1 downloaded model hidden');
    expect(JSON.parse(w.localStorage.getItem('llmCachedHidden'))).toEqual(['a/b:Q4_K_M']);
    cac().querySelector('[data-act="cached-unhide"]').click();
    expect(cac().querySelectorAll('.mc-cached')).toHaveLength(2);
  });
  test('Deploy refuses a file that a config section already uses', async () => {
    const w = run({}, `document.body.innerHTML = '<div id="llmModelCards"></div>';
      _llmConfig = { 'add-to-config': { model: '/m/x.gguf' } };
      window.fetch = (u, o) => { __posts.push([u, null]); return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) }); };
      _llamaMode.setExtra({ mode: { mode: 'router', managed: true, presets_file: '/p.ini' }, cached: ${JSON.stringify(ROWS)} });
      _llamaMode.render(document.getElementById('llmModelCards'));
      document.querySelector('[data-act="cached-deploy"]').click();`);
    await flush();
    expect(w.__posts.map(p => p[0])).not.toContain('/api/llm/config');
    expect(w.__toasts[0]).toContain('add-to-config already uses this file');
    expect(w.__refreshed).toBe(true);
  });
  test('Deploy posts config, restarts, polls for the model, then refreshes', async () => {
    const w = run({}, `document.body.innerHTML = '<div id="llmModelCards"></div>';
      _llamaMode.opts.pollMs = 1; _llamaMode.opts.maxMs = 20;
      var __n = 0;
      window.fetch = (u, o) => { __posts.push([u, o && o.body ? JSON.parse(o.body) : null]);
        const body = u === '/api/llm/models' ? { data: ++__n > 1 ? [{ id: 'a/b:q4_k_m' }] : [] } : { ok: true };
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) }); };
      _llamaMode.setExtra({ mode: { mode: 'router', managed: true, presets_file: '/p.ini' }, cached: ${JSON.stringify(ROWS)} });
      _llamaMode.render(document.getElementById('llmModelCards'));
      document.querySelector('[data-act="cached-deploy"]').click();`);
    await flush();
    expect(w.document.querySelector('#llmCachedCards').textContent).toContain('Deploying…');
    await new Promise(r => setTimeout(r, 60));
    const urls = w.__posts.map(p => p[0]);
    expect(urls.slice(0, 2)).toEqual(['/api/llm/config', '/api/llm/server/restart']);
    expect(urls.filter(u => u === '/api/llm/models').length).toBe(2);
    expect(w.__posts[0][1]['a/b:Q4_K_M']).toEqual({ model: '/m/x.gguf', 'hf-file': 'x.gguf', 'ctx-size': '4096' });
    expect(w.__refreshed).toBe(true);
    expect(w.__toasts).toEqual([]);
  });
  test('Deploy omits hf-file for a row that is not from the hub cache', async () => {
    const rows = [{ ...ROWS[0], source: 'llama-cache' }];
    const w = run({}, `document.body.innerHTML = '<div id="llmModelCards"></div>';
      _llamaMode.opts.pollMs = 1; _llamaMode.opts.maxMs = 5;
      window.fetch = (u, o) => { __posts.push([u, o && o.body ? JSON.parse(o.body) : null]);
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(u === '/api/llm/models' ? { data: [] } : { ok: true }) }); };
      _llamaMode.setExtra({ mode: { mode: 'router', managed: true, presets_file: '/p.ini' }, cached: ${JSON.stringify(rows)} });
      _llamaMode.render(document.getElementById('llmModelCards'));
      document.querySelector('[data-act="cached-deploy"]').click();`);
    await new Promise(r => setTimeout(r, 40));
    expect(w.__posts[0][1]['a/b:Q4_K_M']).toEqual({ model: '/m/x.gguf', 'ctx-size': '4096' });
  });
  test('Deploy reports a timeout when the model never appears', async () => {
    const w = run({}, `document.body.innerHTML = '<div id="llmModelCards"></div>';
      _llamaMode.opts.pollMs = 1; _llamaMode.opts.maxMs = 5;
      window.fetch = (u) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(u === '/api/llm/models' ? { data: [] } : { ok: true }) });
      _llamaMode.setExtra({ mode: { mode: 'router', managed: true, presets_file: '/p.ini' }, cached: ${JSON.stringify(ROWS)} });
      _llamaMode.render(document.getElementById('llmModelCards'));
      document.querySelector('[data-act="cached-deploy"]').click();`);
    await new Promise(r => setTimeout(r, 60));
    expect(w.__toasts[0]).toContain('a/b:Q4_K_M did not appear within');
  });
  test('reasons for undeployable rows', () => {
    const w = run({});
    const html = (mode) => w.eval(`_llamaMode.cachedHtml(${JSON.stringify({ mode, cached: [ROWS[1]] })})`);
    expect(html({ mode: 'router', managed: false })).toContain('Not a system service');
    expect(html({ mode: 'router', managed: true, presets_file: '/p', presets_writable: false }))
      .toContain('Presets file is read-only for the agent');
  });
});

describe('helper update notice', () => {
  test('shows the root update command when the helper is too old for the moved unit', () => {
    const w = run({ mode: 'router', managed: true, presets_file: '/p.ini', unit: 'llama-server.service',
                    helper_update: true, root_update_cmd: 'sudo bash x --mode 5 -- --update' });
    expect(text(w)).toContain('one-time root update');
    expect(text(w)).toContain('sudo bash x --mode 5 -- --update');
    expect(w.document.querySelector('#c pre.mc-cmd').textContent).toBe('sudo bash x --mode 5 -- --update');
    expect(btns(w)).toEqual(['Show me how']);
    const a = w.document.querySelector('#c a.mcbtn-ghost');
    expect(a.getAttribute('href')).toBe(HELP.replace(/#.*$/, '') + '#updating-the-agents-helper');
    expect(a.getAttribute('target')).toBe('_blank');
  });
  test('an outdated helper answer opens a dialog with the command instead of a toast', async () => {
    const w = run({}, `
      _llamaMode.setExtra({ mode: { mode: 'single', managed: true }, cached: [] });
      window.fetch = () => Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve(
        { reason: 'wrapper-outdated', detail: 'Old helper. Run once on that computer: sudo bash y', root_update_cmd: 'sudo bash x' }) });
      document.body.innerHTML = '<div id="cards"></div>';
      _llamaMode.render(document.getElementById('cards'));`);
    w.document.querySelector('#llmModeBanner [data-act="mode-switch"]').click();
    await flush(); await flush();
    expect(w.__toasts).toEqual([]);
    expect(w.__alerts).toHaveLength(1);
    const a = w.__alerts[0];
    expect(a.title).toBe("Update the agent's helper first");
    expect(a.okLabel).toBe('Close');
    expect(a.bodyHtml).toContain('<pre class="mc-cmd">sudo bash x</pre>');
    expect(a.bodyHtml).toContain('updating-the-agents-helper');
  });
  test('an outdated helper answer without the command field takes it from the detail', async () => {
    const w = run({}, `
      _llamaMode.setExtra({ mode: { mode: 'single', managed: true }, cached: [] });
      window.fetch = () => Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve(
        { reason: 'wrapper-outdated', detail: 'Old helper. Run once on that computer: sudo bash <(curl -fsSL https://x/i.sh)' }) });
      document.body.innerHTML = '<div id="cards"></div>';
      _llamaMode.render(document.getElementById('cards'));`);
    w.document.querySelector('#llmModeBanner [data-act="mode-switch"]').click();
    await flush(); await flush();
    expect(w.__alerts[0].bodyHtml).toContain('<pre class="mc-cmd">sudo bash &lt;(curl -fsSL https://x/i.sh)</pre>');
  });
});


describe('_llamaCardIds', () => {
  const IDS = src.slice(src.indexOf('// ===== Llama card ids (#1419) begin'), src.indexOf('// ===== Llama card ids (#1419) end'));
  test('served models without a presets section get a card after the configured ones', () => {
    const w = runHarness({ sources: [IDS + `
      window.__ids = _llamaCardIds({ '*': {}, '__DEFAULTS__': {}, 'a/b:Q4': {} }, [{ id: 'c/d:Q8' }, { id: 'a/b:Q4' }, {}]);`],
      bodyHtml: '', bootstrap: '' });
    expect(w.__ids).toEqual(['a/b:Q4', 'c/d:Q8']);
  });
});

describe('_resolveDownloadQuants', () => {
  const Q = src.slice(src.indexOf('// ===== Download quant resolution (#1419) begin'), src.indexOf('// ===== Download quant resolution (#1419) end'));
  const QF = src.slice(src.indexOf('function extractQuantId('), src.indexOf('// ===== Download quant resolution (#1419) begin'));
  const rows = [{ repo: 'a/b', model_id: 'a/b:Q8_0' }, { repo: 'a/b', model_id: 'a/b:Q4_K_M' }, { repo: 'c/d', model_id: 'c/d:F16' }];
  const run = (filter) => runHarness({ sources: [QF + Q + `
      window.__q = _resolveDownloadQuants(${JSON.stringify(filter)}, 'a/b', ${JSON.stringify(rows)});`],
    bodyHtml: '', bootstrap: '' }).__q;
  test('wildcard and blank filters use the cached quants of the repo', () => {
    expect(run('*')).toEqual(['Q8_0', 'Q4_K_M']);
    expect(run('')).toEqual(['Q8_0', 'Q4_K_M']);
  });
  test('an explicit filter picks the matching cached quants, never its own text', () => {
    expect(run('q8_0')).toEqual(['Q8_0']);
    expect(run('Q8')).toEqual(['Q8_0']);
    expect(run('Q4')).toEqual(['Q4_K_M']);
    expect(run('Q5')).toEqual(['Q8_0', 'Q4_K_M']);
  });
  test('an explicit filter is used only when nothing of the repo is cached', () => {
    const w = runHarness({ sources: [QF + Q + `window.__q = _resolveDownloadQuants('Q8', 'x/y', ${JSON.stringify(rows)});`], bodyHtml: '', bootstrap: '' });
    expect(w.__q).toEqual(['Q8']);
  });
  test('nothing cached falls back to the bare repo', () => {
    const w = runHarness({ sources: [QF + Q + `window.__q = _resolveDownloadQuants('*', 'x/y', ${JSON.stringify(rows)});`], bodyHtml: '', bootstrap: '' });
    expect(w.__q).toEqual(['']);
  });
});

describe('_afterDownload', () => {
  const Q = src.slice(src.indexOf('// ===== Download quant resolution (#1419) begin'), src.indexOf('// ===== Download quant resolution (#1419) end'));
  const QF = src.slice(src.indexOf('function extractQuantId('), src.indexOf('// ===== Download quant resolution (#1419) begin'));
  const AD = src.slice(src.indexOf('async function _afterDownload('), src.indexOf('async function _renderAddDownloadedButtons('));
  const ROWS = [{ repo: 'a/b', file: 'x.gguf', path: '/m/x.gguf', model_id: 'a/b:Q8_0', deployable: true, source: 'hf-hub' }];
  const go = (choice, deployable = true) => {
    const rows = ROWS.map(r => ({ ...r, deployable }));
    return runHarness({
      sources: [`
        function _esc(s) { return String(s); } function adminEsc(s) { return String(s); }
        function shortName(s) { return String(s).split('/').pop(); }
        var _llmModels = [], _llmConfig = {}, __toasts = [], __posts = [], __asked = [], __added = [];
        var _dlLastRepo = 'a/b', _dlLastQuant = '*';
        function _toastErr(m) { __toasts.push(m); }
        function _themedConfirm(o) { __asked.push(o); return Promise.resolve(${JSON.stringify(choice)}); }
        async function refreshLLMTab() { _llamaMode.setExtra({ mode: { mode: 'router', managed: true }, cached: ${JSON.stringify(rows)} }); }
        function addDownloadedModel(q) { __added.push(q); }
        window.fetch = (u, o) => { __posts.push(u); return Promise.resolve({ ok: true, status: 200,
          json: () => Promise.resolve(u === '/api/llm/cache/gguf' ? { data: ${JSON.stringify(rows)} } : { data: [{ id: 'a/b:Q8_0' }] }) }); };
        ` + BLOCK + QF + Q + AD],
      bodyHtml: '<div id="llmModelCards"></div>', bootstrap: '_llamaMode.opts.pollMs = 1; _llamaMode.opts.maxMs = 5; _afterDownload();',
    });
  };
  test('offers Deploy and Custom config for one new deployable file; Deploy skips the second confirm', async () => {
    const w = go(true);
    await new Promise(r => setTimeout(r, 40));
    expect(w.__asked).toHaveLength(1);
    expect(w.__asked[0].confirmLabel).toBe('Deploy');
    expect(w.__asked[0].altLabel).toBe('Custom config');
    expect(w.__posts).toContain('/api/llm/config');
    expect(w.__added).toEqual([]);
  });
  test('Custom config opens the editor with the real quant', async () => {
    const w = go('alt');
    await flush(); await flush(); await flush();
    expect(w.__added).toEqual(['Q8_0']);
    expect(w.__posts).not.toContain('/api/llm/config');
  });
  test('Not now does nothing', async () => {
    const w = go(false);
    await flush(); await flush(); await flush();
    expect(w.__added).toEqual([]);
    expect(w.__posts).not.toContain('/api/llm/config');
  });
  test('an undeployable file only offers the custom config', async () => {
    const w = go(true, false);
    await flush(); await flush(); await flush();
    expect(w.__asked[0].confirmLabel).toBe('Custom config');
    expect(w.__asked[0].altLabel).toBe('');
    expect(w.__added).toEqual(['Q8_0']);
  });
});
