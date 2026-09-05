// #797: System Health card — service rows, data-flow edge/node states, the
// overall pill, warning ordering and the per-node detail strip.
import { describe, test, expect } from 'vitest';
import { srcFile, runHarness } from './helpers/harness.js';

const adminSrc = srcFile('js/admin.js');
const healthSrc = srcFile('js/admin-health.js');
const indexSrc = srcFile('index.html');
const CARD = indexSrc.slice(indexSrc.indexOf('<div id="adminHealthCard">'),
                            indexSrc.indexOf('<!-- Sub-tabs underneath System Health -->'));

function card(d, rel, extra) {
  const win = runHarness({
    sources: [adminSrc, healthSrc],
    bodyHtml: `<div id="adminTab">${CARD}</div>`,
    bootstrap: `HealthView.render(${JSON.stringify(d)}, ${JSON.stringify(rel || null)}); ${extra || ''}`,
  });
  return win.document;
}
function view() {
  return runHarness({ sources: [adminSrc, healthSrc] }).HealthView;
}

const HEALTHY = {
  overall: 'ok',
  manager: {
    ok: true, version: 'v2026.09.02-5', uptime_s: 22320,
    streams: { active: 3, limit: 32, peak: 9, refusals: 0 },
    connections: { browsers: 2, agents: 9, worker_threads: 32, worker_threads_busy: 4 },
    push_subscriptions: 1,
  },
  services: [
    { name: 'alarm_engine', ok: true, latency_ms: 12, version: 'v2026.09.02-1', uptime_s: 273600,
      tls: { enabled: true, active: true }, rule_eval_ms: 42, ingest_points_per_s: 42, active_alerts: 1 },
    { name: 'influxdb', ok: true, state: 'connected', version: '2.7.11', ping_ms: 3, writes_per_s: 40 },
  ],
  agents: [
    { id: 'a1', hostname: 'h1', status: 'approved', liveness: 'live', tls_direction: 'both' },
    { id: 'a2', hostname: 'h2', status: 'approved', liveness: 'stale', tls_direction: 'both' },
  ],
  data_flow: {
    primary_llama_push: { has_agent: true, ok: true, age_s: 4 },
    primary_lms_push: { has_agent: true, ok: true, age_s: 6 },
    primary_vllm_push: { has_agent: false },
  },
  flow: { agent_pushes_per_s: 3, ae_ingest_points_per_s: 42, influx_writes_per_s: 40, history_req_per_s: 0.8 },
  agent_update: { latest: 'v2026.09.01-3', outdated: 0, hostnames: [] },
  ae_restart: { available: true, via: 'systemctl' },
  warnings: [],
};

const DOWN = {
  overall: 'down',
  manager: { ok: true, version: 'v2026.09.02-5', uptime_s: 22320,
             streams: { active: 31, limit: 32, peak: 32, refusals: 4 },
             connections: { browsers: 4, agents: 8, worker_threads: 32, worker_threads_busy: 29 } },
  services: [
    { name: 'alarm_engine', ok: false, error: 'ConnectTimeout', url: 'https://ae:8081',
      tls: { enabled: true, active: false, error: 'cert missing' } },
    { name: 'influxdb', ok: false, via: 'alarm_engine (unreachable)' },
  ],
  agents: [{ id: 'a1', hostname: 'gpu-box', status: 'approved', liveness: 'down', tls_direction: 'none' }],
  data_flow: {
    primary_llama_push: { has_agent: true, ok: false, age_s: 84 },
    primary_lms_push: { has_agent: true, ok: true, age_s: 6 },
    primary_vllm_push: { has_agent: false },
  },
  flow: { agent_pushes_per_s: 1, ae_ingest_points_per_s: null, influx_writes_per_s: null, history_req_per_s: 0 },
  ae_restart: { available: true, via: 'self-restart' },
  agent_update: { latest: 'v2026.09.01-3', outdated: 3, hostnames: ['a', 'b', 'c'] },
  warnings: ['alarm engine unreachable: ConnectTimeout', 'agent gpu-box TLS cert expires in 9d'],
};

