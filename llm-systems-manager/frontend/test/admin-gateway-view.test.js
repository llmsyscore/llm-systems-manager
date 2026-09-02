// #797: Routing → Inference Gateway card — client/host diagram, metric tiles,
// the off state and the enable toggle.
import { describe, test, expect } from 'vitest';
import { srcFile, runHarness, flush } from './helpers/harness.js';

const gwSrc = srcFile('js/admin-gateway.js');
const indexSrc = srcFile('index.html');
const CARD = indexSrc.slice(indexSrc.indexOf('<div class="card" id="rtGatewayCard"'),
                            indexSrc.indexOf('<div class="card" id="apEntriesCard">'));

const FLOW = {
  ok: true, enabled: true, endpoint: '/api/gateway/v1', keys: 3, usage_probe: true,
  clients: [
    { label: 'ops-1', ip: '192.168.1.30', req_per_min: 8, inflight: 2, prompt_tokens: 10,
      gen_tokens: 5, last_seen_s: 0.4, state: 'ok' },
    { label: 'dev', ip: '192.168.1.22', req_per_min: 4, inflight: 1, last_seen_s: 3, state: 'ok' },
    { label: 'auto', ip: '192.168.1.40', req_per_min: 0, inflight: 0, last_seen_s: 7200, state: 'idle' },
  ],
  hosts: [
    { agent_id: 'a1', hostname: 'llm-syscore', provider: 'llama', model: 'Qwen2.5-32B',
      gen_tps: 61, inflight: 2, state: 'ok' },
    { agent_id: 'a2', hostname: 'gpu-box', provider: 'llama', model: 'Llama-3.3-70B',
      gen_tps: 31, inflight: 1, state: 'ok' },
    { agent_id: 'a3', hostname: 'mac-mini', provider: 'lms', model: null,
      gen_tps: 0, inflight: 0, state: 'idle' },
  ],
  totals: { req_per_min: 12, prompt_tps: 1800, gen_tps: 92, p50_ms: 640, inflight: 3, errors_15m: 0 },
  energy: { serving_w: 412, kwh_today: 0.9, cost_today: 0.14, usd_per_mtok: 0.06, cloud_usd_per_mtok: 2.5 },
};

function card(payload) {
  return runHarness({
    sources: [gwSrc],
    bodyHtml: `<div id="adminTab"><div id="admin-routing">${CARD}</div></div>`,
    bootstrap: `GatewayView.render(${JSON.stringify(payload)});`,
  });
}

describe('gateway diagram', () => {
  test('renders one node per client and host plus the gateway box', () => {
    const doc = card(FLOW).document;
    const names = [...doc.querySelectorAll('#rtGwDiagram .rt-node .nm')].map(n => n.textContent);
    expect(names).toEqual(['ops-1', 'dev', 'auto', 'Gateway', 'llm-syscore', 'gpu-box']);   // idle mac-mini is not drawn while others serve
    expect(doc.querySelector('#rtGwDiagram .rt-node.gwn')).toBeTruthy();
  });

  test('client sub-labels carry the key and the IP', () => {
    const doc = card(FLOW).document;
    const subs = [...doc.querySelectorAll('#rtGwDiagram .rt-node .sb')].map(n => n.textContent);
    expect(subs[0]).toBe('key ops-1 · 192.168.1.30');
    expect(subs).toContain('llama · Qwen2.5-32B');
    expect(subs).not.toContain('lms · idle');
  });

  test('an idle client edge goes off and its label reads idle', () => {
    const doc = card(FLOW).document;
    const edges = [...doc.querySelectorAll('#rtGwDiagram .rt-e')].map(e => e.getAttribute('class'));
    expect(edges.filter(c => c.includes('off')).length).toBe(1);   // idle client
    const labels = [...doc.querySelectorAll('#rtGwDiagram .rt-el text')].map(t => t.textContent);
    expect(labels).toContain('8 req/min');
    expect(labels).toContain('61 tok/s · 2');
    expect(labels.some(l => l.startsWith('idle'))).toBe(true);
  });

  test('the gateway box shows requests, in flight, p50 and errors', () => {
    const doc = card(FLOW).document;
    const gw = doc.querySelector('#rtGwDiagram .rt-node.gwn');
    expect(gw.textContent).toContain('12 req/min · 3 in flight');
    expect(gw.textContent).toContain('p50 640 ms · 0 errors');
  });

  test('hostnames are set as SVG text, never parsed as markup', () => {
    const doc = card({ ...FLOW, hosts: [{ hostname: '<img src=x>', provider: 'llama', gen_tps: 1, inflight: 0, state: 'ok' }] }).document;
    expect(doc.querySelector('#rtGwDiagram').innerHTML).not.toContain('<img');
  });
});

