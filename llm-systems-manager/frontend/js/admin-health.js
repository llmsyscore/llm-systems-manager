// Admin → System Health card (#797): services column, live data-flow diagram
// with a per-node detail strip, and the ordered warnings column.
(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const SVG_RESTART = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13.2 8.6A5.3 5.3 0 1 1 11.8 4.2"/><path d="M12 1.8v3h-3"/></svg>';
  const NODE_NAME = { agents: 'Agents', manager: 'Manager', browsers: 'Browsers', ae: 'Alarm Engine', influx: 'InfluxDB' };
  const MARKER = { ok: 'url(#hcAhOk)', warn: 'url(#hcAhWarn)', crit: 'url(#hcAhCrit)', off: 'url(#hcAhOff)' };
  const EDGE_IDS = ['eAgMg', 'eAgAe', 'eMgAe', 'eMgBr', 'eAeIn'];
  const WORST = { ok: 0, off: 0, warn: 1, crit: 2 };
  // Which edges each node's dot/border follows (worst incident edge wins).
  const NODE_EDGES = {
    nAg: ['eAgMg', 'eAgAe'], nMg: ['eAgMg', 'eMgAe', 'eMgBr'],
    nBr: ['eMgBr'], nAe: ['eAgAe', 'eMgAe', 'eAeIn'], nIn: ['eAeIn'],
  };

  let _selNode = null;
  let _last = null;

  // ── formatting ───────────────────────────────────────────────────────
  // Shrinks, then ellipsises, an SVG text run so it stays inside its box.
  function fitText(t, maxW) {
    if (!t || typeof t.getComputedTextLength !== 'function') return;
    const fits = () => { try { return t.getComputedTextLength() <= maxW; } catch (_) { return true; } };
    let size = 9.5;
    while (!fits() && size > 8) { size -= 0.5; t.style.fontSize = size + 'px'; }
    let s = t.textContent;
    while (!fits() && s.length > 4) { s = s.slice(0, -2); t.textContent = s + '…'; }
  }

  function num(n) {
    if (n == null || isNaN(n)) return null;
    const v = Number(n);
    return v >= 10 ? String(Math.round(v)) : String(parseFloat(v.toFixed(1)));
  }
  // Two-unit uptime: "3d 4h" / "6h 12m" / "45m" / "12s".
  function upStr(s) {
    if (s == null || isNaN(s)) return null;
    const t = Math.max(0, Math.floor(Number(s)));
    if (t >= 86400) return `${Math.floor(t / 86400)}d ${Math.floor((t % 86400) / 3600)}h`;
    if (t >= 3600) return `${Math.floor(t / 3600)}h ${Math.floor((t % 3600) / 60)}m`;
    if (t >= 60) return `${Math.floor(t / 60)}m`;
    return `${t}s`;
  }
  function svcOf(d, name) {
    return ((d && d.services) || []).find(s => s && s.name === name) || null;
  }
  function ms(v) { return v == null ? '—' : `${Math.round(v)} ms`; }
  function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

  // ── services column ──────────────────────────────────────────────────
  // One row per service: dot, name (+ link chip), version, uptime, restart.
  function svcRows(d) {
    const mgr = (d && d.manager) || {};
    const ae = svcOf(d, 'alarm_engine');
    const influx = svcOf(d, 'influxdb');
    const rows = [{
      st: 'ok', n: 'Manager', lk: null, ver: mgr.version || '—',
      up: upStr(mgr.uptime_s), upTxt: 'unknown', upCls: upStr(mgr.uptime_s) ? '' : 'crit',
      act: 'Restart Manager', svc: 'manager',
    }];

    const aeOk = !!(ae && ae.ok);
    const tls = (ae && ae.tls && typeof ae.tls === 'object') ? ae.tls : null;
    let lk = null;
    if (tls) {
      if (tls.enabled && tls.active) lk = ['https', 'on'];
      else if (tls.enabled) lk = ['cert missing', 'crit'];
      else lk = ['http', 'off'];
    }
    // #764: the AE restart is always offered; the tip names how it restarts.
    const via = ((d && d.ae_restart) || {}).via;
    rows.push({
      st: aeOk ? 'ok' : 'crit', n: 'Alarm Engine', lk, ver: (ae && ae.version) || '—',
      up: aeOk ? upStr(ae && ae.uptime_s) : null,
      upTxt: aeOk ? 'connected' : 'unreachable', upCls: aeOk ? '' : 'crit',
      act: 'Restart Alarm Engine' + (via === 'self-restart' ? ' · via its self-restart API' : ''),
      svc: 'alarm_engine',
    });

    const inOk = !!(influx && influx.ok);
    rows.push({
      st: inOk ? 'ok' : 'crit', n: 'InfluxDB', lk: null, ver: (influx && influx.version) || '—',
      up: null, upTxt: (influx && influx.state) || 'unknown', upCls: inOk ? '' : 'crit',
      act: null, svc: null,
    });
    return rows;
  }

  function svcRowHtml(s) {
    const lk = s.lk
      ? `<span class="lk ${s.lk[1] === 'crit' ? 'crit' : (s.lk[1] === 'on' ? '' : 'off')}">${esc(s.lk[0])}</span>` : '';
    const up = s.up ? `<span class="l">up</span>${esc(s.up)}` : esc(s.upTxt);
    const btn = s.act
      ? `<button type="button" class="ib warnh" data-restart-svc="${esc(s.svc)}" data-tip="${esc(s.act)}">${SVG_RESTART}</button>`
      : '<span class="ib none"></span>';
    return `<div class="hc-svcr"><span class="dot ${s.st}"></span>`
      + `<div class="n"><span class="nt">${esc(s.n)}</span>${lk}</div><div class="v">${esc(s.ver)}</div>`
      + `<div class="up ${s.upCls || ''}">${up}</div>${btn}</div>`;
  }

  // ── data-flow edges ──────────────────────────────────────────────────
  // Each edge: {state, label, rate 0..1}. Missing counters degrade to a
  // label of "—" rather than inventing a number.
  function edgeStates(d) {
    const flow = (d && d.flow) || {};
    const df = (d && d.data_flow) || {};
    const mgr = (d && d.manager) || {};
    const streams = mgr.streams || {};
    const ae = svcOf(d, 'alarm_engine');
    const influx = svcOf(d, 'influxdb');
    const aeOk = !!(ae && ae.ok);
    const inOk = !!(influx && influx.ok);

    const pushes = ['primary_llama_push', 'primary_lms_push', 'primary_vllm_push'].map(k => df[k] || {});
    const expected = pushes.some(p => p.has_agent);
    const stale = pushes.some(p => p.has_agent && p.ok === false);

    const out = {};
    const mk = (state, label, rate) => ({ state, label, rate: rate == null ? 0 : Math.max(0, Math.min(1, rate)) });

    const push = flow.agent_pushes_per_s;
    out.eAgMg = !expected ? mk('off', '—', 0)
      : stale ? mk('warn', push != null ? `${num(push)} push/s` : 'stale', push != null ? push / 5 : 0.1)
      : push == null ? mk('ok', '—', 0.3)
      : push === 0 ? mk('crit', 'no pushes', 0)
      : mk('ok', `${num(push)} push/s`, push / 5);

    const ing = flow.ae_ingest_points_per_s;
    out.eAgAe = !aeOk ? mk('crit', 'no data', 0)
      : !expected ? mk('off', '—', 0)
      : ing == null ? mk('ok', '—', 0.3)
      : ing === 0 ? mk('crit', 'no data', 0)
      : mk('ok', `${num(ing)} metrics/s`, ing / 60);

    const hist = flow.history_req_per_s;
    out.eMgAe = !aeOk ? mk('crit', 'timeout', 0)
      : hist == null ? mk('ok', '—', 0.15)
      : mk('ok', `history ${num(hist)}/s`, hist / 2);

    const act = streams.active, lim = streams.limit;
    const near = lim ? act / lim : 0;
    out.eMgBr = act == null ? mk('off', '—', 0)
      : mk(lim && near >= 0.9 ? 'warn' : 'ok', `${act} ${act === 1 ? 'stream' : 'streams'}`, near);

    const wr = flow.influx_writes_per_s;
    out.eAeIn = (!aeOk || !inOk) ? mk('off', '—', 0)
      : wr == null ? mk('ok', '—', 0.3)
      : mk('ok', `${num(wr)} writes/s`, wr / 60);

    return out;
  }

  // Node sub-labels; the dot/border comes from the worst incident edge.
  function nodeSubs(d) {
    const mgr = (d && d.manager) || {};
    const conns = mgr.connections || {};
    const agents = (d && d.agents) || [];
    const approved = agents.filter(a => a.status === 'approved');
    const live = approved.filter(a => a.liveness === 'live').length;
    const ae = svcOf(d, 'alarm_engine');
    const influx = svcOf(d, 'influxdb');
    const ing = ((d && d.flow) || {}).ae_ingest_points_per_s;
    const tabs = conns.browsers;
    const phones = mgr.push_subscriptions;
    let br = '—';
    if (tabs != null) {
      br = plural(tabs, 'tab', 'tabs');
      if (phones) br += ` · ${plural(phones, 'phone', 'phones')}`;
    }
    return {
      nAg: approved.length ? `${live} / ${approved.length} live` : 'none registered',
      nMg: 'this host',
      nBr: br,
      nAe: (ae && ae.ok) ? (ing != null ? `${num(ing)} points/s` : 'connected') : 'unreachable',
      nIn: (influx && influx.state) || 'unknown',
    };
  }

  // ── node detail strip ────────────────────────────────────────────────
  function detailRows(d, node) {
    const mgr = (d && d.manager) || {};
    const streams = mgr.streams || {};
    const conns = mgr.connections || {};
    const flow = (d && d.flow) || {};
    const df = (d && d.data_flow) || {};
    const ae = svcOf(d, 'alarm_engine');
    const influx = svcOf(d, 'influxdb');
    const agents = (d && d.agents) || [];
    const approved = agents.filter(a => a.status === 'approved');
    const dash = v => (v == null || v === '' ? '—' : String(v));

    if (node === 'agents') {
      const live = approved.filter(a => a.liveness === 'live').length;
      const stale = approved.filter(a => a.liveness === 'stale').length;
      const down = approved.filter(a => a.liveness === 'down');
      const bothTls = approved.filter(a => a.tls_direction === 'both').length;
      const age = k => {
        const p = df[k] || {};
        if (!p.has_agent) return 'no agent';
        return p.age_s == null ? 'no push' : `${Math.round(p.age_s)} s`;
      };
      const hb = typeof _adminCollectInterval !== 'undefined' && _adminCollectInterval
        ? `${_adminCollectInterval} s` : '—';
      return [
        ['live', `${live} / ${approved.length}`, live === approved.length && approved.length ? 'ok' : ''],
        ['stale', String(stale), stale ? 'warn' : ''],
        ['down', down.length ? `${down.length} · ${down.map(a => a.hostname || a.id).join(', ')}` : '0', down.length ? 'crit' : ''],
        ['heartbeat every', hb],
        ['pushes', `llama.cpp ${age('primary_llama_push')} · LM Studio ${age('primary_lms_push')} · vLLM ${age('primary_vllm_push')}`],
        ['metrics to engine', flow.ae_ingest_points_per_s != null ? `${num(flow.ae_ingest_points_per_s)} /s` : '—',
          flow.ae_ingest_points_per_s === 0 ? 'crit' : ''],
        ['TLS both ways', `${bothTls} of ${approved.length}`, bothTls === approved.length && approved.length ? 'ok' : ''],
      ];
    }
    if (node === 'manager') {
      const near = streams.limit ? streams.active / streams.limit : 0;
      const busy = conns.worker_threads_busy, total = conns.worker_threads;
      return [
        ['version', dash(mgr.version)],
        ['up', dash(upStr(mgr.uptime_s))],
        ['streams', streams.limit != null
          ? `${streams.active} / ${streams.limit} · peak ${streams.peak} · refusals ${streams.refusals}` : '—',
          near >= 0.9 || streams.refusals ? 'warn' : ''],
        ['browsers', nodeSubs(d).nBr],
        ['agent links', dash(conns.agents)],
        ['worker threads', total != null ? `${busy} / ${total} busy` : '—',
          total && busy / total >= 0.9 ? 'warn' : ''],
        ['engine probe', ae && ae.ok ? ms(ae.latency_ms) : 'timeout', ae && ae.ok ? '' : 'crit'],
      ];
    }
    if (node === 'browsers') {
      return [
        ['dashboard tabs', dash(conns.browsers)],
        ['companion', mgr.push_subscriptions ? plural(mgr.push_subscriptions, 'phone', 'phones') : '0'],
        ['live streams', dash(streams.active)],
        ['push subscriptions', dash(mgr.push_subscriptions)],
        ['ws relay', dash(mgr.ws_relay)],
      ];
    }
    if (node === 'ae') {
      if (!ae || !ae.ok) {
        const t = (ae && ae.tls && typeof ae.tls === 'object') ? ae.tls : null;
        return [
          ['last good probe', dash(ae && ae.last_ok_at), 'crit'],
          ['failed probes', dash(ae && ae.consecutive_failures), 'crit'],
          ['serving', !t ? dash(ae && ae.url)
            : (t.enabled && !t.active ? 'cert missing → http' : (t.enabled ? 'https' : 'http')),
            t && t.enabled && !t.active ? 'crit' : ''],
          ['error', dash(ae && ae.error), 'crit'],
          ['ingest', 'unknown'],
          ['rules eval', '—'],
          ['active alerts', '—'],
        ];
      }
      const tls = (ae.tls && typeof ae.tls === 'object') ? ae.tls : null;
      const serving = !tls ? 'unknown'
        : (tls.enabled && tls.active) ? 'https' : (tls.enabled ? 'cert missing → http' : 'http');
      return [
        ['version', dash(ae.version)],
        ['up', dash(upStr(ae.uptime_s))],
        ['serving', serving, serving === 'https' ? 'ok' : (serving.indexOf('cert') === 0 ? 'crit' : '')],
        ['ingest', ae.ingest_points_per_s != null ? `${num(ae.ingest_points_per_s)} points/s` : '—'],
        ['rules eval', ae.rule_eval_ms != null ? ms(ae.rule_eval_ms) : '—'],
        ['active alerts', dash(ae.active_alerts), ae.active_alerts ? 'warn' : ''],
        ['probe from manager', ms(ae.latency_ms)],
      ];
    }
    const inOk = !!(influx && influx.ok);
    if (!inOk) {
      return [
        ['state', dash(influx && influx.state) === '—' ? 'unknown' : influx.state, 'crit'],
        ['reached via', dash(influx && influx.via) === '—' ? 'alarm engine' : influx.via, 'crit'],
      ];
    }
    return [
      ['version', dash(influx.version)],
      ['state', dash(influx.state), 'ok'],
      ['ping', ms(influx.ping_ms)],
      ['writes', influx.writes_per_s != null ? `${num(influx.writes_per_s)} /s` : '—'],
      ['reached via', dash(influx.via) === '—' ? 'alarm engine' : influx.via],
    ];
  }

  // ── warnings column ──────────────────────────────────────────────────
  const CRIT_RE = /down|unreachable|stale|EXPIRED/;

  // Ordered crit → warn → note → info; empty renders the mono "None".
  function warnRows(d, rel) {
    const rows = [];
    for (const w of ((d && d.warnings) || [])) {
      const crit = CRIT_RE.test(String(w));
      rows.push({ k: crit ? 'crit' : 'warn', g: crit ? '▲' : '!', t: esc(w) });
    }
    if (rel && rel.enabled && rel.update_available === true) {
      const url = `https://github.com/${rel.repo || ''}/releases/latest`;
      rows.push({ k: 'note', g: '↑',
        t: `Manager <span class="m">${esc(rel.latest || '')}</span> is available`
          + (rel.installed ? ` <span class="m">(installed ${esc(rel.installed)})</span>` : '')
          + ` · <a href="${esc(url)}" target="_blank" rel="noopener">release notes</a>` });
    }
    const au = (d && d.agent_update) || {};
    if (au.outdated) {
      rows.push({ k: 'note', g: '↑',
        t: `${au.outdated} agent${au.outdated === 1 ? '' : 's'} can update to `
          + `<span class="m">${esc(au.latest || '')}</span> · `
          + '<button type="button" class="lnk" data-act="updateall">Update all</button>' });
    }
    const info = releaseInfoText(rel);
    if (info) rows.push({ k: 'info', g: 'i', t: esc(info) });
    const rank = { crit: 0, warn: 1, note: 2, info: 3 };
    return rows.sort((a, b) => rank[a.k] - rank[b.k]);
  }

  // Same verdict logic as _adminReleaseInfoHtml; returns the message or ''.
  function releaseInfoText(rel) {
    if (typeof _adminReleaseInfoText === 'function') return _adminReleaseInfoText(rel);
    return '';
  }

  function pillOf(d, rows) {
    if ((d && d.overall) === 'down') return ['crit', 'Down'];
    return rows.some(r => r.k === 'crit') ? ['crit', 'Down']
      : rows.some(r => r.k === 'warn') ? ['warn', 'Attention'] : ['ok', 'Healthy'];
  }

  // ── render ───────────────────────────────────────────────────────────
  function renderDetail() {
    const host = $('adminHealthDetail');
    if (!host) return;
    const svg = $('adminHealthDataFlow');
    if (svg) svg.querySelectorAll('.hc-node').forEach(n => n.classList.toggle('sel', n.dataset.node === _selNode));
    if (!_selNode || !_last) { host.innerHTML = ''; return; }
    const rows = detailRows(_last, _selNode)
      .map(([k, v, c]) => `<dt>${esc(k)}</dt><dd class="${c || ''}">${esc(v)}</dd>`).join('');
    host.innerHTML = `<div class="hc-det-h"><span class="microlbl">${esc(NODE_NAME[_selNode])}</span>`
      + '<button type="button" class="ib" data-close aria-label="Close">✕</button></div>'
      + `<dl class="hc-kv">${rows}</dl>`;
  }

  function render(d, rel) {
    _last = d || {};
    const rows = warnRows(_last, rel);

    const pill = $('adminHealthOverall');
    if (pill) {
      const [cls, txt] = pillOf(_last, rows);
      pill.className = 'pill ' + cls;
      pill.textContent = txt;
    }

    const svcEl = $('adminHealthServices');
    if (svcEl) {
      svcEl.innerHTML = svcRows(_last).map(svcRowHtml).join('');
      if (!svcEl._hcBound) {
        svcEl._hcBound = true;
        svcEl.addEventListener('click', e => {
          const btn = e.target.closest('[data-restart-svc]');
          if (btn && typeof _restartService === 'function') _restartService(btn.getAttribute('data-restart-svc'));
        });
      }
    }

    const svg = $('adminHealthDataFlow');
    if (svg) {
      const edges = edgeStates(_last);
      const subs = nodeSubs(_last);
      for (const id of EDGE_IDS) {
        const e = svg.querySelector('#hc' + id);
        const l = svg.querySelector('#hcl' + id.slice(1));
        const st = edges[id];
        if (e) {
          e.setAttribute('class', 'hc-e ' + st.state);
          e.setAttribute('marker-end', MARKER[st.state]);
          e.style.setProperty('--dur', (2.2 - 1.7 * st.rate).toFixed(2) + 's');
          e.style.setProperty('--w', (1.2 + 1.3 * st.rate).toFixed(2));
          e.style.setProperty('--op', (0.55 + 0.45 * st.rate).toFixed(2));
        }
        if (l) {
          l.setAttribute('class', 'hc-el ' + (st.state === 'ok' ? '' : st.state));
          const t = l.querySelector('text');
          if (t) t.textContent = st.label;
        }
      }
      for (const [nid, eids] of Object.entries(NODE_EDGES)) {
        const n = svg.querySelector('#hc' + nid);
        if (!n) continue;
        let worst = 'ok';
        for (const eid of eids) if (WORST[edges[eid].state] > WORST[worst]) worst = edges[eid].state;
        n.setAttribute('class', 'hc-node ' + worst + (n.dataset.node === _selNode ? ' sel' : ''));
        const sb = n.querySelector('.sb');
        if (sb) {
          sb.textContent = subs[nid]; sb.style.fontSize = '';
          sb.setAttribute('class', 'sb ' + (worst === 'ok' ? '' : worst));
          fitText(sb, Number(n.querySelector('rect').getAttribute('width')) - 30);
        }
      }
      if (!svg._hcBound) {
        svg._hcBound = true;
        const toggleNode = n => { _selNode = (_selNode === n.dataset.node) ? null : n.dataset.node; renderDetail(); };
        svg.addEventListener('click', ev => {
          const n = ev.target.closest('.hc-node');
          if (n) toggleNode(n);
        });
        svg.addEventListener('keydown', ev => {
          if (ev.key !== 'Enter' && ev.key !== ' ') return;
          const n = ev.target.closest('.hc-node');
          if (!n) return;
          ev.preventDefault();
          toggleNode(n);
        });
      }
    }

    const warnEl = $('adminHealthWarnings');
    if (warnEl) {
      warnEl.innerHTML = rows.length
        ? rows.map(r => `<div class="w ${r.k}"><span class="g">${r.g}</span><div>${r.t}</div></div>`).join('')
        : '<div class="w-none">None</div>';
      if (!warnEl._hcBound) {
        warnEl._hcBound = true;
        warnEl.addEventListener('click', e => {
          if (e.target.closest('[data-act="updateall"]') && typeof adminUpdateAll === 'function') adminUpdateAll();
        });
      }
    }

    const det = $('adminHealthDetail');
    if (det && !det._hcBound) {
      det._hcBound = true;
      det.addEventListener('click', ev => {
        if (ev.target.closest('[data-close]')) { _selNode = null; renderDetail(); }
      });
    }
    renderDetail();
  }

  window.HealthView = {
    render, svcRows, svcRowHtml, edgeStates, nodeSubs, detailRows, warnRows, pillOf, upStr, num,
    select: n => { _selNode = n; renderDetail(); },
    selected: () => _selNode,
  };
})();