describe('services column', () => {
  test('three rows: manager, alarm engine, influxdb — with versions and uptime', () => {
    const rows = view().svcRows(HEALTHY);
    expect(rows.map(r => r.n)).toEqual(['Manager', 'Alarm Engine', 'InfluxDB']);
    expect(rows[0].ver).toBe('v2026.09.02-5');
    expect(rows[0].up).toBe('6h 12m');
    expect(rows[1].up).toBe('3d 4h');
    expect(rows[1].lk).toEqual(['https', 'on']);
    expect(rows[2].upTxt).toBe('connected');
    expect(rows[2].act).toBeNull();
  });

  test('a cert-missing engine chips red and reads unreachable', () => {
    const rows = view().svcRows(DOWN);
    expect(rows[1].lk).toEqual(['cert missing', 'crit']);
    expect(rows[1].upTxt).toBe('unreachable');
    expect(rows[1].upCls).toBe('crit');
    expect(rows[2].upTxt).toBe('unknown');
  });

  test('the AE restart button is always rendered and names the self-restart API', () => {
    const doc = card(DOWN);
    const html = doc.getElementById('adminHealthServices').innerHTML;
    expect(html).toContain('data-restart-svc="alarm_engine"');
    expect(html).toContain('via its self-restart API');
    expect(html).toContain('data-restart-svc="manager"');
  });

  test('the restart button dispatches to admin.js _restartService', () => {
    const win = runHarness({
      sources: [adminSrc, healthSrc],
      bodyHtml: `<div id="adminTab">${CARD}</div>`,
      bootstrap: `window.__calls = [];
        _restartService = (s) => window.__calls.push(s);
        HealthView.render(${JSON.stringify(HEALTHY)}, null);
        document.querySelector('[data-restart-svc="alarm_engine"]').click();`,
    });
    expect(win.__calls).toEqual(['alarm_engine']);
  });
});

describe('data-flow edges and nodes', () => {
  test('healthy payload drives five ok edges with rate labels', () => {
    const e = view().edgeStates(HEALTHY);
    expect(e.eAgMg).toMatchObject({ state: 'ok', label: '3 push/s' });
    expect(e.eAgAe).toMatchObject({ state: 'ok', label: '42 metrics/s' });
    expect(e.eMgAe).toMatchObject({ state: 'ok', label: 'history 0.8/s' });
    expect(e.eMgBr).toMatchObject({ state: 'ok', label: '3 streams' });
    expect(e.eAeIn).toMatchObject({ state: 'ok', label: '40 writes/s' });
  });

  test('an unreachable engine makes its edges crit and the Influx edge off', () => {
    const e = view().edgeStates(DOWN);
    expect(e.eAgAe.state).toBe('crit');
    expect(e.eMgAe.state).toBe('crit');
    expect(e.eAeIn.state).toBe('off');
    expect(e.eAeIn.label).toBe('—');
  });

  test('a stale push warns and a near-full stream pool warns', () => {
    const e = view().edgeStates(DOWN);
    expect(e.eAgMg.state).toBe('warn');
    expect(e.eMgBr.state).toBe('warn');
    expect(e.eMgBr.label).toBe('31 streams');
  });

  test('null AE counters render as — and never as zero', () => {
    const e = view().edgeStates({
      ...HEALTHY,
      flow: { agent_pushes_per_s: 3, ae_ingest_points_per_s: null,
              influx_writes_per_s: null, history_req_per_s: null },
    });
    expect(e.eAgAe.label).toBe('—');
    expect(e.eMgAe.label).toBe('—');
    expect(e.eAeIn.label).toBe('—');
    expect(e.eAgAe.state).toBe('ok');
  });

  test('node sub-labels count agents, browsers and ingest', () => {
    const s = view().nodeSubs(HEALTHY);
    expect(s.nAg).toBe('1 / 2 live');
    expect(s.nMg).toBe('this host');
    expect(s.nBr).toBe('2 tabs · 1 phone');
    expect(s.nAe).toBe('42 points/s');
    expect(s.nIn).toBe('connected');
  });

  test('nodes follow the worst incident edge', () => {
    const doc = card(DOWN);
    expect(doc.getElementById('hcnAe').getAttribute('class')).toContain('crit');
    expect(doc.getElementById('hcnBr').getAttribute('class')).toContain('warn');
    expect(doc.getElementById('hcnMg').getAttribute('class')).toContain('crit');
  });

  test('edge animation variables scale with the rate', () => {
    const doc = card(HEALTHY);
    const e = doc.getElementById('hceAgAe');
    expect(e.getAttribute('class')).toBe('hc-e ok');
    expect(e.getAttribute('marker-end')).toBe('url(#hcAhOk)');
    expect(e.style.getPropertyValue('--dur')).toBeTruthy();
    expect(e.style.getPropertyValue('--w')).toBeTruthy();
  });
});