describe('metric tiles', () => {
  test('eight tiles in the spec order, with k-scaled numbers', () => {
    const win = card(FLOW);
    const t = win.GatewayView.tiles(FLOW);
    expect(t.map(x => x[0])).toEqual(['requests', 'prompt tokens', 'generated', 'latency p50',
      'in flight', 'serving power', 'energy today', 'per 1M tokens']);
    expect(t[1][1]).toBe('1.8k');
    expect(t[6][2]).toBe('kWh · $0.14');
    expect(t[7]).toEqual(['per 1M tokens', '$0.06', 'vs cloud $2.50']);
    expect(win.document.querySelectorAll('#rtGwTiles .rt-gm')).toHaveLength(8);
  });

  test('null latency and null energy render as — not 0', () => {
    const t = card(FLOW).GatewayView.tiles({
      totals: { req_per_min: 0, prompt_tps: 0, gen_tps: 0, p50_ms: null, inflight: 0 },
      energy: { serving_w: 300, kwh_today: null, cost_today: null, usd_per_mtok: null, cloud_usd_per_mtok: 2.5 },
    });
    expect(t[3][1]).toBe('—');
    expect(t[6][1]).toBe('—');
    expect(t[7][1]).toBe('—');
    expect(t[5][1]).toBe('300');
  });
});

describe('off state', () => {
  test('the card takes .off, the toggle clears and the note appears', () => {
    const doc = card({ ...FLOW, enabled: false }).document;
    const cardEl = doc.getElementById('rtGatewayCard');
    expect(cardEl.classList.contains('off')).toBe(true);
    expect(doc.getElementById('rtGwToggle').classList.contains('on')).toBe(false);
    expect(doc.getElementById('rtGwOffNote').textContent).toContain('answers 503 until it is turned back on');
    expect(doc.getElementById('rtGwMeta').textContent).toContain('off');
    expect(doc.getElementById('rtGwMeta').textContent).toContain('3 API keys');
  });

  test('on state keeps the endpoint, key count and usage probe in the meta', () => {
    const doc = card(FLOW).document;
    expect(doc.getElementById('rtGatewayCard').classList.contains('off')).toBe(false);
    expect(doc.getElementById('rtGwMeta').textContent)
      .toBe('/api/gateway/v1 · 3 API keys · usage probe on');
    expect(doc.getElementById('rtGwToggle').classList.contains('on')).toBe(true);
  });

  test('the last live picture stays on screen while off', () => {
    const win = card(FLOW);
    win.GatewayView.render({ ...FLOW, enabled: false });
    expect(win.document.querySelectorAll('#rtGwDiagram .rt-node').length).toBe(6);
  });
});

