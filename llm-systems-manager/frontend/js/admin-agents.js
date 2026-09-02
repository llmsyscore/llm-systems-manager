// Admin → Agents (#793): roster header, Manage menu, state-dot rows and the row
// drawer. Reads admin.js registry state and calls its action functions.
(() => {
  'use strict';

  const FRESH_MS = 30000;
  const esc = s => (typeof adminEsc === 'function' ? adminEsc(s) : String(s == null ? '' : s));
  const call = (name, ...args) => {
    const fn = window[name];
    return typeof fn === 'function' ? fn(...args) : undefined;
  };

  // ── pure helpers (exported on window.AgentsView for tests) ────────────────
  function ipOf(a) {
    if (typeof window._adminAgentIP === 'function') return window._adminAgentIP(a);
    const m = (a.bind_url || '').match(/^https?:\/\/([^:/]+)/);
    return (m && m[1]) || a.registered_from || '—';
  }
  // One dot per row: pending / disabled / down / stale / paused / live.
  function rowState(a) {
    const status = a.status || 'unknown';
    if (status === 'pending') return 'pending';
    if (status === 'disabled') return 'disabled';
    if (status !== 'approved') return 'unknown';
    const live = a.liveness || 'unknown';
    if (live === 'down') return 'down';
    if (live === 'stale') return 'stale';
    const hb = a.last_heartbeat_data || {};
    if (hb.collection_enabled === false) return 'paused';
    return live === 'live' ? 'live' : 'unknown';
  }
  const DOT_TITLE = {
    live: 'live · collecting', stale: 'stale — last heartbeat over the grace window',
    down: 'down — no heartbeat', paused: 'collection paused', pending: 'pending approval',
    disabled: 'disabled', unknown: 'state unknown',
  };
  // TLS as one glyph chip: ⇄ mutual, → manager→agent only, ← agent→manager only, ○ http.
  function tlsInfo(a) {
    const m2a = (a.bind_url || '').startsWith('https://');
    const a2m = !!(a.last_heartbeat_data && a.last_heartbeat_data.control_channel_tls);
    const issued = a.last_cert_issued_at ? String(a.last_cert_issued_at).slice(0, 10) : '';
    if (m2a && a2m) return { mode: 'mutual', glyph: '⇄', cls: 'tls', label: 'tls', issued,
      title: 'Mutual TLS — both directions encrypted' + (issued ? ' · cert issued ' + issued : '') };
    if (m2a) return { mode: 'in', glyph: '→', cls: 'tls one', label: 'tls', issued,
      title: 'TLS manager → agent only; control channel is plain' };
    if (a2m) return { mode: 'out', glyph: '←', cls: 'tls one', label: 'tls', issued,
      title: 'TLS agent → manager only; the manager dials this agent over HTTP' };
    let title = 'Plain HTTP both ways';
    if (a.status === 'pending') title = 'Registered over HTTP — a cert is issued on approval';
    else if (issued) title = `Cert issued ${issued} — the agent has not restarted to bind HTTPS yet`;
    else if (a.status === 'approved') title = 'No cert issued yet — distribution happens on the next heartbeat';
    return { mode: 'http', glyph: '○', cls: 'tls off', label: 'http', issued, title };
  }
  // Description shows only when it adds information beyond the hostname.
  function showDesc(a) {
    const d = String(a.description || '').trim();
    if (!d) return false;
    const host = String(a.hostname || '').trim();
    if (!host) return true;
    const dl = d.toLowerCase();
    if (dl === host.toLowerCase()) return false;
    const os = String(a.os || '').trim().toLowerCase();
    return !(os && dl === `${host.toLowerCase()} (${os})`);
  }
  function fmtAgo(iso, nowMs) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return '—';
    const s = Math.max(0, Math.round(((nowMs == null ? Date.now() : nowMs) - t) / 1000));
    if (s < 60) return `${s} s ago`;
    if (s < 3600) { const r = s % 60; return `${Math.floor(s / 60)} m${r ? ' ' + r + ' s' : ''} ago`; }
    if (s < 86400) { const r = Math.floor((s % 3600) / 60); return `${Math.floor(s / 3600)} h${r ? ' ' + r + ' m' : ''} ago`; }
    return `${Math.floor(s / 86400)} d ago`;
  }
  function clock(d) {
    let h = d.getHours();
    const ap = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    const p2 = n => String(n).padStart(2, '0');
    return `${h}:${p2(d.getMinutes())}:${p2(d.getSeconds())} ${ap}`;
  }
  function fingerprintShort(fp) {
    const s = String(fp || '').replace(/^sha256:/i, '').replace(/[^0-9a-f]/gi, '');
    if (!s) return '—';
    if (s.length <= 12) return 'sha256:' + s;
    return `sha256:${s.slice(0, 4)} ${s.slice(4, 8)} … ${s.slice(-4)}`;
  }
  // Filter grammar: free text plus needs:update, state:<dot>, cap:<name>.
  function parseFilter(q) {
    const f = { text: [], needs: [], state: [], cap: [] };
    String(q || '').toLowerCase().split(/\s+/).filter(Boolean).forEach(tok => {
      const m = tok.match(/^(needs|state|cap):(.+)$/);
      if (m) f[m[1]].push(m[2]);
      else f.text.push(tok);
    });
    return f;
  }
  const INFRA_LABELS = { manager: 'manager', alarm_engine: 'alarm engine', influxdb: 'influxdb' };
  function infraList(a) {
    return (Array.isArray(a.colocated_infra) ? a.colocated_infra : [])
      .map(svc => ({ label: INFRA_LABELS[svc.role] || String(svc.role || ''), version: svc.version || '' }));
  }
  function enabledCaps(a) {
    const c = a.capabilities || {};
    return Object.keys(c).filter(k => c[k]);
  }
  function matches(a, f) {
    if (f.needs.some(n => n === 'update' && !a.update_available)) return false;
    if (f.state.length && !f.state.includes(rowState(a))) return false;
    const caps = enabledCaps(a).map(k => k.toLowerCase());
    if (f.cap.some(c => !caps.includes(c))) return false;
    if (!f.text.length) return true;
    const hay = [a.hostname, a.description, ipOf(a), a.registered_from, a.os, a.role, a.version,
      a.agent_id, ...caps].map(v => String(v || '').toLowerCase()).join(' ');
    return f.text.every(t => hay.includes(t));
  }
  function summary(agents) {
    const list = agents || [];
    return {
      total: list.length,
      live: list.filter(a => a.status === 'approved' && a.liveness === 'live').length,
      pending: list.filter(a => a.status === 'pending').length,
      needsUpdate: list.filter(a => a.update_available).length,
    };
  }
  // ↻ colour: green = fresh, amber = last refresh failed or stale, red = unreachable.
  function stampState(st, nowMs) {
    if (st.unreachable) return 'crit';
    if (st.failed || !st.lastOkAt) return 'warn';
    return ((nowMs == null ? Date.now() : nowMs) - st.lastOkAt) > FRESH_MS ? 'warn' : 'ok';
  }

  // ── state ────────────────────────────────────────────────────────────────
  // admin.js declares its state with top-level `let`, so read it by name, not via window.
  /* eslint-disable no-undef */
  const ctx = () => ({
    agents: (typeof _adminAgentsCache !== 'undefined' && _adminAgentsCache) || [],
    global: (typeof _adminGlobal !== 'undefined' && _adminGlobal) || {},
    providers: (typeof _adminProviders !== 'undefined' && _adminProviders) || [],
    poolProviders: (typeof _adminPoolProviders !== 'undefined' && _adminPoolProviders) || [],
    hostAuto: typeof _adminHostAutoDetected !== 'undefined' && !!_adminHostAutoDetected,
    latest: (typeof _latestAgentVersion !== 'undefined' && _latestAgentVersion) || null,
    managerVersion: (typeof _adminManagerVersion !== 'undefined' && _adminManagerVersion) || null,
    interval: (typeof _adminCollectInterval !== 'undefined' && _adminCollectInterval) || null,
  });
  /* eslint-enable no-undef */
  const openIds = new Set();
  let sort = { key: 'caps', dir: 1 };
  let filterQ = '';
  let openMenu = null;
  const st = { lastOkAt: 0, failed: false, unreachable: false, busy: false };
  let ticker = null;

  const singleShow = (aid, holder, hidden) => (typeof window._singleSelectShow === 'function'
    ? window._singleSelectShow(aid, holder, hidden)
    : (!hidden && (!holder || holder === aid)));

  // ── markup ───────────────────────────────────────────────────────────────
  const ICON = {
    pause: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5.5 3v10M10.5 3v10" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    resume: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3l8 5-8 5z" fill="currentColor"/></svg>',
    restart: '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M13.3 8.6A5.4 5.4 0 1 1 11.8 4"/><path d="M13.4 2.6v3.3h-3.3"/></svg>',
    menu: '<svg viewBox="0 0 16 16" aria-hidden="true" fill="currentColor"><circle cx="8" cy="3.5" r="1.4"/><circle cx="8" cy="8" r="1.4"/><circle cx="8" cy="12.5" r="1.4"/></svg>',
  };
  function toggleHtml(on, label, attrs, hint) {
    return `<button type="button" class="mc-toggle${on ? ' on' : ''}" ${attrs} aria-pressed="${on ? 'true' : 'false'}">`
      + `<span class="track"></span><span class="tlbl">${label}</span>${hint ? `<span class="hint">${hint}</span>` : ''}</button>`;
  }
  function capsHtml(a) {
    const c = ctx();
    const caps = a.capabilities || {};
    const aid = esc(a.agent_id);
    const out = [];
    const provKeys = new Set();
    for (const p of c.providers) {
      provKeys.add(p.capability_key);
      if (!caps[p.capability_key]) continue;
      const isP = c.global['primary_' + p.name + '_id'] === a.agent_id;
      const idx = ((c.global[p.name + '_pool']) || []).indexOf(a.agent_id);
      const bits = [`<span>${esc(p.name)}</span>`];
      if (isP) bits.push('<span class="star">★</span>');
      if (idx >= 0) bits.push(`<span class="sep">·</span><span class="slot">pool #${idx + 1}</span>`);
      const title = [esc(p.label || p.name), isP ? 'primary' : '', idx >= 0 ? `pool slot ${idx + 1}` : '']
        .filter(Boolean).join(' — ');
      const open = a.status === 'approved' ? ` data-act="open" data-prov="${esc(p.name)}" data-aid="${aid}" role="button" tabindex="0"` : '';
      out.push(`<span class="ag-cap prov"${open} title="${title}">${bits.join('')}</span>`);
    }
    const quiet = ['openclaw', 'image_gen', 'perf_controller', 'sysperf'];
    const rest = enabledCaps(a).filter(k => !provKeys.has(k))
      .sort((x, y) => (quiet.indexOf(x) + 1 || 99) - (quiet.indexOf(y) + 1 || 99) || x.localeCompare(y));
    rest.forEach(k => out.push(`<span class="ag-cap q">${esc(k)}</span>`));
    const infra = infraList(a);
    if (infra.length) {
      const title = 'Co-located on this host: ' + infra.map(i => i.label + (i.version ? ' ' + i.version : '')).join(' · ');
      const more = infra.length > 1 ? ` <span class="more">+${infra.length - 1}</span>` : '';
      out.push(`<span class="ag-cap infra" title="${esc(title)}">⛬ ${esc(infra[0].label)}${more}</span>`);
    }
    const t = tlsInfo(a);
    out.push(`<span class="ag-cap ${t.cls}" title="${esc(t.title)}">${t.glyph} ${t.label}</span>`);
    return out.join('');
  }
  function rolesHtml(a) {
    const c = ctx();
    if (a.status !== 'approved') return '<span class="none">Approve the agent to assign roles</span>';
    const caps = a.capabilities || {};
    const aid = esc(a.agent_id);
    const rows = [];
    for (const p of c.providers) {
      if (!caps[p.capability_key]) continue;
      const holder = c.global['primary_' + p.name + '_id'];
      if (!singleShow(a.agent_id, holder, false)) continue;
      rows.push(toggleHtml(holder === a.agent_id, `Primary ${esc(p.name)}`,
        `data-act="primary" data-prov="${esc(p.name)}" data-aid="${aid}"`));
    }
    for (const p of c.poolProviders) {
      if (!caps[p.name]) continue;
      const idx = ((c.global[p.name + '_pool']) || []).indexOf(a.agent_id);
      rows.push(toggleHtml(idx >= 0, `In ${esc(p.name)} pool`,
        `data-act="pool" data-prov="${esc(p.name)}" data-aid="${aid}"`, idx >= 0 ? `slot #${idx + 1}` : ''));
    }
    if (singleShow(a.agent_id, c.global.host_agent_id, c.hostAuto)) {
      rows.push(toggleHtml(!!a.is_host_agent, 'Manager host', `data-act="host" data-aid="${aid}"`));
    }
    return rows.length ? rows.join('') : '<span class="none">No roles available</span>';
  }
  function connectionHtml(a) {
    const c = ctx();
    const t = tlsInfo(a);
    let tls, tlsCls = '';
    if (t.mode === 'mutual') { tls = 'mutual' + (t.issued ? ` · cert issued ${t.issued}` : ''); tlsCls = 'ok'; }
    else if (t.mode === 'in') tls = 'manager → agent only' + (t.issued ? ` · cert issued ${t.issued}` : '');
    else if (t.mode === 'out') tls = 'agent → manager only';
    else { tls = 'http' + (t.issued ? ` · cert issued ${t.issued}, restart pending` : ''); tlsCls = 'warn'; }
    const hb = a.last_heartbeat_data || {};
    let collector = '—';
    if (a.status === 'approved') {
      if (hb.collection_enabled === false) collector = 'paused';
      else if (hb.collection_enabled === true) collector = (c.interval ? `every ${c.interval} s` : 'on') + (a.role ? ` · ${a.role}` : '');
    }
    const seenCls = a.liveness === 'down' ? 'crit' : a.liveness === 'stale' ? 'warn' : '';
    const kv = [
      ['last seen', a.last_heartbeat ? `<span class="ag-seen" data-seen="${esc(a.last_heartbeat)}">${fmtAgo(a.last_heartbeat)}</span>` : '—', seenCls, true],
      ['bind', a.bind_url || '—', ''],
      ['tls', tls, tlsCls],
      ['registered from', a.registered_from || '—', ''],
      ['fingerprint', fingerprintShort(a.fingerprint), ''],
      ['runs as', a.agent_user || '—', ''],
      ['collector', collector, ''],
    ];
    return kv.map(([k, v, cls, raw]) => `<dt>${esc(k)}</dt><dd${cls ? ` class="${cls}"` : ''}${raw ? '' : ` title="${esc(v)}"`}>${raw ? v : esc(v)}</dd>`).join('');
  }
  function shortcutsHtml(a) {
    const c = ctx();
    const aid = esc(a.agent_id);
    const caps = a.capabilities || {};
    const out = [];
    if (a.status === 'approved') {
      c.providers.filter(p => caps[p.capability_key]).forEach(p => {
        out.push(`<button type="button" class="ag-lnk" data-act="open" data-prov="${esc(p.name)}" data-aid="${aid}"><span class="mi">⧉</span>Open ${esc(p.label || p.name)} control</button>`);
      });
      out.push(`<button type="button" class="ag-lnk" data-act="log" data-aid="${aid}"><span class="mi">≣</span>Stream agent log</button>`);
      out.push(`<button type="button" class="ag-lnk" data-act="ping" data-aid="${aid}"><span class="mi">⟁</span>Ping now</button>`);
      out.push(`<button type="button" class="ag-lnk" data-act="config" data-aid="${aid}"><span class="mi">✎</span>Edit agent config</button>`);
    }
    return out.join('');
  }
  function infraHtml(a) {
    const infra = infraList(a);
    if (!infra.length) return '';
    const rows = infra.map(i => `<dt>${esc(i.label)}</dt><dd title="${esc(i.version || 'version unknown')}">${esc(i.version || '—')}</dd>`).join('');
    return `<span class="microlbl sub">Co-located services</span><dl class="ag-kv">${rows}</dl>`;
  }
  function drawerHtml(a) {
    return `<div class="ag-drawer">
      <div class="g"><span class="microlbl">Roles</span><div class="tg">${rolesHtml(a)}</div></div>
      <div class="g"><span class="microlbl">Connection</span><dl class="ag-kv">${connectionHtml(a)}</dl></div>
      <div class="g"><span class="microlbl">Shortcuts</span><div class="ag-links">${shortcutsHtml(a)}</div>${infraHtml(a)}</div>
    </div>`;
  }
  function menuHtml(a) {
    const c = ctx();
    const aid = esc(a.agent_id);
    const item = (act, glyph, label, cls, extra) =>
      `<button type="button" class="mi-row${cls ? ' ' + cls : ''}" data-act="${act}" data-aid="${aid}"${extra || ''}><span class="mi">${glyph}</span>${label}</button>`;
    const items = [];
    if (a.status === 'approved') {
      if (a.update_available) {
        items.push(item('update', '⇈', `Update to ${esc(c.latest || '?')}`, 'warn'), '<hr>');
      }
      items.push(item('ping', '⟁', 'Ping agent'), item('log', '≣', 'Stream log'));
      const provs = c.providers.filter(p => (a.capabilities || {})[p.capability_key]);
      provs.forEach(p => items.push(item('open', '⧉',
        provs.length > 1 ? `Open in LLM Control · ${esc(p.name)}` : 'Open in LLM Control',
        '', ` data-prov="${esc(p.name)}"`)));
      items.push('<hr>');
      if (!a.update_available) items.push(item('update', '⇈', 'Re-deploy current version'));
      items.push(item('config', '✎', 'Edit agent config…'), '<hr>', item('disable', '⊘', 'Disable agent'));
    }
    items.push(item('delete', '⊗', 'Delete agent', 'danger'));
    return `<div class="mc-menu" data-menu="${aid}">${items.join('')}</div>`;
  }
  function actionsHtml(a) {
    const aid = esc(a.agent_id);
    const ib = (act, glyph, tip, cls) =>
      `<button type="button" class="ag-ib${cls ? ' ' + cls : ''}" data-act="${act}" data-aid="${aid}" data-tip="${tip}" aria-label="${tip}">${glyph}</button>`;
    const kebab = `<div class="mc-menuwrap">${ib('menu', ICON.menu, 'More')}${menuHtml(a)}</div>`;
    if (a.status === 'pending') {
      return `<button type="button" class="mcbtn mcbtn-pri mcbtn-sm" data-act="approve" data-aid="${aid}">Approve</button>${kebab}`;
    }
    if (a.status === 'disabled') {
      return `<button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" data-act="approve" data-aid="${aid}">Re-enable</button>${kebab}`;
    }
    if (a.status !== 'approved') return kebab;
    const paused = rowState(a) === 'paused';
    return (paused ? ib('resume', ICON.resume, 'Resume collection', 'resume') : ib('pause', ICON.pause, 'Pause collection'))
      + ib('restart', ICON.restart, 'Restart agent', 'warnh restart') + kebab;
  }
  function rowHtml(a, nowMs) {
    const c = ctx();
    const state = rowState(a);
    const aid = esc(a.agent_id);
    const open = openIds.has(a.agent_id);
    let pill = '';
    if (state === 'pending') pill = '<span class="ag-pill warn">pending approval</span>';
    else if (state === 'paused') pill = '<span class="ag-pill dim">paused</span>';
    else if (state === 'disabled') pill = '<span class="ag-pill dim">disabled</span>';
    const ver = a.version
      ? esc(a.version) + (a.update_available
        ? ` <span class="up" title="${esc(c.latest || '?')} available — see Warnings">↑</span>`
        : (c.latest && a.version === c.latest ? ' <span class="cur">current</span>' : ''))
      : '<span class="cur">no version</span>';
    const meta = [esc(a.os || '?'), esc(a.role || '?')].join('<i>·</i>');
    let seen = '';
    if (a.status === 'approved' && (a.liveness === 'down' || a.liveness === 'stale')) {
      seen = `<div class="ag-seen ${a.liveness === 'down' ? 'gone' : 'old'}"><span class="lbl">${a.liveness}</span> · <span data-seen="${esc(a.last_heartbeat || '')}">${fmtAgo(a.last_heartbeat, nowMs)}</span></div>`;
    } else if (a.status !== 'approved' && a.last_heartbeat) {
      seen = `<div class="ag-seen dim">seen <span data-seen="${esc(a.last_heartbeat)}">${fmtAgo(a.last_heartbeat, nowMs)}</span></div>`;
    }
    return `<div class="ag-rw${open ? ' open' : ''}${state === 'pending' ? ' pending' : ''}" data-row="${aid}">
      <span class="ag-dot ${state}" title="${DOT_TITLE[state] || state}"></span>
      <div class="ag-who" data-act="toggle" data-aid="${aid}" role="button" tabindex="0" aria-expanded="${open}">
        <div class="n"><span class="chev">▸</span><span class="host">${esc(a.hostname || '(no hostname)')}</span>${pill}</div>
        <div class="sub">${showDesc(a) ? `<div class="d">${esc(a.description)}</div>` : ''}<div class="m">${meta}</div></div>
      </div>
      <div class="ag-caps">${capsHtml(a)}</div>
      <div class="ag-ep"><div class="ip">${esc(ipOf(a))}</div><div class="ver">${ver}</div>${seen}</div>
      <div class="ag-act">${actionsHtml(a)}</div>
      ${open ? drawerHtml(a) : ''}
    </div>`;
  }
  // IPs sort numerically per octet; hostnames fall back to plain text.
  const ipKey = a => String(ipOf(a)).replace(/\d+/g, n => n.padStart(3, '0'));
  // Caps order: co-located infra hosts first, then provider caps, then the rest.
  const capsKey = a => {
    const infra = infraList(a).length;
    const provs = ctx().providers.filter(p => (a.capabilities || {})[p.capability_key]).map(p => p.name).sort();
    return `${infra ? 0 : 1}${9 - Math.min(infra, 9)}|${provs.length ? 0 : 1}${provs.join(',')}|${enabledCaps(a).sort().join(',')}|${(a.hostname || '').toLowerCase()}`;
  };
  const SORT_VAL = {
    agent: a => (a.hostname || '').toLowerCase(),
    caps: a => capsKey(a),
    endpoint: a => ipKey(a),
  };
  function sortedFiltered(agents) {
    const f = parseFilter(filterQ);
    const val = SORT_VAL[sort.key] || SORT_VAL.agent;
    return agents.filter(a => matches(a, f))
      .sort((x, y) => sort.dir * String(val(x)).localeCompare(String(val(y))));
  }

  // ── render ───────────────────────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  function renderHeader(c) {
    const s = summary(c.agents);
    const sum = $('agSummary');
    if (sum) {
      sum.innerHTML = c.agents.length
        ? `<span><b>${s.total}</b> registered</span>`
          + `<span><b class="${s.live ? 'ok' : ''}">${s.live}</b> live</span>`
          + `<span><b class="${s.pending ? 'warn' : ''}">${s.pending}</b> pending</span>`
          + `<span><b class="${s.needsUpdate ? 'warn' : ''}">${s.needsUpdate}</b> needs update</span>`
        : '<span>No agents registered</span>';
    }
    const mv = $('agMgrVer'), av = $('agAgentVer');
    if (mv) { mv.hidden = !c.managerVersion; mv.innerHTML = `manager <b>${esc(c.managerVersion || '')}</b>`; }
    if (av) { av.hidden = !c.latest; av.innerHTML = `agent <b>${esc(c.latest || '')}</b>`; }
    const authOn = !(c.global.auth_disabled);
    const tg = $('agAuthTg'), row = $('agAuthRow'), sub = $('agAuthSub');
    if (tg) { tg.classList.toggle('on', authOn); tg.setAttribute('aria-pressed', String(authOn)); }
    if (row) row.classList.toggle('warn', !authOn);
    if (sub) sub.textContent = authOn ? 'Secure agent authentication is on' : 'Secure agent authentication is off';
    const cnt = $('agUpdateCnt');
    if (cnt) { cnt.hidden = !s.needsUpdate; cnt.textContent = `${s.needsUpdate} pending`; }
    const aa = $('agApproveAll');
    if (aa) { aa.hidden = s.pending < 2; aa.querySelector('.cnt').textContent = `${s.pending} waiting`; }
    return s;
  }
  function renderRoster(c) {
    const host = $('agRoster');
    if (!host) return;
    const nowMs = Date.now();
    const rows = sortedFiltered(c.agents);
    const hd = `<div class="ag-rw hd"><span></span>`
      + `<span class="sortable${sort.key === 'agent' ? ' on' : ''}" data-sort="agent"${sort.key === 'agent' ? ` data-dir="${sort.dir}"` : ''}>Agent</span>`
      + `<span class="sortable${sort.key === 'caps' ? ' on' : ''}" data-sort="caps"${sort.key === 'caps' ? ` data-dir="${sort.dir}"` : ''}>Capabilities &amp; roles</span>`
      + `<span class="sortable${sort.key === 'endpoint' ? ' on' : ''}" data-sort="endpoint"${sort.key === 'endpoint' ? ` data-dir="${sort.dir}"` : ''}>Endpoint</span>`
      + `<span></span></div>`;
    let body;
    let noMatch = false;
    if (!c.agents.length) body = '<div class="ag-empty">No agents registered yet. Install an agent and it appears here for approval.</div>';
    else if (!rows.length) { body = '<div class="ag-empty">No agents match <b class="q"></b></div>'; noMatch = true; }
    else body = rows.map(a => rowHtml(a, nowMs)).join('');
    host.innerHTML = hd + body;
    // The query is operator input; set it as text so it never parses as markup.
    if (noMatch) host.querySelector('.ag-empty .q').textContent = filterQ;
  }
  // One stamp for the whole Admin tab, in the System Health header.
  function renderStamp() {
    const rf = $('adminRefreshRf'), t = $('adminRefreshTime'), stamp = $('adminRefreshStamp');
    const state = stampState(st);
    if (rf) rf.className = 'hc-rf ' + state + (st.busy ? ' busy' : '');
    if (t) t.textContent = st.lastOkAt ? 'updated ' + clock(new Date(st.lastOkAt)) : '—';
    if (stamp) stamp.setAttribute('aria-label', 'Refresh now');
  }
  function render() {
    const c = ctx();
    renderHeader(c);
    const host = $('agRoster');
    if (!openMenu || !(host && host.querySelector('.ag-rw'))) renderRoster(c);
    renderStamp();
    if (!ticker && typeof setInterval === 'function') ticker = setInterval(tick, 5000);
  }
  function tick() {
    renderStamp();
    const host = $('agRoster');
    if (!host) return;
    const now = Date.now();
    host.querySelectorAll('[data-seen]').forEach(el => { el.textContent = fmtAgo(el.dataset.seen, now); });
  }
  // Refresh outcome from adminLoadAgents: ok, failed (HTTP error) or unreachable.
  function stamp(r) {
    st.busy = false;
    if (r && r.ok) { st.lastOkAt = Date.now(); st.failed = false; st.unreachable = false; }
    else { st.failed = true; st.unreachable = !!(r && r.unreachable); }
    renderStamp();
  }

  // ── menus + events ───────────────────────────────────────────────────────
  function closeMenus() {
    document.querySelectorAll('#admin-agents .mc-menu.open').forEach(m => {
      m.classList.remove('open');
      m.style.cssText = '';
    });
    openMenu = null;
  }
  function openMenuEl(m, btn) {
    const was = m.classList.contains('open');
    closeMenus();
    if (was) return;
    m.classList.add('open');
    openMenu = m;
    if (!btn || typeof btn.getBoundingClientRect !== 'function') return;
    const r = btn.getBoundingClientRect();
    const vw = window.innerWidth || document.documentElement.clientWidth || 0;
    m.style.position = 'fixed';
    m.style.top = `${Math.round(r.bottom + 6)}px`;
    m.style.right = `${Math.max(8, Math.round(vw - r.right))}px`;
    m.style.zIndex = '1200';
    const mh = m.offsetHeight || 0;
    const vh = window.innerHeight || document.documentElement.clientHeight || 0;
    if (vh && mh && r.bottom + 6 + mh > vh - 8) m.style.top = `${Math.max(8, Math.round(r.top - 6 - mh))}px`;
  }
  function refreshNow() {
    st.busy = true;
    renderStamp();
    if (typeof window.adminRefreshNow === 'function') window.adminRefreshNow();
    else call('adminLoadAgents');
  }
  async function approveAll() {
    const pend = ctx().agents.filter(a => a.status === 'pending');
    for (const a of pend) await call('adminApprove', a.agent_id);
  }
  function onAction(act, el) {
    const aid = el.dataset.aid;
    const prov = el.dataset.prov;
    const on = el.classList.contains('on');
    switch (act) {
      case 'toggle': {
        if (openIds.has(aid)) openIds.delete(aid); else openIds.add(aid);
        renderRoster(ctx()); break;
      }
      case 'menu': openMenuEl(el.parentElement.querySelector('.mc-menu'), el); break;
      case 'approve': call('adminApprove', aid); break;
      case 'pause': call('adminToggleCollection', aid, false); break;
      case 'resume': call('adminToggleCollection', aid, true); break;
      case 'restart': closeMenus(); call('adminRestart', aid); break;
      case 'ping': closeMenus(); call('adminPing', aid); break;
      case 'log': closeMenus(); call('adminLogs', aid); break;
      case 'open': closeMenus(); call('_jumpToDashboard', aid, prov); break;
      case 'update': closeMenus(); call('adminUpdate', aid); break;
      case 'config': closeMenus(); call('adminEditConfig', aid); break;
      case 'disable': closeMenus(); call('adminDisable', aid); break;
      case 'delete': closeMenus(); call('adminDelete', aid); break;
      case 'primary': call('adminTogglePrimary', aid, prov, !on); break;
      case 'pool': call('adminTogglePool', prov, aid, !on); break;
      case 'host': call('adminToggleHostAgent', aid, !on); break;
      case 'auth': {
        const c = ctx();
        c.global.auth_disabled = on;   // toggling: on → off means auth disabled
        renderHeader(c);
        call('adminToggleAuth', on);
        break;
      }
      case 'updateall': closeMenus(); call('adminUpdateAll'); break;
      case 'pushca': closeMenus(); call('adminPushCaToAgents'); break;
      case 'approveall': closeMenus(); approveAll(); break;
      case 'refresh': refreshNow(); break;
      default: break;
    }
  }
  function init() {
    const panel = $('admin-agents');
    if (!panel || panel._agBound) return;
    panel._agBound = true;
    panel.addEventListener('click', ev => {
      if (!ev.target.closest('.mc-menuwrap')) closeMenus();
      const sortEl = ev.target.closest('[data-sort]');
      if (sortEl) {
        const key = sortEl.dataset.sort;
        sort = { key, dir: sort.key === key ? -sort.dir : 1 };
        renderRoster(ctx());
        return;
      }
      const el = ev.target.closest('[data-act]');
      if (!el || !panel.contains(el)) return;
      if (el.classList.contains('mc-toggle') && el.classList.contains('disabled')) return;
      ev.stopPropagation();
      onAction(el.dataset.act, el);
    });
    panel.addEventListener('keydown', ev => {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      const el = ev.target.closest('[data-act]');
      if (!el || el.tagName === 'BUTTON' || el.tagName === 'INPUT') return;
      ev.preventDefault();
      onAction(el.dataset.act, el);
    });
    const q = $('agFilter');
    if (q) q.addEventListener('input', () => { filterQ = q.value; renderRoster(ctx()); });
    document.addEventListener('click', ev => {
      if (openMenu && !ev.target.closest('#admin-agents .mc-menuwrap')) closeMenus();
    });
    document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && openMenu) closeMenus(); });
    document.addEventListener('scroll', () => { if (openMenu) closeMenus(); }, true);
    window.addEventListener('resize', () => { if (openMenu) closeMenus(); });
    const stamp = $('adminRefreshStamp');
    if (stamp && !stamp._agBound) { stamp._agBound = true; stamp.addEventListener('click', refreshNow); }
  }
  if (typeof document !== 'undefined') {
    init();
    document.addEventListener('DOMContentLoaded', init);
  }

  window.AgentsView = {
    FRESH_MS, ipOf, infraList, rowState, tlsInfo, showDesc, fmtAgo, clock, fingerprintShort,
    parseFilter, matches, summary, stampState, capsHtml, rolesHtml, connectionHtml, shortcutsHtml,
    drawerHtml, infraHtml, menuHtml, actionsHtml, rowHtml, render, stamp, init, openIds, refreshNow,
    setFilter: q => { filterQ = q; }, setSort: (key, dir) => { sort = { key, dir }; },
    getSort: () => ({ ...sort }),
  };
})();
