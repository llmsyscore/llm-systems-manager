// Admin → Routing → Inference Gateway card (#797): live clients → gateway →
// hosts diagram plus the throughput/energy tiles, polled while Routing shows.
(() => {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';
  const $ = id => document.getElementById(id);
  const POLL_MS = 5000;
  // Row pitch and edge geometry for the clients → gateway → hosts canvas.
  const ROW_PITCH = 66, ROW_TOP = 41, MAX_ROWS = 8;
  const busy = h => (h.inflight || 0) > 0 || (h.gen_tps || 0) > 0;
  // Hosts drawn: the ones serving right now; with nothing in flight, each provider's primary, dimmed.
  function visibleHosts(hosts) {
    const active = hosts.filter(busy).sort((a, b) => (b.gen_tps || 0) - (a.gen_tps || 0));
    if (active.length) return active.slice(0, MAX_ROWS);
    return hosts.filter(h => h.primary).slice(0, MAX_ROWS);
  }
  function rowsY(count) {
    return Array.from({ length: count }, (_, i) => ROW_TOP + i * ROW_PITCH);
  }

  let _timer = null;
  let _last = null;
  let _wired = false;

  function n(el, attrs, text) {
    const e = document.createElementNS(NS, el);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, String(v));
    if (text != null) e.textContent = String(text);
    return e;
  }
  function kfmt(v, digits) {
    if (v == null || isNaN(v)) return '—';
    const x = Number(v);
    if (Math.abs(x) >= 1000) return (x / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    if (Number.isInteger(x)) return String(x);
    return x.toFixed(digits == null ? 1 : digits);
  }
  function money(v) { return v == null || isNaN(v) ? '—' : '$' + Number(v).toFixed(Number(v) < 1 ? 2 : 2); }
  function ago(s) {
    if (s == null) return 'idle';
    if (s < 90) return `idle ${Math.round(s)} s`;
    if (s < 5400) return `idle ${Math.round(s / 60)} m`;
    return `idle ${Math.round(s / 3600)} h`;
  }

  // ── diagram ──────────────────────────────────────────────────────────
  function labelGroup(cx, y, text, off) {
    const g = n('g', { class: 'rt-el' + (off ? ' off' : '') });
    const w = Math.round(String(text).length * 5.9 + 7);
    g.appendChild(n('rect', { x: cx - w / 2, y, width: w, height: 14, rx: 3 }));
    g.appendChild(n('text', { x: cx, y: y + 10, 'text-anchor': 'middle' }, text));
    return g;
  }

  // Shrinks, then ellipsises, an SVG text run so it stays inside its box.
  function fitText(t, maxW) {
    if (!t || typeof t.getComputedTextLength !== 'function') return;
    const fits = () => { try { return t.getComputedTextLength() <= maxW; } catch (_) { return true; } };
    let size = 9.5;
    while (!fits() && size > 8) { size -= 0.5; t.style.fontSize = size + 'px'; }
    let s = t.textContent;
    while (!fits() && s.length > 4) { s = s.slice(0, -2); t.textContent = s + '…'; }
  }
  function boxNode(x, y, w, name, sub, cls) {
    const g = n('g', { class: 'rt-node' + (cls ? ' ' + cls : ''), transform: `translate(${x},${y})` });
    g.appendChild(n('rect', { width: w, height: sub.length > 1 ? 66 : 46, rx: 7 }));
    g.appendChild(n('circle', { cx: 13, cy: 16, r: 3 }));
    g.appendChild(n('text', { class: 'nm', x: 22, y: 19.5 }, name));
    sub.forEach((s, i) => g.appendChild(n('text', { class: 'sb', x: 22, y: 35 + i * 16.5 }, s)));
    return g;
  }
  function edge(d, on, rate) {
    const p = n('path', { class: 'rt-e ' + (on ? 'ok' : 'off'), d,
      'marker-end': on ? 'url(#rtAh)' : 'url(#rtAhOff)' });
    if (on) {
      const r = Math.max(0, Math.min(1, rate || 0));
      p.style.setProperty('--dur', (1.6 - 0.9 * r).toFixed(2) + 's');
      p.style.setProperty('--w', (1.5 + 0.9 * r).toFixed(2));
      p.style.setProperty('--op', (0.7 + 0.3 * r).toFixed(2));
    }
    return p;
  }

  function buildSvg(d) {
    const clients = (d.clients || []).slice(0, MAX_ROWS);
    const hosts = visibleHosts(d.hosts || []);
    const rows = Math.max(1, clients.length, hosts.length);
    const height = ROW_TOP + (rows - 1) * ROW_PITCH + 23 + 18;
    const gy = ROW_TOP + Math.round((rows - 1) * ROW_PITCH / 2);
    const svg = n('svg', { viewBox: `0 0 720 ${height}`, class: 'rt-svg', role: 'img',
      'aria-label': 'Live inference flow from clients through the gateway to the serving hosts' });
    const defs = n('defs');
    for (const [id, cls] of [['rtAh', 'rt-ah'], ['rtAhOff', 'rt-ah off']]) {
      const m = n('marker', { id, viewBox: '0 0 8 8', refX: 7, refY: 4, markerUnits: 'userSpaceOnUse',
        markerWidth: 8, markerHeight: 8, orient: 'auto-start-reverse' });
      m.appendChild(n('path', { d: 'M0,0.5 L8,4 L0,7.5 z', class: cls }));
      defs.appendChild(m);
    }
    svg.appendChild(defs);

    const cy = rowsY(clients.length);
    const hy = rowsY(hosts.length);
    const totals = d.totals || {};
    const peakReq = Math.max(1, ...clients.map(c => c.req_per_min || 0));
    const peakTps = Math.max(1, ...hosts.map(h => h.gen_tps || 0));
    const hostOn = h => h.state === 'ok' && busy(h);
    const labelY = (y, side) => Math.abs(y - gy) < 4 ? y - 15 : Math.round((y + gy) / 2) - 7 + side;

    clients.forEach((c, i) => {
      const on = c.state === 'ok';
      const y = cy[i];
      svg.appendChild(edge(Math.abs(y - gy) < 4 ? `M186,${y} L260,${gy}` : `M186,${y} C224,${y} 222,${gy} 260,${gy}`,
        on, (c.req_per_min || 0) / peakReq));
    });
    hosts.forEach((h, i) => {
      const on = hostOn(h);
      const y = hy[i];
      svg.appendChild(edge(Math.abs(y - gy) < 4 ? `M448,${gy} L542,${y}` : `M448,${gy} C496,${gy} 494,${y} 542,${y}`,
        on, (h.gen_tps || 0) / peakTps));
    });
    clients.forEach((c, i) => {
      const on = c.state === 'ok';
      svg.appendChild(labelGroup(222, labelY(cy[i], 0),
        on ? `${kfmt(c.req_per_min)} req/min` : (c.last_seen_s == null ? 'idle' : ago(c.last_seen_s)), !on));
    });
    hosts.forEach((h, i) => {
      const on = hostOn(h);
      svg.appendChild(labelGroup(495, labelY(hy[i], 0),
        `${kfmt(h.gen_tps)} tok/s · ${h.inflight || 0}`, !on));
    });

    clients.forEach((c, i) => {
      svg.appendChild(boxNode(8, cy[i] - 23, 172, c.label || 'client',
        [[c.label ? 'key ' + c.label : '', c.ip || (c.last_seen_s == null ? 'no requests yet' : '')].filter(Boolean).join(' · ') || '—'],
        c.state === 'ok' ? 'ok' : ''));
    });
    svg.appendChild(boxNode(266, gy - 33, 176, 'Gateway', [
      `${kfmt(totals.req_per_min)} req/min · ${totals.inflight || 0} in flight`,
      `p50 ${totals.p50_ms == null ? '—' : Math.round(totals.p50_ms) + ' ms'} · ${totals.errors_15m || 0} errors`,
    ], 'ok gwn'));
    hosts.forEach((h, i) => {
      svg.appendChild(boxNode(548, hy[i] - 23, 164, h.hostname || '—',
        [[h.provider, h.model || 'idle'].filter(Boolean).join(' · ')],
        busy(h) ? 'ok' : (h.model ? 'dim' : '')));
    });
    return svg;
  }

  // ── tiles ────────────────────────────────────────────────────────────
  function tiles(d) {
    const t = d.totals || {};
    const e = d.energy || {};
    return [
      ['requests', kfmt(t.req_per_min), '/min'],
      ['prompt tokens', kfmt(t.prompt_tps), '/s'],
      ['generated', kfmt(t.gen_tps), 'tok/s'],
      ['latency p50', t.p50_ms == null ? '—' : String(Math.round(t.p50_ms)), 'ms'],
      ['in flight', t.inflight == null ? '—' : String(t.inflight), 'streams'],
      ['serving power', e.serving_w == null ? '—' : String(Math.round(e.serving_w)), 'W'],
      ['energy today', e.kwh_today == null ? '—' : Number(e.kwh_today).toFixed(1),
        `kWh · ${money(e.cost_today)}`],
      ['per 1M tokens', money(e.usd_per_mtok),
        e.cloud_usd_per_mtok == null ? 'vs cloud —' : `vs cloud ${money(e.cloud_usd_per_mtok)}`],
    ];
  }

  function renderTiles(d) {
    const host = $('rtGwTiles');
    if (!host) return;
    host.replaceChildren();
    for (const [k, v, u] of tiles(d)) {
      const box = document.createElement('div');
      box.className = 'rt-gm';
      const kk = document.createElement('span'); kk.className = 'k'; kk.textContent = k;
      const vv = document.createElement('b'); vv.textContent = v;
      const uu = document.createElement('span'); uu.className = 'u'; uu.textContent = u;
      box.append(kk, vv, uu);
      host.appendChild(box);
    }
  }

  // ── card ─────────────────────────────────────────────────────────────
  function renderMeta(d) {
    const meta = $('rtGwMeta');
    if (!meta) return;
    meta.replaceChildren();
    const b = document.createElement('b');
    b.textContent = d.endpoint || '/api/gateway/v1';
    meta.appendChild(b);
    if (!d.enabled) {
      meta.appendChild(document.createTextNode(' · '));
      const off = document.createElement('span');
      off.style.color = 'var(--warn)';
      off.textContent = 'off';
      meta.appendChild(off);
      meta.appendChild(document.createTextNode(` · ${d.keys || 0} API keys`));
      return;
    }
    meta.appendChild(document.createTextNode(
      ` · ${d.keys || 0} API keys · usage probe ${d.usage_probe ? 'on' : 'off'}`));
  }

  function render(d) {
    _last = d;
    const card = $('rtGatewayCard');
    if (!card) return;
    card.hidden = false;
    card.classList.toggle('off', !d.enabled);
    const tg = $('rtGwToggle');
    if (tg) { tg.classList.toggle('on', !!d.enabled); tg.setAttribute('aria-pressed', String(!!d.enabled)); }
    renderMeta(d);
    const wrap = $('rtGwDiagram');
    if (wrap) {
      wrap.replaceChildren(buildSvg(d));
      wrap.querySelectorAll('.rt-node').forEach(gn => {
        const w = Number(gn.querySelector('rect').getAttribute('width')) - 30;
        gn.querySelectorAll('.sb').forEach(t => fitText(t, w));
      });
    }
    renderTiles(d);
  }

  async function refresh() {
    const card = $('rtGatewayCard');
    if (!card) return;
    try {
      const r = await fetch('/api/admin/gateway/flow');
      if (!r.ok) { card.hidden = true; return; }
      const d = await r.json();
      if (!d || d.ok === false) { card.hidden = true; return; }
      render(d);
    } catch (_) {
      // Keep the last good picture; a hard failure hides the card at load time.
      if (!_last) card.hidden = true;
    }
  }

  async function setEnabled(on) {
    try {
      const r = await fetch('/api/admin/gateway', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: on }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) {
        if (typeof _themedToast === 'function') _themedToast(d.error || `HTTP ${r.status}`, { kind: 'err' });
      }
    } catch (e) {
      if (typeof _themedToast === 'function') _themedToast('gateway toggle failed', { kind: 'err' });
    }
    refresh();
  }

  function wire() {
    if (_wired) return;
    const tg = $('rtGwToggle');
    const head = $('rtGwHead');
    if (!tg && !head) return;
    _wired = true;
    if (tg) {
      tg.addEventListener('click', ev => {
        ev.stopPropagation();
        setEnabled(!tg.classList.contains('on'));
      });
    }
    if (head) {
      head.addEventListener('click', () => {
        const card = $('rtGatewayCard');
        if (card) card.classList.toggle('collapsed');
      });
    }
  }

  function start() {
    wire();
    refresh();
    if (_timer) return;
    _timer = setInterval(refresh, POLL_MS);
  }
  function stop() {
    if (!_timer) return;
    clearInterval(_timer);
    _timer = null;
  }

  window.GatewayView = { start, stop, refresh, render, tiles, buildSvg, setEnabled,
    last: () => _last, POLL_MS };
})();