describe('polling and the enable toggle', () => {
  test('an unavailable endpoint hides the card entirely', async () => {
    const win = runHarness({
      sources: [gwSrc],
      bodyHtml: `<div id="adminTab"><div id="admin-routing">${CARD}</div></div>`,
      bootstrap: `window.fetch = () => Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
        window.__done = GatewayView.refresh();`,
    });
    await win.__done;
    expect(win.document.getElementById('rtGatewayCard').hidden).toBe(true);
  });

  test('a payload unhides the card', async () => {
    const win = runHarness({
      sources: [gwSrc],
      bodyHtml: `<div id="adminTab"><div id="admin-routing">${CARD}</div></div>`,
      bootstrap: `window.fetch = () => Promise.resolve({ ok: true, status: 200, json: async () => JSON.parse(atob('${Buffer.from(JSON.stringify(FLOW)).toString('base64')}')) });
        window.__done = GatewayView.refresh();`,
    });
    await win.__done;
    expect(win.document.getElementById('rtGatewayCard').hidden).toBe(false);
  });

  test('the toggle PUTs the new state and does not collapse the card', async () => {
    const win = runHarness({
      sources: [gwSrc],
      bodyHtml: `<div id="adminTab"><div id="admin-routing">${CARD}</div></div>`,
      bootstrap: `window.__calls = [];
        window.fetch = (url, opts) => { window.__calls.push([String(url), opts && opts.body]);
          return Promise.resolve({ ok: true, status: 200, json: async () => JSON.parse(atob('${Buffer.from(JSON.stringify(FLOW)).toString('base64')}')) }); };
        GatewayView.render(${JSON.stringify(FLOW)});
        GatewayView.start();
        document.getElementById('rtGwToggle').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));`,
    });
    await flush();
    const put = win.__calls.find(c => c[0] === '/api/admin/gateway');
    expect(put).toBeTruthy();
    expect(JSON.parse(put[1])).toEqual({ enabled: false });
    expect(win.document.getElementById('rtGatewayCard').classList.contains('collapsed')).toBe(false);
    win.GatewayView.stop();
  });

  test('the card header collapses on click and the poll interval is 5 s', () => {
    const win = card(FLOW);
    win.GatewayView.start();
    win.document.getElementById('rtGwHead').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    expect(win.document.getElementById('rtGatewayCard').classList.contains('collapsed')).toBe(true);
    expect(win.GatewayView.POLL_MS).toBe(5000);
    win.GatewayView.stop();
  });
});

describe('host list follows activity (#797)', () => {
  test('with nothing serving, only each provider primary is drawn, dimmed, with off edges', () => {
    const hosts = [
      { agent_id: 'a1', hostname: 'llm-syscore', provider: 'llama', model: 'Qwen2.5-32B', gen_tps: 0, inflight: 0, state: 'ok', primary: true },
      { agent_id: 'a2', hostname: 'gpu-box', provider: 'llama', model: null, gen_tps: 0, inflight: 0, state: 'idle', primary: false },
      { agent_id: 'a3', hostname: 'mac-mini', provider: 'lms', model: null, gen_tps: 0, inflight: 0, state: 'idle', primary: true },
    ];
    const doc = card({ ...FLOW, hosts }).document;
    const names = [...doc.querySelectorAll('#rtGwDiagram .rt-node .nm')].map(n => n.textContent);
    expect(names.slice(-2)).toEqual(['llm-syscore', 'mac-mini']);
    const classes = [...doc.querySelectorAll('#rtGwDiagram .rt-node')].map(n => n.getAttribute('class'));
    expect(classes.filter(c => c.includes('dim')).length).toBe(1);   // resident model, no traffic
    const hostEdges = [...doc.querySelectorAll('#rtGwDiagram .rt-e')].slice(-2).map(e => e.getAttribute('class'));
    expect(hostEdges.every(c => c.includes('off'))).toBe(true);
  });

  test('rows grow with the fleet and the gateway box stays centred', () => {
    const hosts = Array.from({ length: 5 }, (_, i) => ({ agent_id: 'h' + i, hostname: 'host-' + i, provider: 'llama',
      model: 'm', gen_tps: 10 + i, inflight: 1, state: 'ok', primary: i === 0 }));
    const doc = card({ ...FLOW, hosts }).document;
    const svg = doc.querySelector('#rtGwDiagram svg');
    expect(svg.getAttribute('viewBox')).toBe('0 0 720 346');
    expect(doc.querySelectorAll('#rtGwDiagram .rt-node').length).toBe(3 + 1 + 5);
    expect(doc.querySelector('#rtGwDiagram .rt-node.gwn').getAttribute('transform')).toBe('translate(266,140)');
  });
});
