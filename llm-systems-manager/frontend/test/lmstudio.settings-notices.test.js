// #1422: LM Studio settings notices under the server buttons, and their Save button.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { srcFile, runHarness } from './helpers/harness.js';

const src = srcFile('js/lmstudio.js');
const foundation = srcFile('js/foundation.js');
const PICKER = foundation.slice(foundation.indexOf('// ── Multi-agent picker state'), foundation.indexOf('const MAX_POINTS'));
const BEGIN = '// ===== LM Studio settings notices (#1422) begin';
const END = '// ===== LM Studio settings notices (#1422) end';
const BLOCK = src.slice(src.indexOf(BEGIN), src.indexOf(END));
const NOTE = "LM Studio is not a system service. Start and Stop run the lms command as the agent's user.";

const base = { settings_source: 'saved', api_url: 'http://127.0.0.1:1234', lms_cmd: '/home/ana/.lmstudio/bin/lms',
               cli_found: true, running: true, process_user: 'llmsys', agent_user: 'llmsys' };

function run(d, extraBootstrap = '') {
  return runHarness({
    sources: [`
      function _esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
      var __toasts = [], __posts = [], __confirm = true, __asked = [], __refreshed = 0;
      var __resp = { ok: true, status: 200, json: () => Promise.resolve({ ok: true, settings_source: 'saved' }) };
      function _toastErr(m) { __toasts.push(m); }
      function _themedConfirm(o) { __asked.push(o); return Promise.resolve(__confirm); }
      function fetchLMStudioMetrics() { __refreshed++; }
      window.fetch = (u, o) => { __posts.push([u, o && o.method]); return Promise.resolve(__resp); };
      ` + BLOCK],
    bodyHtml: '<div id="c"></div>',
    bootstrap: `renderLmsSettingsNotices(${JSON.stringify(d)}, document.getElementById('c'));` + extraBootstrap,
  });
}
const texts = (s, server = {}) => run({}).eval(`lmsSettingsNotices(${JSON.stringify(s)}, ${JSON.stringify(server)})`)
  .map(n => n.text.replace(/<[^>]+>/g, ''));
const btns = (w) => [...w.document.querySelectorAll('#c button')].map(b => b.textContent);
const text = (w) => w.document.getElementById('c').textContent;
const flush = () => new Promise(r => setTimeout(r, 0));
const online = (settings, server = { on: true }) => ({ agent_online: true, settings, server });

describe('lmsSettingsNotices', () => {
  test('detected shows Save with url and lms path', () => {
    const w = run(online({ ...base, settings_source: 'detected', api_url: 'http://127.0.0.1:1240' }));
    expect(text(w)).toContain('Found LM Studio on this computer (API http://127.0.0.1:1240, lms at /home/ana/.lmstudio/bin/lms). Save these settings so they are kept after a restart.');
    expect(btns(w)).toEqual(['Save']);
  });
  test('stopped shows start hint with user', () => {
    const t = texts({ ...base, running: false, process_user: '', agent_user: 'ana' }, { on: false });
    expect(t).toContain('LM Studio is installed but its server is not running. Press Start, or run lms server start as ana.');
  });
  test('other user shows mismatch', () => {
    const t = texts({ ...base, process_user: 'ana', agent_user: 'llmsys' });
    expect(t).toContain('LM Studio runs as ana; the agent runs as llmsys, so the lms command cannot see its models. Run both as the same user.');
  });
  test('not found shows LMS_CMD hint', () => {
    const t = texts({ ...base, cli_found: false, running: false, lms_cmd: '', process_user: '' }, { on: false });
    expect(t).toContain('The agent cannot find the lms command. Set its location as LMS_CMD in the agent settings.');
    expect(t.some(x => x.includes('is installed but'))).toBe(false);
  });
  test('not-a-service note always present', () => {
    for (const s of [base, { ...base, settings_source: 'detected' }, { ...base, running: false }, {}, null]) {
      expect(texts(s)).toContain(NOTE);
    }
    const w = run(online({ ...base, settings_source: 'detected' }));
    expect(w.document.querySelector('#c .mc-mode-hint').textContent).toBe(NOTE);
  });
  test('saved and running shows only the note', () => {
    expect(texts(base)).toEqual([NOTE]);
    const w = run(online(base));
    expect(w.document.querySelector('#c .mc-mode-banner')).toBeNull();
    expect(text(w)).toBe(NOTE);
  });
  test('a running server reported by the server block is not stopped', () => {
    expect(texts({ ...base, running: false }, { on: true })).toEqual([NOTE]);
  });
  test('an agent without the settings block gets only the note', () => {
    expect(texts(undefined, { on: false })).toEqual([NOTE]);
  });
  test('values are HTML-escaped', () => {
    const w = run(online({ ...base, settings_source: 'detected', lms_cmd: '/x/<b>lms', process_user: '<i>a', agent_user: 'llmsys' }));
    expect(w.document.querySelector('#c b')).toBeNull();
    expect(w.document.querySelector('#c i')).toBeNull();
    expect(text(w)).toContain('lms at /x/<b>lms)');
    expect(text(w)).toContain('LM Studio runs as <i>a;');
  });
});

