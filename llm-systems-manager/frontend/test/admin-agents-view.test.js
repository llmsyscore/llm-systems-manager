// #793: Admin → Agents roster — pure helpers (state dot, TLS glyph, description
// rule, filter grammar, summary, stamp health) and the header/roster render.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const adminSrc = readFileSync(join(here, '..', 'js', 'admin.js'), 'utf8');
const agentsSrc = readFileSync(join(here, '..', 'js', 'admin-agents.js'), 'utf8');
const indexSrc = readFileSync(join(here, '..', 'index.html'), 'utf8');

// The real panel markup, lifted from index.html so ids stay in lockstep.
const STAMP = indexSrc.slice(indexSrc.indexOf('<button type="button" class="hc-stamp"'), indexSrc.indexOf('</button>', indexSrc.indexOf('id="adminRefreshStamp"')) + 9);
const PANEL = STAMP + indexSrc.slice(indexSrc.indexOf('<div id="admin-agents"'), indexSrc.indexOf('<!-- Routing sub-tab'));

function harness(bootstrap, body = PANEL) {
  const dom = new JSDOM(`<!doctype html><html><head></head><body>${body}</body></html>`,
    { runScripts: 'dangerously', url: 'http://localhost/' });
  const inject = (code) => {
    const s = dom.window.document.createElement('script');
    s.textContent = code;
    dom.window.document.head.appendChild(s);
  };
  inject(adminSrc);
  inject(agentsSrc);
  if (bootstrap) inject(bootstrap);
  return dom.window;
}
const V = harness('').AgentsView;

const NOW = Date.parse('2026-09-02T14:00:00Z');
const iso = (secAgo) => new Date(NOW - secAgo * 1000).toISOString();
const approved = (id, extra = {}) => ({
  agent_id: id, hostname: id, status: 'approved', liveness: 'live', os: 'linux', role: 'llama host',
  version: 'v2', bind_url: `https://192.0.2.${id.length}:8098`, registered_from: `192.0.2.${id.length}`,
  capabilities: { llama: true, sysperf: true }, last_heartbeat: iso(40),
  last_heartbeat_data: { collection_enabled: true, control_channel_tls: true }, ...extra,
});

describe('#793 row state dot', () => {
  test('pending / disabled come from status', () => {
    expect(V.rowState({ status: 'pending', liveness: 'pending' })).toBe('pending');
    expect(V.rowState({ status: 'disabled' })).toBe('disabled');
  });
  test('approved: liveness first, then paused collection', () => {
    expect(V.rowState(approved('a'))).toBe('live');
    expect(V.rowState(approved('a', { liveness: 'stale' }))).toBe('stale');
    expect(V.rowState(approved('a', { liveness: 'down', last_heartbeat_data: { collection_enabled: false } }))).toBe('down');
    expect(V.rowState(approved('a', { last_heartbeat_data: { collection_enabled: false } }))).toBe('paused');
  });
});

describe('#793 TLS glyph chip', () => {
  test('mutual / one-way / http', () => {
    expect(V.tlsInfo(approved('a')).glyph).toBe('⇄');
    expect(V.tlsInfo(approved('a', { last_heartbeat_data: { control_channel_tls: false } })).mode).toBe('in');
    expect(V.tlsInfo(approved('a', { bind_url: 'http://192.0.2.1:8098' })).mode).toBe('out');
    const http = V.tlsInfo(approved('a', { bind_url: 'http://192.0.2.1:8098', last_heartbeat_data: {} }));
    expect(http.glyph).toBe('○');
    expect(http.label).toBe('http');
  });
  test('cert issued but not bound yet is still http, with the date in the tooltip', () => {
    const t = V.tlsInfo(approved('a', { bind_url: 'http://x:1', last_heartbeat_data: {}, last_cert_issued_at: '2026-08-30T10:00:00Z' }));
    expect(t.mode).toBe('http');
    expect(t.title).toContain('2026-08-30');
  });
  test('pending agent explains the cert is issued on approval', () => {
    expect(V.tlsInfo({ status: 'pending', bind_url: 'http://x:1' }).title).toMatch(/issued on approval/);
  });
});

