// #1009: Admin › Gateway "Model Profile Maintenance" card — leftover and unverified rows, empty
// state, the hosts note, and the selection each Delete sends.
import { describe, it, expect } from 'vitest';
import { srcFile, blockSrc, runHarness } from './helpers/harness.js';

const adminSrc = srcFile('js/admin.js');
const agentsSrc = srcFile('js/admin-agents.js');
const indexSrc = srcFile('index.html');
const cardHtml = blockSrc(indexSrc, '<div class="card" id="adminStoresCard">', '<!-- Backups sub-tab', { includeEnd: false });

const FIXTURE = {
  ok: true, at: 1000, reason: 'first read', pruned_smoke: 2, aliases_checked: true, unverified: [],
  removed_agents: [{ agent: 'deadbeef-1234-5678', models: ['m1', 'old'], profiles: 2 }],
  absent_models: [{ agent: 'a1', host: 'box-1', model: 'gone', profiles: 2 }],
  absent_aliases: [{ model: 'zzz', alias: 'Gone <b>' }],
  counts: { removed_agents: 1, absent_models: 1, absent_aliases: 1 }, total: 3,
};

function harness(bootstrap) {
  return runHarness({
    sources: [adminSrc, agentsSrc],
    bootstrap: 'if (typeof Sortable === "undefined") { Sortable = { create: () => ({ destroy(){} }) }; }\n' + bootstrap,
    bodyHtml: `<div id="adminTab">${cardHtml}</div>`,
  });
}

describe('model profile maintenance card (#1009)', () => {
  it('index.html places the card in the Gateway sub-tab after Model pins', () => {
    const routing = blockSrc(indexSrc, '<div id="admin-routing"', '<!-- Backups sub-tab', { includeEnd: false });
    expect(routing.indexOf('id="adminStoresCard"')).toBeGreaterThan(routing.indexOf('id="adminPinsCard"'));
    expect(routing).toContain('id="adminStoresCheck"');
    expect(routing).toContain('id="adminStoresCleanAll"');
  });

  it('renders one row per leftover with a plain-English reason and escapes text', () => {
    const w = harness(`window.__T = { render: adminRenderStores, rows: adminStoreRows };`);
    w.__T.render(FIXTURE);
    const trs = [...w.document.querySelectorAll('#adminStoresTbody tr')];
    expect(trs.length).toBe(3);
    expect(trs[0].textContent).toContain('Profiles for 2 models');
    expect(trs[0].textContent).toContain('agent deadbeef…');
    expect(trs[0].textContent).toContain('Agent no longer registered');
    expect(trs[1].textContent).toContain('gone');
    expect(trs[1].textContent).toContain('box-1');
    expect(trs[1].textContent).toContain('Model not on this host');
    expect(trs[2].textContent).toContain('zzz');
    expect(trs[2].textContent).toContain('named “Gone <b>”');
    expect(trs[2].querySelector('b')).toBeNull();
    expect(trs[2].textContent).toContain('No host has this model');
    expect(w.document.getElementById('adminStoresCleanAll').hidden).toBe(false);
    expect(w.document.getElementById('adminStoresNote').textContent).toContain('checked');
    const sels = w.__T.rows(FIXTURE).map(r => r.sel);
    expect(sels).toEqual([
      { agents: ['deadbeef-1234-5678'] },
      { models: [{ agent: 'a1', model: 'gone' }] },
      { aliases: ['zzz'] },
    ]);
  });

  it('shows the empty state and hides Delete all when nothing is left over', () => {
    const w = harness(`window.__T = { render: adminRenderStores };`);
    w.__T.render({ ok: true, at: 1000, removed_agents: [], absent_models: [], absent_aliases: [], aliases_checked: true, unverified: [] });
    expect(w.document.querySelector('#adminStoresTbody .empty').textContent).toContain('Nothing to review');
    expect(w.document.getElementById('adminStoresCleanAll').hidden).toBe(true);
  });

  it('lists unverified entries as rows the admin may delete and names the hosts in the note', () => {
    const w = harness(`window.__T = { render: adminRenderStores, rows: adminStoreRows };`);
    const d = { ok: true, at: 1000, removed_agents: [], absent_models: [], absent_aliases: [], aliases_checked: false,
      unverified: ['box-2', 'mac-mini'],
      unverified_models: [{ agent: 'a2', host: 'box-2', model: 'm2', profiles: 1 }],
      unverified_aliases: [{ model: 'zzz', alias: 'Gone' }] };
    w.__T.render(d);
    const trs = [...w.document.querySelectorAll('#adminStoresTbody tr')];
    expect(trs.length).toBe(2);
    expect(trs[0].textContent).toContain('m2');
    expect(trs[0].textContent).toContain('Unverified');
    expect(trs[0].querySelector('button').textContent).toBe('Delete');
    expect(trs[1].textContent).toContain('named “Gone”');
    expect(w.__T.rows(d).map(r => r.sel)).toEqual([{ models: [{ agent: 'a2', model: 'm2' }] }, { aliases: ['zzz'] }]);
    expect(w.document.getElementById('adminStoresCleanAll').hidden).toBe(false);
    const note = w.document.getElementById('adminStoresNote').textContent;
    expect(note).toContain('not checked: box-2, mac-mini');
    expect(note).toContain('display names not checked');
  });

  it('index.html titles the card Model Profile Maintenance', () => {
    expect(cardHtml).toContain('<h3>Model Profile Maintenance</h3>');
  });

  it('rows carry a Delete button and the footer a Delete all button', () => {
    const w = harness(`window.__T = { render: adminRenderStores };`);
    w.__T.render(FIXTURE);
    const btns = [...w.document.querySelectorAll('#adminStoresTbody button')];
    expect(btns.length).toBe(3);
    expect(btns.every(b => b.textContent === 'Delete' && b.classList.contains('mcbtn'))).toBe(true);
    expect(w.document.getElementById('adminStoresCleanAll').textContent).toBe('Delete all');
  });

  it('Delete all posts {all:true} and a row Delete posts that row\'s selection', async () => {
    const w = harness(`
      window.__calls = [];
      window.fetch = (url, opts) => { window.__calls.push([url, opts && JSON.parse(opts.body)]);
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, removed: { agents: 0, models: 1, aliases: 0 },
          removed_agents: [], absent_models: [], absent_aliases: [], aliases_checked: true, unverified: [], at: 1000 }) }); };
      window.__T = { render: adminRenderStores, clean: adminCleanStores };`);
    w.__T.render(FIXTURE);
    await w.__T.clean(1);
    await w.__T.clean();
    expect(w.__calls[0]).toEqual(['/api/admin/stores/clean', { models: [{ agent: 'a1', model: 'gone' }] }]);
    expect(w.__calls[1]).toEqual(['/api/admin/stores/clean', { all: true }]);
    expect(w.document.getElementById('adminStoresResult').textContent).toBe('removed 1 entry');
    expect(w.document.querySelector('#adminStoresTbody .empty')).toBeTruthy();
  });
});