describe('renderLmsSettingsNotices', () => {
  test('an offline agent clears the container', () => {
    const w = run(online({ ...base, settings_source: 'detected' }),
      `renderLmsSettingsNotices({ agent_online: false, settings: {} }, document.getElementById('c'));`);
    expect(w.document.getElementById('c').innerHTML).toBe('');
    expect(w.document.getElementById('c').style.display).toBe('none');
  });
  test('Save asks first, posts, then refreshes the view', async () => {
    const w = run(online({ ...base, settings_source: 'detected' }));
    w.document.querySelector('#c [data-act="lms-settings-save"]').click();
    await flush(); await flush();
    expect(w.__asked).toHaveLength(1);
    expect(w.__asked[0].confirmLabel).toBe('Save');
    expect(w.__posts).toEqual([['/api/lmstudio/settings/save', 'POST']]);
    expect(w.__refreshed).toBe(1);
    expect(w.__toasts).toEqual([]);
  });
  test('Cancel posts nothing', async () => {
    const w = run(online({ ...base, settings_source: 'detected' }), '__confirm = false;');
    w.document.querySelector('#c [data-act="lms-settings-save"]').click();
    await flush(); await flush();
    expect(w.__posts).toEqual([]);
  });
  test('a refused save shows the agent detail as a toast', async () => {
    const w = run(online({ ...base, settings_source: 'detected' }), `
      __resp = { ok: false, status: 409, json: () => Promise.resolve({ reason: 'not-detected', detail: 'There are no detected settings to save.' }) };`);
    w.document.querySelector('#c [data-act="lms-settings-save"]').click();
    await flush(); await flush(); await flush();
    expect(w.__toasts).toEqual(['There are no detected settings to save.']);
    expect(w.__refreshed).toBe(0);
  });
});

describe('placement and agent routing', () => {
  const doc = new JSDOM(srcFile('index.html')).window.document;
  test('the notice container sits above Models, outside the collapsible sections', () => {
    const ban = doc.getElementById('lmsSettingsBanner');
    expect(ban).not.toBeNull();
    expect(ban.closest('.llm-section')).toBeNull();
    expect(ban.parentElement.id).toBe('llm-lmstudio');
    expect(ban.compareDocumentPosition(doc.getElementById('lmsSecModels')) & 4).toBe(4);
  });
  test('the not-a-service note sits under the server buttons', () => {
    const note = doc.getElementById('lmsServiceNote');
    expect(note.closest('#lmsSecCtrl')).not.toBeNull();
    expect(note.previousElementSibling.querySelector('#lmsBtnStart')).not.toBeNull();
  });
  test('notices and note render into their own containers', () => {
    const w = run({}, `document.body.innerHTML = '<div id="b"></div><div id="n"></div>';
      renderLmsSettingsNotices(${JSON.stringify(online({ ...base, settings_source: 'detected' }))},
        document.getElementById('b'), document.getElementById('n'));`);
    expect(w.document.querySelector('#b [data-act="lms-settings-save"]')).not.toBeNull();
    expect(w.document.querySelector('#b .mc-mode-hint')).toBeNull();
    expect(w.document.getElementById('n').textContent).toBe(NOTE);
    w.eval(`renderLmsSettingsNotices(${JSON.stringify(online(base))}, document.getElementById('b'), document.getElementById('n'))`);
    expect(w.document.getElementById('b').style.display).toBe('none');
    expect(w.document.getElementById('n').textContent).toBe(NOTE);
  });
  test('Save goes to the agent picked in the LM Studio view', async () => {
    const go = async (sel) => {
      const w = runHarness({
        sources: [`
          function _esc(s) { return String(s); }
          var __urls = [];
          window.fetch = (u) => { __urls.push(u); return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) }); };
          function _toastErr() {} function _themedConfirm() { return Promise.resolve(true); } function fetchLMStudioMetrics() {}
          ` + PICKER + BLOCK],
        bodyHtml: '<div id="c"></div>',
        bootstrap: `window._selectedAgents.lms = ${JSON.stringify(sel)};
          renderLmsSettingsNotices(${JSON.stringify(online({ ...base, settings_source: 'detected' }))}, document.getElementById('c'));`,
      });
      w.document.querySelector('#c [data-act="lms-settings-save"]').click();
      await flush(); await flush();
      return w.__urls;
    };
    expect(await go('agent-2')).toEqual(['/api/lmstudio/settings/save?agent=agent-2']);
    expect(await go(null)).toEqual(['/api/lmstudio/settings/save']);
  });
});