describe('overall pill wording', () => {
  test('Healthy when nothing but note rows', () => {
    const doc = card({ ...HEALTHY, agent_update: { latest: 'v9', outdated: 2 } });
    expect(doc.getElementById('adminHealthOverall').textContent).toBe('Healthy');
    expect(doc.getElementById('adminHealthOverall').className).toBe('pill ok');
  });
  test('Attention when a warn row is present', () => {
    const doc = card({ ...HEALTHY, warnings: ['agent mac-mini TLS cert expires in 9d'] });
    expect(doc.getElementById('adminHealthOverall').textContent).toBe('Attention');
    expect(doc.getElementById('adminHealthOverall').className).toBe('pill warn');
  });
  test('Down when the roll-up says down', () => {
    const doc = card(DOWN);
    expect(doc.getElementById('adminHealthOverall').textContent).toBe('Down');
    expect(doc.getElementById('adminHealthOverall').className).toBe('pill crit');
  });
});

describe('warnings column', () => {
  test('empty renders the mono None only', () => {
    const doc = card(HEALTHY);
    const html = doc.getElementById('adminHealthWarnings').innerHTML;
    expect(html).toBe('<div class="w-none">None</div>');
  });

  test('crit / warn / note / info order with the right glyphs', () => {
    const doc = card(DOWN, { enabled: true, update_available: null, note: 'no tag' });
    const rows = [...doc.querySelectorAll('#adminHealthWarnings .w')];
    expect(rows.map(r => r.className)).toEqual(['w crit', 'w warn', 'w note', 'w info']);
    expect(rows.map(r => r.querySelector('.g').textContent)).toEqual(['▲', '!', '↑', 'i']);
  });

  test('the agent-update row offers Update all and calls adminUpdateAll', () => {
    const win = runHarness({
      sources: [adminSrc, healthSrc],
      bodyHtml: `<div id="adminTab">${CARD}</div>`,
      bootstrap: `window.__called = 0;
        adminUpdateAll = () => { window.__called++; };
        HealthView.render(${JSON.stringify(DOWN)}, null);
        document.querySelector('#adminHealthWarnings [data-act="updateall"]').click();`,
    });
    expect(win.__called).toBe(1);
  });

  test('server warning text is escaped, never parsed as markup', () => {
    const doc = card({ ...HEALTHY, warnings: ['<img src=x onerror=1>'] });
    const html = doc.getElementById('adminHealthWarnings').innerHTML;
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });
});

describe('node detail strip', () => {
  test('no node strip is open on entry (#837); selecting the alarm engine shows its metrics', () => {
    const doc = card(HEALTHY);
    expect(doc.getElementById('adminHealthDetail').textContent).toBe('');
    expect(doc.getElementById('hcnAe').getAttribute('class')).not.toContain('sel');
    const sel = card(HEALTHY, null, "HealthView.select('ae');");
    const det = sel.getElementById('adminHealthDetail');
    expect(det.textContent).toContain('Alarm Engine');
    expect(det.textContent).toContain('42 points/s');
    expect(det.textContent).toContain('42 ms');
    expect(sel.getElementById('hcnAe').getAttribute('class')).toContain('sel');
  });

  test('clicking a node swaps the strip; clicking it again closes it', () => {
    const win = runHarness({
      sources: [adminSrc, healthSrc],
      bodyHtml: `<div id="adminTab">${CARD}</div>`,
      bootstrap: `HealthView.render(${JSON.stringify(HEALTHY)}, null);
        document.getElementById('hcnAg').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
        window.__afterAgents = document.getElementById('adminHealthDetail').textContent;
        document.getElementById('hcnAg').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
        window.__afterClose = document.getElementById('adminHealthDetail').innerHTML;`,
    });
    expect(win.__afterAgents).toContain('Agents');
    expect(win.__afterAgents).toContain('TLS both ways');
    expect(win.__afterClose).toBe('');
  });

  test('the ✕ closes the strip', () => {
    const win = runHarness({
      sources: [adminSrc, healthSrc],
      bodyHtml: `<div id="adminTab">${CARD}</div>`,
      bootstrap: `HealthView.render(${JSON.stringify(HEALTHY)}, null); HealthView.select('ae');
        window.__open = document.getElementById('adminHealthDetail').innerHTML.length > 0;
        document.querySelector('#adminHealthDetail [data-close]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
        window.__html = document.getElementById('adminHealthDetail').innerHTML;`,
    });
    expect(win.__open).toBe(true);
    expect(win.__html).toBe('');
  });

  test('an unreachable engine strip reports the failure instead of metrics', () => {
    const rows = view().detailRows(DOWN, 'ae').map(r => r[0]);
    expect(rows).toContain('last good probe');
    expect(rows).toContain('failed probes');
    expect(rows).toContain('error');
  });

  test('the manager strip reports streams, workers and the engine probe', () => {
    const rows = Object.fromEntries(view().detailRows(HEALTHY, 'manager').map(r => [r[0], r[1]]));
    expect(rows.streams).toBe('3 / 32 · peak 9 · refusals 0');
    expect(rows['worker threads']).toBe('4 / 32 busy');
    expect(rows['engine probe']).toBe('12 ms');
  });
});