describe('#793 description line only when it adds information', () => {
  test('hidden when equal to the hostname or the installer default', () => {
    expect(V.showDesc({ hostname: 'box', description: 'box' })).toBe(false);
    expect(V.showDesc({ hostname: 'box', os: 'linux', description: 'box (linux)' })).toBe(false);
    expect(V.showDesc({ hostname: 'box', description: '  ' })).toBe(false);
  });
  test('shown otherwise', () => {
    expect(V.showDesc({ hostname: 'box', os: 'linux', description: 'Primary inference box' })).toBe(true);
  });
});

describe('#793 last-seen + fingerprint formatting', () => {
  test('fmtAgo uses spaced mono units', () => {
    expect(V.fmtAgo(iso(44), NOW)).toBe('44 s ago');
    expect(V.fmtAgo(iso(160), NOW)).toBe('2 m 40 s ago');
    expect(V.fmtAgo(iso(180), NOW)).toBe('3 m ago');
    expect(V.fmtAgo(iso(3900), NOW)).toBe('1 h 5 m ago');
    expect(V.fmtAgo(iso(3 * 86400), NOW)).toBe('3 d ago');
    expect(V.fmtAgo(null, NOW)).toBe('—');
    expect(V.fmtAgo('garbage', NOW)).toBe('—');
  });
  test('fingerprint is shortened to head/tail groups', () => {
    expect(V.fingerprintShort('3f9a41c0' + 'ab'.repeat(24) + 'c21e')).toBe('sha256:3f9a 41c0 … c21e');
    expect(V.fingerprintShort('')).toBe('—');
  });
});

describe('#793 filter grammar', () => {
  const up = approved('needs-up', { update_available: true, version: 'v1' });
  const pend = { agent_id: 'p', hostname: 'newbox', status: 'pending', capabilities: { openclaw: true } };
  const lms = approved('lmsbox', { capabilities: { lms: true } });
  test('free text matches name, IP, capability', () => {
    expect(V.matches(lms, V.parseFilter('LMS'))).toBe(true);
    expect(V.matches(lms, V.parseFilter('192.0.2.6'))).toBe(true);
    expect(V.matches(lms, V.parseFilter('needs'))).toBe(false);
  });
  test('needs:update, state:pending, cap:lms', () => {
    expect(V.matches(up, V.parseFilter('needs:update'))).toBe(true);
    expect(V.matches(lms, V.parseFilter('needs:update'))).toBe(false);
    expect(V.matches(pend, V.parseFilter('state:pending'))).toBe(true);
    expect(V.matches(up, V.parseFilter('state:pending'))).toBe(false);
    expect(V.matches(lms, V.parseFilter('cap:lms'))).toBe(true);
    expect(V.matches(up, V.parseFilter('cap:lms'))).toBe(false);
  });
  test('tokens combine with AND', () => {
    expect(V.matches(up, V.parseFilter('needs:update cap:llama needs'))).toBe(true);
    expect(V.matches(up, V.parseFilter('needs:update cap:lms'))).toBe(false);
  });
});

describe('#793 header summary + stamp health', () => {
  test('summary counts', () => {
    const s = V.summary([approved('a'), approved('b', { liveness: 'stale', update_available: true }),
      { status: 'pending' }, { status: 'disabled' }]);
    expect(s).toEqual({ total: 4, live: 1, pending: 1, needsUpdate: 1 });
  });
  test('stamp: green fresh, amber failed/stale, red unreachable', () => {
    expect(V.stampState({ lastOkAt: NOW - 5000 }, NOW)).toBe('ok');
    expect(V.stampState({ lastOkAt: NOW - 31000 }, NOW)).toBe('warn');
    expect(V.stampState({ lastOkAt: NOW - 5000, failed: true }, NOW)).toBe('warn');
    expect(V.stampState({ lastOkAt: NOW - 5000, failed: true, unreachable: true }, NOW)).toBe('crit');
    expect(V.stampState({ lastOkAt: 0 }, NOW)).toBe('warn');
  });
});