describe('alarm-engine auth posture (#828)', () => {
  const withAuth = (auth, detail, bearer) => {
    const d = JSON.parse(JSON.stringify(HEALTHY));
    const ae = d.services.find(s => s.name === 'alarm_engine');
    if (auth !== undefined) ae.auth = auth;
    if (detail !== undefined) ae.auth_detail = detail;
    if (bearer !== undefined) ae.bearer_configured = bearer;
    return d;
  };
  const OPEN = { management: 'open', ingest: 'open', loopback_only: false, open_on_network: true, bearer_ok: null };
  const ENF = { management: 'management_token', ingest: 'enforced', loopback_only: false, open_on_network: false, bearer_ok: true };

  test('an open engine on the network chips "auth open" with the remedy', () => {
    const d = withAuth('open', OPEN, true);
    const rows = view().svcRows(d);
    expect(rows[1].ak).toEqual({ chip: 'auth open', tip: expect.stringContaining('Admin → Settings → Alarm engine') });
    const html = card(d).getElementById('adminHealthServices').innerHTML;
    expect(html).toContain('>auth open<');
    expect(html).toContain('management_token on both hosts');
    expect(view().detailRows(d, 'ae')).toContainEqual(['auth', 'open — no token on the engine', 'warn']);
  });

  test('an open engine bound to loopback is not flagged', () => {
    const d = withAuth('open', { ...OPEN, loopback_only: true, open_on_network: false }, true);
    expect(view().svcRows(d)[1].ak).toBeNull();
    expect(view().detailRows(d, 'ae')).toContainEqual(['auth', 'open (loopback bind only)', '']);
  });

  test('an enforcing engine with no manager token chips "no token"', () => {
    const d = withAuth('enforced', { ...ENF, bearer_ok: null }, false);
    expect(view().svcRows(d)[1].ak).toEqual({ chip: 'no token', tip: expect.stringContaining('Admin → Settings') });
    expect(view().detailRows(d, 'ae')).toContainEqual(['auth', 'enforced — manager sends no token', 'warn']);
  });

  test('an enforcing engine that rejects the manager token chips "token mismatch"', () => {
    const d = withAuth('enforced', { ...ENF, bearer_ok: false }, true);
    expect(view().svcRows(d)[1].ak).toEqual({ chip: 'token mismatch', tip: expect.stringContaining('both hosts') });
    expect(view().detailRows(d, 'ae')).toContainEqual(['auth', 'enforced — engine rejects the manager token', 'warn']);
    const html = card(d).getElementById('adminHealthServices').innerHTML;
    expect(html).toContain('>token mismatch<');
  });

  test('an enforcing engine with a manager token has no chip and an ok detail row', () => {
    const d = withAuth('enforced', ENF, true);
    expect(view().svcRows(d)[1].ak).toBeNull();
    expect(view().detailRows(d, 'ae')).toContainEqual(['auth', 'management_token', 'ok']);
    const fb = withAuth('enforced', { ...ENF, management: 'ingest_token' }, true);
    expect(view().detailRows(fb, 'ae')).toContainEqual(['auth', 'ingest_token (fallback)', 'ok']);
  });

  test('an older engine without the field reads unknown and is not flagged', () => {
    expect(view().svcRows(HEALTHY)[1].ak).toBeNull();
    expect(view().detailRows(HEALTHY, 'ae')).toContainEqual(['auth', 'unknown', '']);
    expect(view().authState(null)).toBeNull();
  });

  test('the backend auth warning ranks as warn, so the pill reads Attention', () => {
    const d = withAuth('open', OPEN, true);
    d.warnings = ['alarm engine auth open: no management_token or ingest_token on the engine — set the same [alarm_engine].management_token on both hosts (Admin → Settings → Alarm engine)'];
    const rows = view().warnRows(d, null);
    expect(rows[0].k).toBe('warn');
    expect(view().pillOf(d, rows)).toEqual(['warn', 'Attention']);
  });
});