describe('#793 row + drawer markup', () => {
  const boot = (agents, globals = {}, extra = '') => `
    _adminProviders = [
      { name:'llama', label:'llama.cpp', capability_key:'llama', sub_tab:'llamacpp' },
      { name:'lms',   label:'LM Studio', capability_key:'lms',   sub_tab:'lmstudio' },
    ];
    _adminPoolProviders = [{ name:'llama', label:'llama.cpp', pin_key:'llama_model_pins' }];
    _adminGlobal = ${JSON.stringify(globals)};
    _adminAgentsCache = ${JSON.stringify(agents)};
    _latestAgentVersion = 'v2';
    _adminManagerVersion = 'v9';
    _adminCollectInterval = 5;
    Date.now = () => ${NOW};
    ${extra}
    AgentsView.stamp({ ok: true });
    AgentsView.render();
  `;
  test('one dot, no pill, for a live agent; pill only for exceptions', () => {
    const win = harness(boot([approved('live1'), approved('paused1', { last_heartbeat_data: { collection_enabled: false } }),
      { agent_id: 'p1', hostname: 'p1', status: 'pending', capabilities: {} }]));
    const rows = win.document.querySelectorAll('#agRoster .ag-rw:not(.hd)');
    expect(rows.length).toBe(3);
    const byId = id => win.document.querySelector(`[data-row="${id}"]`);
    expect(byId('live1').querySelector('.ag-dot').className).toBe('ag-dot live');
    expect(byId('live1').querySelector('.ag-pill')).toBeNull();
    expect(byId('paused1').querySelector('.ag-dot').className).toBe('ag-dot paused');
    expect(byId('paused1').querySelector('.ag-pill').textContent).toBe('paused');
    expect(byId('paused1').querySelector('[data-act="resume"]')).toBeTruthy();
    expect(byId('p1').querySelector('.ag-pill').textContent).toBe('pending approval');
    expect(byId('p1').querySelector('[data-act="approve"]').textContent).toBe('Approve');
    expect(byId('p1').querySelector('[data-act="pause"]')).toBeNull();
  });
  test('endpoint column carries IP + version; meta line is os · role; update shows as an amber ↑', () => {
    const win = harness(boot([approved('old', { version: 'v1', update_available: true, agent_user: 'llmsys' })]));
    const row = win.document.querySelector('[data-row="old"]');
    expect(row.querySelector('.sub .m').textContent).toBe('linux·llama host');
    expect(row.querySelector('.ag-ep .ip').textContent).toBe('192.0.2.3');
    expect(row.querySelector('.ag-ep .ver').textContent).toContain('v1');
    expect(row.querySelector('.ag-ep .up').getAttribute('title')).toContain('v2');
    expect(row.querySelector('.ag-seen')).toBeNull();   // live: no countdown
    const menu = win.document.querySelector('[data-row="old"] .mc-menu');
    expect(menu.querySelector('[data-act="update"]').textContent).toContain('Update to v2');
    expect(menu.textContent).not.toContain('Re-deploy');
  });
  test('capability chip reads llama ★ · pool #2 for primary + pool slot', () => {
    const win = harness(boot([approved('a1'), approved('a2')],
      { primary_llama_id: 'a2', llama_pool: ['a1', 'a2'] }));
    const chip = win.document.querySelector('[data-row="a2"] .ag-cap.prov');
    expect(chip.textContent).toBe('llama★·pool #2');
    expect(win.document.querySelector('[data-row="a1"] .ag-cap.prov .star')).toBeNull();
    expect(win.document.querySelector('[data-row="a1"] .ag-cap.q').textContent).toBe('sysperf');
    expect(win.document.querySelector('[data-row="a1"] .ag-cap.tls').textContent).toBe('⇄ tls');
  });
  test('last seen appears only when it matters: stale/down rows and pending agents', () => {
    const win = harness(boot([approved('st', { liveness: 'stale', last_heartbeat: iso(160) }),
      approved('dn', { liveness: 'down', last_heartbeat: iso(900) }),
      { agent_id: 'p', hostname: 'p', status: 'pending', capabilities: {}, last_heartbeat: iso(180) }]));
    const d = win.document;
    expect(d.querySelector('[data-row="st"] .ag-seen').className).toBe('ag-seen old');
    expect(d.querySelector('[data-row="st"] .ag-seen').textContent).toMatch(/^stale · \d+ m/);
    expect(d.querySelector('[data-row="dn"] .ag-seen').className).toBe('ag-seen gone');
    expect(d.querySelector('[data-row="p"] .ag-seen').textContent).toMatch(/^seen \d+ m/);
  });
  test('co-located services collapse into one chip; the drawer lists them all', () => {
    const infra = [{ role: 'manager', version: 'v9' }, { role: 'alarm_engine', version: 'v8' }, { role: 'influxdb', version: '2.7' }];
    const win = harness(boot([approved('h', { colocated_infra: infra })]));
    const d = win.document;
    const chips = d.querySelectorAll('[data-row="h"] .ag-cap.infra');
    expect(chips.length).toBe(1);
    expect(chips[0].textContent).toBe('⛬ manager +2');
    expect(chips[0].getAttribute('title')).toContain('alarm engine v8');
    d.querySelector('[data-row="h"] .ag-who').click();
    const kv = d.querySelectorAll('[data-row="h"] .ag-drawer .ag-kv')[1].textContent;
    expect(kv).toContain('managerv9');
    expect(kv).toContain('alarm enginev8');
    expect(kv).toContain('influxdb2.7');
  });
  test('header summary, version chips and stamp', () => {
    const win = harness(boot([approved('a1'), approved('a2', { update_available: true }), { status: 'pending', agent_id: 'p', hostname: 'p' }]));
    const d = win.document;
    expect(d.getElementById('agSummary').textContent).toBe('3 registered2 live1 pending1 needs update');
    expect(d.getElementById('agMgrVer').textContent).toBe('manager v9');
    expect(d.getElementById('agAgentVer').textContent).toBe('agent v2');
    expect(d.getElementById('adminRefreshRf').className).toBe('hc-rf ok');
    expect(d.getElementById('adminRefreshTime').textContent).toMatch(/^updated \d/);
    expect(d.getElementById('agUpdateCnt').hidden).toBe(false);
    expect(d.getElementById('agUpdateCnt').textContent).toBe('1 pending');
    expect(d.getElementById('agApproveAll').hidden).toBe(true);
    expect(d.getElementById('agFoot')).toBeNull();
  });
  test('agent security slider mirrors global.auth_disabled and tints the row when off', () => {
    const win = harness(boot([approved('a1')], { auth_disabled: true }));
    const d = win.document;
    expect(d.getElementById('agAuthTg').classList.contains('on')).toBe(false);
    expect(d.getElementById('agAuthRow').classList.contains('warn')).toBe(true);
    expect(d.getElementById('agAuthSub').textContent).toBe('Secure agent authentication is off');
  });
  test('failed refresh turns the stamp amber, unreachable red', () => {
    const win = harness(boot([approved('a1')]));
    win.AgentsView.stamp({ ok: false });
    expect(win.document.getElementById('adminRefreshRf').className).toBe('hc-rf warn');
    win.AgentsView.stamp({ ok: false, unreachable: true });
    expect(win.document.getElementById('adminRefreshRf').className).toBe('hc-rf crit');
  });
  test('clicking the name opens the drawer with roles, connection and shortcuts', () => {
    const win = harness(boot([approved('a1', { fingerprint: 'ab'.repeat(32), agent_user: 'llmsys', last_cert_issued_at: '2026-08-30T00:00:00Z' })],
      { llama_pool: ['a1'] }));
    const d = win.document;
    d.querySelector('[data-row="a1"] .ag-who').click();
    const dr = d.querySelector('[data-row="a1"] .ag-drawer');
    expect(dr).toBeTruthy();
    expect(d.querySelector('[data-row="a1"]').classList.contains('open')).toBe(true);
    expect(dr.querySelector('[data-act="primary"][data-prov="llama"]')).toBeTruthy();
    expect(dr.querySelector('[data-act="pool"][data-prov="llama"] .hint').textContent).toBe('slot #1');
    const kv = dr.querySelector('.ag-kv').textContent;
    expect(kv).toContain('last seen40 s ago');
    expect(kv).toContain('mutual · cert issued 2026-08-30');
    expect(kv).toContain('llmsys');
    expect(kv).toContain('every 5 s · llama host');
    expect(kv).toContain('sha256:abab abab … abab');
    expect(dr.querySelector('[data-act="open"][data-prov="llama"]').textContent).toContain('Open llama.cpp control');
    // The drawer survives a re-render (auto-refresh).
    win.AgentsView.render();
    expect(d.querySelector('[data-row="a1"] .ag-drawer')).toBeTruthy();
  });
  test('drawer sliders and row buttons dispatch to the admin.js actions', () => {
    const win = harness(boot([approved('a1'), { agent_id: 'p1', hostname: 'p1', status: 'pending', capabilities: {} }], {},
      `window.__calls = [];
       for (const fn of ['adminTogglePrimary','adminTogglePool','adminToggleHostAgent','adminToggleCollection','adminApprove','adminToggleAuth','adminRefreshNow'])
         window[fn] = (...a) => window.__calls.push([fn, ...a]);`));
    const d = win.document;
    d.querySelector('[data-row="a1"] .ag-who').click();
    d.querySelector('[data-row="a1"] [data-act="primary"][data-prov="llama"]').click();
    d.querySelector('[data-row="a1"] [data-act="pool"][data-prov="llama"]').click();
    d.querySelector('[data-row="a1"] [data-act="pause"]').click();
    d.querySelector('[data-row="p1"] [data-act="approve"]').click();
    d.getElementById('agAuthTg').click();
    d.getElementById('adminRefreshStamp').click();
    expect(win.__calls).toContainEqual(['adminTogglePrimary', 'a1', 'llama', true]);
    expect(win.__calls).toContainEqual(['adminTogglePool', 'llama', 'a1', true]);
    expect(win.__calls).toContainEqual(['adminToggleCollection', 'a1', false]);
    expect(win.__calls).toContainEqual(['adminApprove', 'p1']);
    expect(win.__calls).toContainEqual(['adminToggleAuth', true]);   // security was on → now disabled
    expect(win.__calls).toContainEqual(['adminRefreshNow']);
    expect(d.getElementById('adminRefreshRf').className).toContain('busy');
    expect(d.getElementById('agAuthRow').classList.contains('warn')).toBe(true);
  });
  test('filter box narrows the roster; empty state names the query', () => {
    const win = harness(boot([approved('alpha'), approved('beta')]));
    const d = win.document;
    const q = d.getElementById('agFilter');
    q.value = 'beta'; q.dispatchEvent(new win.Event('input'));
    expect(d.querySelectorAll('#agRoster .ag-rw:not(.hd)').length).toBe(1);
    q.value = 'state:pending'; q.dispatchEvent(new win.Event('input'));
    expect(d.querySelector('#agRoster .ag-empty').textContent).toContain('state:pending');
  });
  test('row menu opens as a fixed layer under its button and closes on scroll', () => {
    const win = harness(boot([approved('a1')]));
    const d = win.document;
    const btn = d.querySelector('[data-row="a1"] [data-act="menu"]');
    btn.getBoundingClientRect = () => ({ top: 500, bottom: 526, left: 1200, right: 1226 });
    btn.click();
    const menu = d.querySelector('[data-row="a1"] .mc-menu');
    expect(menu.classList.contains('open')).toBe(true);
    expect(menu.style.position).toBe('fixed');
    expect(menu.style.top).toBe('532px');
    expect(menu.style.zIndex).toBe('1200');
    d.dispatchEvent(new win.Event('scroll'));
    expect(menu.classList.contains('open')).toBe(false);
    expect(menu.style.position).toBe('');
  });
  test('icon buttons are inline SVGs with instant tips', () => {
    const win = harness(boot([approved('a1')]));
    const row = win.document.querySelector('[data-row="a1"]');
    for (const act of ['pause', 'restart', 'menu']) {
      const b = row.querySelector(`[data-act="${act}"]`);
      expect(b.querySelector('svg')).toBeTruthy();
      expect(b.textContent.trim()).toBe('');
    }
    expect(row.querySelector('[data-act="pause"]').dataset.tip).toBe('Pause collection');
    expect(row.querySelector('[data-act="restart"]').dataset.tip).toBe('Restart agent');
  });
  test('default order puts co-located infra hosts first, then provider hosts; Capabilities is sortable', () => {
    const infra = [{ role: 'manager', version: 'v9' }];
    const win = harness(boot([approved('zed', { capabilities: { lms: true } }), approved('mgr', { capabilities: { sysperf: true }, colocated_infra: infra }),
      approved('amy', { capabilities: { sysperf: true } }), approved('bob', { capabilities: { llama: true } })]));
    const d = win.document;
    const hosts = () => [...d.querySelectorAll('#agRoster .ag-rw:not(.hd) .host')].map(e => e.textContent);
    expect(hosts()).toEqual(['mgr', 'bob', 'zed', 'amy']);
    expect(d.querySelector('#agRoster [data-sort="caps"]').classList.contains('on')).toBe(true);
    d.querySelector('#agRoster [data-sort="caps"]').click();
    expect(hosts()).toEqual(['amy', 'zed', 'bob', 'mgr']);
  });
  test('identity block nests description + os · role under the name', () => {
    const win = harness(boot([approved('a1', { description: 'Primary box' })]));
    const who = win.document.querySelector('[data-row="a1"] .ag-who');
    expect(who.querySelector('.sub .d').textContent).toBe('Primary box');
    expect(who.querySelector('.sub .m').textContent).toBe('linux·llama host');
  });
  test('hostnames are escaped', () => {
    const win = harness(boot([approved('x', { hostname: '<img src=x onerror=1>' })]));
    expect(win.document.querySelector('[data-row="x"] .host').innerHTML).toBe('&lt;img src=x onerror=1&gt;');
  });
});

describe('#793 wiring', () => {
  test('index.html loads agents.css + admin-agents.js and drops the old table ids', () => {
    expect(indexSrc).toMatch(/css\/agents\.css\?v=[\w.-]+/);
    expect(indexSrc).toMatch(/js\/admin-agents\.js\?v=[\w.-]+/);
    for (const id of ['adminAgentsTable', 'adminAuthDisabled', 'adminLatestVersion', 'adminAgentsCount', 'adminLastRefresh', 'adminHealthRefresh', 'auStamp', 'agStamp', 'agFoot']) {
      expect(indexSrc).not.toContain(`id="${id}"`);
    }
    expect(indexSrc).toContain('id="adminRefreshStamp"');
    expect(indexSrc).not.toMatch(/id="adminRefreshStamp"[^>]*data-tip/);
    expect(indexSrc).not.toContain('Disable agent auth');
  });
  test('admin.js no longer renders the table itself', () => {
    for (const fn of ['_adminRenderAgentsTable', '_adminRowHtml', '_adminCapsAndPrimary', '_adminActions', '_adminMenuToggle']) {
      expect(adminSrc).not.toContain(`function ${fn}`);
    }
  });
});
