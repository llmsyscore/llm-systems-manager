// Admin tab auto-refresh — only ticks when the tab is visible.
let _adminRefreshTimer = null;
function adminStartAutoRefresh() {
  if (_adminRefreshTimer) return;
  // 20s cadence (was 10s) — paired with the backend's anti-flap
  // (requires 2 consecutive failed alarm-engine probes before flipping to
  // DOWN). Slower polling reduces the chance of a transient slow probe
  // landing on the dashboard while still surfacing real outages quickly.
  _adminRefreshTimer = setInterval(() => {
    if (_activeTab === 'admin') { adminLoadAgents(); adminLoadHealth(); }
  }, 20000);
}
function adminStopAutoRefresh() {
  if (_adminRefreshTimer) {
    clearInterval(_adminRefreshTimer);
    _adminRefreshTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Admin tab — agents registry
// ---------------------------------------------------------------------------
function adminEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function adminAgo(iso) {
  if (!iso) return '—';
  const dt = new Date(iso); const ms = Date.now() - dt.getTime();
  if (isNaN(ms)) return '—';
  const s = Math.round(ms/1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.round(s/60) + 'm ago';
  if (s < 86400) return Math.round(s/3600) + 'h ago';
  return Math.round(s/86400) + 'd ago';
}
function adminCaps(c) {
  if (!c || typeof c !== 'object') return '—';
  const enabled = Object.keys(c).filter(k => c[k]);
  return enabled.length ? enabled.join(', ') : '(none)';
}
// Render the per-agent Primary checkbox cluster. Disabled when the agent
// doesn't advertise the matching capability, or when it isn't approved.
// Only one agent can be primary for each kind; flipping a different
// agent's checkbox automatically clears the previous one server-side.
function adminPrimaryCell(a) {
  const aid = adminEsc(a.agent_id);
  const caps = a.capabilities || {};
  const approved = a.status === 'approved';
  const isLlamaPrimary = approved && _adminGlobal.primary_llama_id === a.agent_id;
  const isLmsPrimary   = approved && _adminGlobal.primary_lms_id   === a.agent_id;
  const llamaDisabled  = !approved || !caps.llama;
  const lmsDisabled    = !approved || !caps.lms;
  const llamaTitle = !approved ? 'agent must be approved'
                    : !caps.llama ? 'agent does not advertise llama capability'
                    : isLlamaPrimary ? 'currently the primary llama host — uncheck to clear'
                    : 'mark as the primary llama host';
  const lmsTitle = !approved ? 'agent must be approved'
                    : !caps.lms ? 'agent does not advertise lms capability'
                    : isLmsPrimary ? 'currently the primary lms host — uncheck to clear'
                    : 'mark as the primary lms host';
  return `
    <label style="display:block;${llamaDisabled?'opacity:0.4;':''}cursor:${llamaDisabled?'not-allowed':'pointer'};" title="${llamaTitle}">
      <input type="checkbox" ${isLlamaPrimary?'checked':''} ${llamaDisabled?'disabled':''}
             onchange="adminTogglePrimary('${aid}','llama',this.checked)"> llama
    </label>
    <label style="display:block;margin-top:3px;${lmsDisabled?'opacity:0.4;':''}cursor:${lmsDisabled?'not-allowed':'pointer'};" title="${lmsTitle}">
      <input type="checkbox" ${isLmsPrimary?'checked':''} ${lmsDisabled?'disabled':''}
             onchange="adminTogglePrimary('${aid}','lms',this.checked)"> lms
    </label>`;
}
async function adminTogglePrimary(aid, kind, set) {
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(aid)}/role-primary`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, set }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) {
      _adminLog(`primary ${kind} ${set?'set':'cleared'} failed: ${d.error || r.status}`, 'err');
    } else {
      _adminLog(`✓ primary ${kind} ${set?'set':'cleared'}`, 'ok');
    }
  } catch (e) {
    _adminLog(`primary ${kind} request failed: ${e.message}`, 'err');
  }
  adminLoadAgents();
}
function _adminAgentIP(a) {
  // Pull host from bind_url; if it's a hostname (not an IP), prefer the
  // registered_from address since that's what the manager actually sees.
  const ipRe = /^\d+\.\d+\.\d+\.\d+$/;
  const url = a.bind_url || '';
  const m = url.match(/^https?:\/\/([^:\/]+)/);
  const fromUrl = m ? m[1] : '';
  if (fromUrl && ipRe.test(fromUrl)) return fromUrl;
  if (a.registered_from && ipRe.test(a.registered_from)) return a.registered_from;
  return fromUrl || a.registered_from || '—';
}
function adminCollectionState(agent) {
  // When the agent has gone stale or down, the cached collection_enabled
  // value is meaningless — the agent could be off, the host could be
  // rebooting, anything. Show that explicitly instead of a stale "on".
  const liveness = agent.liveness;
  if (liveness === 'down')  return 'down (no heartbeat ≥10m)';
  if (liveness === 'stale') return 'stale (heartbeats missed)';
  const hb = agent.last_heartbeat_data || {};
  if (hb.collection_enabled === false) return 'paused';
  if (hb.collection_enabled === true)  return 'on';
  return '—';
}
// Two log levels: 'ok' (auto-clears after 6s), 'err' (sticks until next event).
let _adminLogClearTimer = null;
function _adminLog(msg, level = 'ok') {
  const el = document.getElementById('adminAgentsResult');
  if (!el) return;
  const ts = (new Date()).toLocaleTimeString();
  const color = level === 'err' ? 'var(--crit)' : level === 'warn' ? 'var(--warn)' : 'var(--ok)';
  el.style.color = color;
  el.textContent = `${ts}  ${msg}`;
  if (_adminLogClearTimer) {
    clearTimeout(_adminLogClearTimer);
    _adminLogClearTimer = null;
  }
  if (level === 'ok') {
    _adminLogClearTimer = setTimeout(() => { el.textContent = ''; }, 6000);
  }
}

// ── System Health card (Phase 2.5) ────────────────────────────────────
async function adminLoadHealth() {
  try {
    const r = await fetch('/api/admin/system-health');
    if (!r.ok) return;
    const d = await r.json();
    _renderSystemHealth(d);
  } catch (e) {
    /* keep last successful render */
  }
}

function _healthDot(ok) {
  return ok === true ? 'ok' : (ok === false ? 'down' : 'muted');
}

// ── Tab status dots (Events / Admin) ──────────────────────────────────
// Driven globally so the dots reflect live state on any tab. Events turns
// red while a critical alert is active (cleared → green); Admin turns red
// when the system-health roll-up is anything but "ok". A failed/forbidden
// fetch leaves the dot at its prior state rather than flapping to muted.
function _setTabDot(id, state) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('ok', 'alert');
  if (state === 'ok' || state === 'alert') el.classList.add(state);
}
async function refreshTabIndicators() {
  // Events — active critical alerts only. status=active excludes acknowledged
  // (an acked critical is being handled, so it shouldn't hold the dot red).
  (async () => {
    try {
      const r = await fetch('/api/alarm/alerts/?severity=critical&status=active&limit=1');
      if (!r.ok) return;
      const arr = await r.json();
      _setTabDot('tabDotEvents', (Array.isArray(arr) && arr.length > 0) ? 'alert' : 'ok');
    } catch (_) { /* keep prior state */ }
  })();
  // Admin — system-health roll-up ("ok" | "warn" | "down").
  (async () => {
    if (window._me && window._me.admin_access === false) { _setTabDot('tabDotAdmin', 'ok'); return; }
    try {
      const r = await fetch('/api/admin/system-health');
      if (!r.ok) return;
      const d = await r.json();
      _setTabDot('tabDotAdmin', d.overall === 'ok' ? 'ok' : 'alert');
    } catch (_) { /* keep prior state */ }
  })();
}

function _renderSystemHealth(d) {
  // Overall pill
  const pill = document.getElementById('adminHealthOverall');
  if (pill) {
    const ovMod = d.overall === 'ok' ? 'ok' : d.overall === 'warn' ? 'warn' : d.overall === 'down' ? 'crit' : 'muted';
    pill.className = 'status status--' + ovMod + ' status--square';
    pill.textContent = (d.overall || 'unknown').toUpperCase();
  }
  const stamp = document.getElementById('adminHealthRefresh');
  if (stamp) stamp.textContent = 'updated ' + (new Date()).toLocaleTimeString();

  // Services
  const svcEl = document.getElementById('adminHealthServices');
  if (svcEl) {
    const rows = [];
    // Manager itself (rendering = healthy)
    rows.push({
      lbl: 'Manager',
      val: 'up ' + _fmtUptime((d.manager || {}).uptime_s),
      ok: true,
      action: { svc: 'manager', label: 'Manager' },
    });
    for (const s of (d.services || [])) {
      const lbl = s.name === 'alarm_engine' ? 'Alarm Engine' :
                  s.name === 'influxdb'     ? 'InfluxDB'      : s.name;
      let val;
      if (s.ok) val = s.latency_ms != null ? (s.latency_ms + 'ms') : (s.state || 'connected');
      else val = s.error ? (s.error.slice(0, 36)) : ('HTTP ' + (s.status_code || '?'));
      // AE restart only when it runs on this host (manager can only systemctl
      // a local unit); a split AE is restarted on its own host.
      const action = (s.name === 'alarm_engine' && d.ae_local)
        ? { svc: 'alarm_engine', label: 'Alarm Engine' } : undefined;
      rows.push({ lbl, val, ok: s.ok, action });
      // Render an extra row for the alarm engine's TLS state. dot is green when
      // serving HTTPS, red when enabled-but-cert-missing, muted when off.
      if (s.name === 'alarm_engine' && s.tls && typeof s.tls === 'object') {
        const t = s.tls;
        let tlsVal, tlsOk;
        if (t.enabled && t.active) {
          tlsVal = 'HTTPS active'; tlsOk = true;
        } else if (t.enabled && !t.active) {
          tlsVal = (t.error || 'enabled but inactive').slice(0, 60); tlsOk = false;
        } else {
          tlsVal = 'off (plain HTTP)'; tlsOk = null;
        }
        rows.push({ lbl: 'AE TLS', val: tlsVal, ok: tlsOk });
      }
    }
    svcEl.innerHTML = rows.map(_healthRowHtml).join('');
    // Delegated click — bound once on the container so it survives the
    // innerHTML rebuild on every refresh (XSS-safe: reads a data-attribute,
    // no interpolated handler).
    if (!svcEl._restartBound) {
      svcEl._restartBound = true;
      svcEl.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-restart-svc]');
        if (btn) _restartService(btn.getAttribute('data-restart-svc'));
      });
    }
  }

  // Data Flow
  const dfEl = document.getElementById('adminHealthDataFlow');
  if (dfEl) {
    const plp   = (d.data_flow || {}).primary_llama_push || {};
    const pmp   = (d.data_flow || {}).primary_lms_push || {};
    const mfwd  = (d.data_flow || {}).manager_to_alarm_forwarding || {};
    const rows = [];

    // Llama push freshness — only render the row when at least one llama
    // agent is in the registry. A fresh install with no approved agents
    // shouldn't flag "no push yet" as a fault.
    if (!plp.has_agent) {
      rows.push({ lbl: 'Primary llama push', val: 'no llama agent registered', ok: null });
    } else {
      rows.push({
        lbl: 'Primary llama push',
        val: plp.age_s != null ? (plp.age_s + 's ago') : 'no push yet',
        ok: plp.ok,
      });
    }

    // LMS push freshness — same gate as above.
    if (!pmp.has_agent) {
      rows.push({ lbl: 'Primary LMS push', val: 'no LMS agent registered', ok: null });
    } else {
      rows.push({
        lbl: 'Primary LMS push',
        val: pmp.age_s != null ? (pmp.age_s + 's ago') : '—',
        ok: pmp.ok,
      });
    }

    // Forwarding mode
    rows.push({
      lbl: 'Alarm forwarding',
      val: mfwd.active ? `manager (${mfwd.ok_count}/${mfwd.ok_count + mfwd.fail_count})` : 'via agent',
      ok: mfwd.active ? (mfwd.fail_count <= mfwd.ok_count) : true,
    });

    dfEl.innerHTML = rows.map(_healthRowHtml).join('');
  }

  // Warnings
  const warnEl = document.getElementById('adminHealthWarnings');
  if (warnEl) {
    const ws = d.warnings || [];
    if (ws.length === 0) {
      warnEl.innerHTML = '<div style="color:var(--ok); padding:8px 0;">✓ All systems nominal</div>';
    } else {
      warnEl.innerHTML = ws.map(w => `<div class="warn-row">${adminEsc(w)}</div>`).join('');
    }
  }
}

function _healthRowHtml(r) {
  const dotClass = r.ok === true ? 'ok' : (r.ok === false ? 'down' : 'muted');
  const valClass = r.ok === false ? 'val down' : 'val';
  // svc is a fixed enum ('manager'|'alarm_engine'), not user data — but route
  // it through adminEsc + a data-attribute (delegated listener) anyway.
  const action = r.action
    ? `<button class="adm-restart-btn" data-restart-svc="${adminEsc(r.action.svc)}" title="Restart ${adminEsc(r.action.label || r.lbl)}">↻ Restart</button>`
    : '';
  return `<div class="adm-health-row">
    <span class="dot ${dotClass}"></span>
    <span class="lbl">${adminEsc(r.lbl)}</span>
    <span class="${valClass}">${adminEsc(r.val)}</span>
    ${action}
  </div>`;
}

async function _restartService(svc) {
  const label = svc === 'alarm_engine' ? 'Alarm Engine' : 'Manager';
  const isMgr = svc === 'manager';
  const ok = await _themedConfirm({
    title: `Restart ${label}?`,
    bodyHtml: isMgr
      ? 'The manager will restart and the dashboard will be briefly unavailable (a few seconds). The page reloads automatically once it is back.'
      : 'The alarm engine will restart. Metric ingest and alerts pause for a few seconds; agents buffer and retry, so no data is lost.',
    confirmLabel: 'Restart',
    cancelLabel:  'Cancel',
    danger: true,
  });
  if (!ok) return;
  _adminLog(`requesting ${label} restart…`);
  try {
    const r = await fetch(`/api/admin/service/${svc}/restart`, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) {
      _adminLog(`✓ ${label} restart requested`);
      if (isMgr) { _adminLog('… reconnecting in ~6s'); setTimeout(() => location.reload(), 6000); }
    } else {
      _adminLog(`✗ ${label} restart failed (HTTP ${r.status}) — ${d.error || 'unknown error'}`, 'err');
    }
  } catch (e) {
    // The manager killing itself can drop the connection before the response
    // arrives — for the manager that's expected, not an error.
    if (isMgr) {
      _adminLog('… manager restarting (connection dropped as expected) — reconnecting in ~6s');
      setTimeout(() => location.reload(), 6000);
    } else {
      _adminLog(`✗ ${label} restart error — ${e}`, 'err');
    }
  }
}

// Phase 4 #4 polish — render the pool order list as a Sortable drag
// surface. Called from adminLoadAgents on every refresh.
let _adminPoolSortable = null;
function adminRenderPoolOrder() {
  const ul = document.getElementById('adminPoolOrderList');
  if (!ul) return;
  const pool = ((_adminGlobal && _adminGlobal.llama_pool) || []).slice();
  const idToAgent = {};
  for (const a of (_adminAgentsCache || [])) idToAgent[a.agent_id] = a;

  if (pool.length === 0) {
    ul.innerHTML = '<li class="adm-muted" style="padding:10px;">(pool is empty — toggle "in llama pool" on one or more agents above)</li>';
    if (_adminPoolSortable) { try { _adminPoolSortable.destroy(); } catch(e){} _adminPoolSortable = null; }
    return;
  }

  ul.innerHTML = pool.map((aid, i) => {
    const a = idToAgent[aid] || { hostname: '(unknown agent ' + aid.slice(0,8) + '…)', liveness: null, version: '' };
    const livenessBadge = a.liveness === 'live'
      ? '<span class="adm-chip tls-on" style="font-size:11px;padding:0 6px;">live</span>'
      : a.liveness === 'stale'
        ? '<span class="adm-chip tls-pending" style="font-size:11px;padding:0 6px;">stale</span>'
        : '<span class="adm-chip tls-off" style="font-size:11px;padding:0 6px;">' + adminEsc(a.liveness || '?') + '</span>';
    return `<li data-agent-id="${adminEsc(aid)}">
      <span class="pool-handle" title="Drag to reorder">⠿</span>
      <span class="pool-pos">#${i + 1}</span>
      <span class="pool-hostname">${adminEsc(a.hostname || aid.slice(0,8))}</span>
      ${livenessBadge}
      <span class="pool-meta">${adminEsc(a.version || '')}</span>
    </li>`;
  }).join('');

  // Tear down any previous Sortable instance and re-attach to the
  // freshly-rendered list so element refs aren't stale.
  if (_adminPoolSortable) { try { _adminPoolSortable.destroy(); } catch(e){} _adminPoolSortable = null; }
  _adminPoolSortable = Sortable.create(ul, {
    animation: 150,
    handle: '.pool-handle',
    ghostClass: 'dragging',
    onEnd: adminPoolReorderCommit,
  });
}

// Called by Sortable when a drag completes. Read the new order out of
// the DOM, POST each moved agent to /api/agents/<id>/llama-pool with
// its new position. Reload on completion so backend truth wins.
async function adminPoolReorderCommit() {
  const ul = document.getElementById('adminPoolOrderList');
  const resultEl = document.getElementById('adminPoolResult');
  const newOrder = Array.from(ul.querySelectorAll('li[data-agent-id]')).map(li => li.dataset.agentId);
  const oldOrder = (_adminGlobal && _adminGlobal.llama_pool) || [];
  if (JSON.stringify(newOrder) === JSON.stringify(oldOrder)) return;

  resultEl.textContent = 'saving new order…';
  // We only need to move agents whose position changed. Walk newOrder
  // and POST each at its target index. The backend's llama-pool
  // endpoint already handles "remove if present, then insert at
  // position" semantics — so one POST per agent is enough.
  for (let i = 0; i < newOrder.length; i++) {
    if (newOrder[i] === oldOrder[i]) continue;   // unchanged
    try {
      await fetch(`/api/agents/${encodeURIComponent(newOrder[i])}/llama-pool`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ in_pool: true, position: i }),
      });
    } catch (e) {
      resultEl.textContent = 'reorder failed at index ' + i + ': ' + e.message;
      adminLoadAgents();
      return;
    }
  }
  resultEl.textContent = 'reordered ✓';
  adminLoadAgents();
}

// Phase 4 #4 polish — fetch the union of model IDs across the pool
// agents and populate the <datalist> the pin-editor's input is wired
// to. Quiet on failure: operator can still type a custom model id.
async function adminLoadLlamaModels() {
  const dl = document.getElementById('adminLlamaModels');
  if (!dl) return;
  try {
    const r = await fetch('/api/admin/llama-models');
    if (!r.ok) return;
    const d = await r.json();
    const models = (d.models || []);
    dl.innerHTML = models.map(m =>
      `<option value="${adminEsc(m.id)}">${m.agents ? 'on: ' + adminEsc(m.agents.join(', ')) : ''}</option>`
    ).join('');
  } catch (e) {
    // Best-effort; pin editor still works with free-form text input
  }
}

// Phase 4 #4 — render the llama-model pins editor. Called from
// adminLoadAgents so every agent-list refresh keeps the pin table in
// sync (and repopulates the agent <select> in case the pool changed).
function adminRenderPins() {
  const tbody = document.getElementById('adminPinsTbody');
  const select = document.getElementById('adminPinAgentSelect');
  if (!tbody || !select) return;
  const pins = (_adminGlobal && _adminGlobal.llama_model_pins) || {};
  const pool = (_adminGlobal && _adminGlobal.llama_pool) || [];
  // Build agent_id → hostname lookup from the agents cache.
  const idToHost = {};
  for (const a of (_adminAgentsCache || [])) idToHost[a.agent_id] = a.hostname;

  // Pin rows
  const entries = Object.entries(pins).sort(([m1], [m2]) => m1.localeCompare(m2));
  if (entries.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3" style="padding:14px;color:var(--fg-muted);text-align:center;">(no pins set — all models load-balance across the pool)</td></tr>';
  } else {
    tbody.innerHTML = entries.map(([model, aid]) => {
      const host = idToHost[aid] || `<span class="adm-muted">${adminEsc(aid).slice(0,8)}… (unknown agent)</span>`;
      return `<tr>
        <td style="font-family:ui-monospace,Menlo,Consolas,monospace;">${adminEsc(model)}</td>
        <td>${typeof host === 'string' && host.indexOf('<') === 0 ? host : adminEsc(host)}</td>
        <td style="text-align:right;">
          <button class="adm-btn-icon danger" title="Remove pin" onclick="adminClearPin('${adminEsc(model)}')">✕</button>
        </td>
      </tr>`;
    }).join('');
  }

  // Refresh the agent <select>. Only show agents that are (a) in the
  // pool, or (b) approved + llama-capable — those are the only ones
  // worth pinning a model to.
  const eligible = (_adminAgentsCache || []).filter(a =>
    a.status === 'approved' &&
    ((a.capabilities || {}).llama) &&
    (pool.includes(a.agent_id) || pool.length === 0)
  );
  const current = select.value;
  select.innerHTML = '<option value="">(choose agent)</option>' +
    eligible.map(a => `<option value="${adminEsc(a.agent_id)}">${adminEsc(a.hostname)}${pool.includes(a.agent_id) ? ' · pool #' + (pool.indexOf(a.agent_id) + 1) : ''}</option>`).join('');
  if (current) select.value = current;
}

async function adminLoadPins() {
  // Pin state lives inside _adminGlobal (loaded by /api/agents). Just
  // re-render — no separate endpoint needed.
  adminRenderPins();
}

async function adminAddPin() {
  const modelEl = document.getElementById('adminPinModelInput');
  const agentEl = document.getElementById('adminPinAgentSelect');
  const resultEl = document.getElementById('adminPinsResult');
  const model = (modelEl.value || '').trim();
  const aid = agentEl.value || '';
  if (!model) { resultEl.textContent = 'Enter a model id'; return; }
  if (!aid)   { resultEl.textContent = 'Pick an agent'; return; }
  try {
    const r = await fetch('/api/admin/llama-pins', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: model, agent_id: aid }),
    });
    const d = await r.json();
    if (!r.ok || d.ok === false) {
      resultEl.textContent = 'failed: ' + (d.error || r.status);
      return;
    }
    resultEl.textContent = `pinned ${model} → ${aid.slice(0,8)}…`;
    modelEl.value = '';
  } catch (e) {
    resultEl.textContent = 'request failed: ' + e.message;
  }
  adminLoadAgents();   // refreshes _adminGlobal, then re-renders pins
}

async function adminClearPin(model) {
  const resultEl = document.getElementById('adminPinsResult');
  try {
    const r = await fetch('/api/admin/llama-pins', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: model, agent_id: '' }),
    });
    const d = await r.json();
    if (!r.ok || d.ok === false) {
      resultEl.textContent = 'failed: ' + (d.error || r.status);
      return;
    }
    resultEl.textContent = `cleared pin for ${model}`;
  } catch (e) {
    resultEl.textContent = 'request failed: ' + e.message;
  }
  adminLoadAgents();
}

async function adminLoadAgents() {
  try {
    const r = await fetch('/api/agents');
    if (!r.ok) {
      _adminLog('GET /api/agents failed: ' + r.status + ' (admin gate denies this IP)');
      return;
    }
    const d = await r.json();
    document.getElementById('adminAuthDisabled').checked = !!(d.global && d.global.auth_disabled);
    _adminGlobal = d.global || {};
    _latestAgentVersion = d.latest_agent_version || null;
    const lavEl = document.getElementById('adminLatestVersion');
    if (lavEl) lavEl.textContent = _latestAgentVersion ? `manager: ${_latestAgentVersion}` : '';
    const tbody = document.getElementById('adminAgentsTbody');
    const agents = (d.agents || []).slice().sort((a,b) => (a.hostname||'').localeCompare(b.hostname||''));
    _adminAgentsCache = agents;
    const countEl = document.getElementById('adminAgentsCount');
    if (countEl) {
      const approved = agents.filter(a => a.status === 'approved').length;
      const total = agents.length;
      const live = agents.filter(a => a.liveness === 'live').length;
      countEl.textContent = total
        ? `${total} registered · ${approved} approved · ${live} live`
        : 'No agents registered';
    }
    if (!agents.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="padding:24px;color:var(--fg-muted);text-align:center;">No agents registered yet.</td></tr>';
      return;
    }
    tbody.innerHTML = agents.map(a => _adminRowHtml(a)).join('');
    const stamp = document.getElementById('adminLastRefresh');
    if (stamp) stamp.textContent = 'updated ' + (new Date()).toLocaleTimeString();
    // Phase 4 #4 — keep the pin editor + pool order + model
    // datalist in sync with every agent refresh. _adminGlobal +
    // _adminAgentsCache are now populated.
    adminRenderPins();
    adminRenderPoolOrder();
    adminLoadLlamaModels();   // fire-and-forget; populates datalist
  } catch(e) {
    _adminLog('error: ' + e.message);
  }
}

// Render one row of the agents table. Compact 5-column layout:
// Agent (hostname/IP/desc/user/version) · State (status + collection
// badges) · Capabilities (chips + primary radios) · Last seen ·
// Actions (primary CTAs + overflow menu).
function _adminRowHtml(a) {
  const aid = adminEsc(a.agent_id);
  const ip = adminEsc(_adminAgentIP(a));
  const status = a.status || 'unknown';
  const isPending  = status === 'pending';
  const isApproved = status === 'approved';
  const isDisabled = status === 'disabled';
  const collection = adminCollectionState(a);
  const live = a.liveness || 'unknown';

  // Identity block
  const ident = `
    <div class="adm-host">${adminEsc(a.hostname || '(no hostname)')}</div>
    <div class="adm-host-meta">
      <code>${ip}</code> · ${adminEsc(a.os || '?')} · ${adminEsc(a.role || '?')}
      ${a.agent_user ? ` · run-as <code>${adminEsc(a.agent_user)}</code>` : ''}
    </div>
    <div class="adm-host-meta">
      <span class="adm-version" title="Agent code version reported in heartbeat">${adminEsc(a.version || 'no version')}</span>
      ${a.update_available ? `<span class="adm-version-new" title="Manager has v${adminEsc(_latestAgentVersion || '?')} — click Update to deploy">↑ update available</span>` : ''}
    </div>
    ${_adminInfraChips(a)}
    ${a.description ? `<div class="adm-host-desc">${adminEsc(a.description)}</div>` : ''}
  `;

  // State badges
  const statusMod = status === 'approved' ? 'ok' : status === 'pending' ? 'warn'
                  : status === 'disabled' ? 'muted' : status === 'unknown' ? 'muted' : 'crit';
  let stateHtml = `<span class="status status--${statusMod} status--square">${adminEsc(status)}</span>`;
  if (isApproved) {
    const liveMod = live === 'live' ? 'ok' : live === 'stale' ? 'warn' : live === 'down' ? 'crit' : 'muted';
    const liveLabel = live === 'live' ? 'live' : live === 'stale' ? 'stale' : live === 'down' ? 'down' : 'unknown';
    stateHtml += `<span class="status status--${liveMod} status--square">${liveLabel}</span>`;
    if (collection === 'on')       stateHtml += `<span class="status status--ok status--square">collecting</span>`;
    else if (collection === 'paused') stateHtml += `<span class="status status--warn status--square">paused</span>`;
    else if (collection === 'down (no heartbeat ≥10m)') stateHtml += ''; // already covered by 'down' badge
    else if (collection === 'stale (heartbeats missed)') stateHtml += ''; // covered by 'stale' badge
    else if (collection !== '—')   stateHtml += `<span class="status status--crit status--square">${adminEsc(collection)}</span>`;
    // ↔ TLS = both directions encrypted; → TLS = manager→agent only (agent
    // still dials the manager over http, e.g. before the auto-upgrade landed).
    const _m2a = (a.bind_url || '').startsWith('https://');
    const _a2m = !!(a.last_heartbeat_data && a.last_heartbeat_data.control_channel_tls);
    if (_m2a && _a2m) {
      stateHtml += `<span class="status status--ok status--square" title="Manager↔agent: both directions over TLS">↔ TLS</span>`;
    } else if (_m2a) {
      stateHtml += `<span class="status status--info status--square" title="Manager→agent over TLS only; agent dials the manager over HTTP. Set [manager].tls_port to enable auto-upgrade.">→ TLS</span>`;
    }
  }
  stateHtml = `<div class="adm-badge-row">${stateHtml}</div>`;

  // Capabilities: chips for active caps; primary radios appended
  const capsHtml = _adminCapsAndPrimary(a);

  // Last seen
  const seen = `<div class="adm-seen">${adminEsc(adminAgo(a.last_heartbeat))}</div>`;

  // Actions
  const actions = _adminActions(a, aid, isPending, isApproved, isDisabled);

  return `<tr>
    <td>${ident}</td>
    <td>${stateHtml}</td>
    <td>${capsHtml}</td>
    <td>${seen}</td>
    <td style="text-align:right;">${actions}</td>
  </tr>`;
}

// Core-infrastructure chips: rendered under the version line when the
// agent's host also runs the manager, alarm engine, or InfluxDB.
// `colocated_infra` is computed server-side in /api/agents.
const _adminInfraLabels = {
  manager:      'manager',
  alarm_engine: 'alarm engine',
  influxdb:     'influxdb',
};
function _adminInfraChips(a) {
  const infra = Array.isArray(a.colocated_infra) ? a.colocated_infra : [];
  if (!infra.length) return '';
  const chips = infra.map(svc => {
    const label = _adminInfraLabels[svc.role] || svc.role;
    const ver   = svc.version ? ` ${svc.version}` : '';
    const title = svc.version
      ? `${label} colocated on this host — version ${svc.version}`
      : `${label} colocated on this host — version unknown`;
    return `<span class="adm-chip infra" title="${adminEsc(title)}">⛬ ${adminEsc(label)}${adminEsc(ver)}</span>`;
  }).join('');
  return `<div class="adm-host-meta">${chips}</div>`;
}

// Capability chips + primary checkboxes inline. Replaces the old
// separate "Capabilities" and "Primary" columns.
function _adminCapsAndPrimary(a) {
  const caps = a.capabilities || {};
  const order = ['llama', 'lms', 'openclaw', 'image_gen', 'perf_controller', 'sysperf'];
  const enabled = order.filter(k => caps[k]);
  const isPrimaryLlama = _adminGlobal.primary_llama_id === a.agent_id;
  const isPrimaryLms   = _adminGlobal.primary_lms_id   === a.agent_id;
  // Phase 4 #4 — pool membership chip.
  const pool = _adminGlobal.llama_pool || [];
  const poolIdx = pool.indexOf(a.agent_id);
  const chipHtml = enabled.map(k => {
    const isP = (k === 'llama' && isPrimaryLlama) || (k === 'lms' && isPrimaryLms);
    return `<span class="adm-chip ${isP ? 'primary' : ''}" title="${isP ? 'primary ' + k + ' host' : k + ' capability'}">${adminEsc(k)}${isP ? ' ★' : ''}</span>`;
  }).join('');

  const approved = a.status === 'approved';
  const aid = adminEsc(a.agent_id);
  const llamaDisabled = !approved || !caps.llama;
  const lmsDisabled   = !approved || !caps.lms;
  const poolBadge = (poolIdx >= 0)
    ? `<span class="adm-chip primary" title="position ${poolIdx + 1} in llama pool (round-robin order)">pool #${poolIdx + 1}</span>`
    : '';
  // Phase 4 #3 — TLS state derived from bind_url + last_cert_issued_at.
  const bind = a.bind_url || '';
  const certIssued = a.last_cert_issued_at;
  let tlsBadge = '';
  if (bind.startsWith('https://')) {
    tlsBadge = `<span class="adm-chip tls-on" title="manager dials this agent over TLS; cert chain validates">🔒 TLS</span>`;
  } else if (certIssued) {
    const issuedDate = certIssued.slice(0, 10);
    tlsBadge = `<span class="adm-chip tls-pending" title="cert was issued ${issuedDate} but agent hasn't restarted to bind HTTPS yet">⏳ TLS pending</span>`;
  } else if (approved) {
    tlsBadge = `<span class="adm-chip tls-off" title="no cert issued yet — auto-distribution will happen on next heartbeat">○ HTTP</span>`;
  }
  const primary = `
    <div class="adm-primary-row">
      <label class="${llamaDisabled ? 'disabled' : ''}" title="${llamaDisabled ? (caps.llama ? 'agent must be approved' : 'agent has no llama capability') : (isPrimaryLlama ? 'currently primary llama host — uncheck to clear' : 'mark as primary llama host')}">
        <input type="checkbox" ${isPrimaryLlama ? 'checked' : ''} ${llamaDisabled ? 'disabled' : ''}
               onchange="adminTogglePrimary('${aid}','llama',this.checked)"> primary llama
      </label>
      <label class="${lmsDisabled ? 'disabled' : ''}" title="${lmsDisabled ? (caps.lms ? 'agent must be approved' : 'agent has no lms capability') : (isPrimaryLms ? 'currently primary lms host — uncheck to clear' : 'mark as primary lms host')}">
        <input type="checkbox" ${isPrimaryLms ? 'checked' : ''} ${lmsDisabled ? 'disabled' : ''}
               onchange="adminTogglePrimary('${aid}','lms',this.checked)"> primary lms
      </label>
      <label class="${llamaDisabled ? 'disabled' : ''}" title="${llamaDisabled ? 'agent must be approved + advertise llama' : (poolIdx >= 0 ? 'remove from llama pool' : 'add to llama pool (round-robin)')}">
        <input type="checkbox" ${poolIdx >= 0 ? 'checked' : ''} ${llamaDisabled ? 'disabled' : ''}
               onchange="adminToggleLlamaPool('${aid}',this.checked)"> in llama pool
      </label>
    </div>
  `;
  // "View dashboard" — jump to the dashboard with this agent selected, one
  // button per provider capability the (approved) agent holds.
  const viewBtns = approved
    ? ['llama', 'lms'].filter(k => caps[k]).map(k =>
        `<button class="adm-chip" style="cursor:pointer;border:none;" title="View this agent on the ${k} dashboard" onclick="_jumpToDashboard('${aid}','${k}')">⧉ view ${adminEsc(k)}</button>`
      ).join(' ')
    : '';
  const viewRow = viewBtns ? `<div class="adm-view-row" style="margin-top:6px;">${viewBtns}</div>` : '';
  return `<div>${chipHtml || '<span class="adm-muted">(no capabilities)</span>'} ${poolBadge} ${tlsBadge}</div>${approved ? primary : ''}${viewRow}`;
}

// Jump to the dashboard with this agent selected for the provider. Only pins a
// selection when there's more than one agent of that provider — a single-agent
// install stays byte-identical (no ?agent= persisted).
function _jumpToDashboard(agentId, provider) {
  const list = (window._agentsByProvider && window._agentsByProvider[provider]) || [];
  if (list.length > 1 && typeof _selectAgent === 'function') {
    _selectAgent(provider, agentId);
  }
  const sub = provider === 'lms' ? 'lmstudio' : 'llamacpp';
  if (typeof switchTab === 'function') switchTab('dashboard');
  if (typeof switchSubTab === 'function') switchSubTab('dashboard', sub);
}

// Phase 4 #4 — POST /api/agents/<id>/llama-pool to add/remove.
async function adminToggleLlamaPool(aid, inPool) {
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(aid)}/llama-pool`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ in_pool: inPool }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      _adminLog('llama-pool ' + (inPool ? 'add' : 'remove') + ' failed: ' + (d.error || r.status));
    }
  } catch (e) {
    _adminLog('llama-pool request failed: ' + e.message);
  }
  adminLoadAgents();
}

// Compact action row: contextual primary CTA + icon strip + overflow.
// Updates / Restart / Ping in the strip; rare actions (Disable /
// Re-enable / Delete) in the kebab menu so they don't dominate.
function _adminActions(a, aid, isPending, isApproved, isDisabled) {
  if (isPending) {
    return `<button class="adm-btn primary" onclick="adminApprove('${aid}')">Approve</button>
            <div class="adm-menu-wrap">
              <button class="adm-btn-icon" onclick="_adminMenuToggle('${aid}', event)" title="More actions">⋮</button>
              <div class="adm-menu" id="adminMenu-${aid}">
                <button class="danger" onclick="adminDelete('${aid}');_adminMenuClose();">Delete</button>
              </div>
            </div>`;
  }
  if (isDisabled) {
    return `<button class="adm-btn primary" onclick="adminApprove('${aid}')">Re-enable</button>
            <div class="adm-menu-wrap">
              <button class="adm-btn-icon" onclick="_adminMenuToggle('${aid}', event)" title="More actions">⋮</button>
              <div class="adm-menu" id="adminMenu-${aid}">
                <button class="danger" onclick="adminDelete('${aid}');_adminMenuClose();">Delete</button>
              </div>
            </div>`;
  }
  if (isApproved) {
    const collection = adminCollectionState(a);
    const isPaused = collection === 'paused';
    const pauseIcon = isPaused
      ? `<button class="adm-btn-icon" onclick="adminToggleCollection('${aid}', true)" title="Resume collection">▶</button>`
      : `<button class="adm-btn-icon" onclick="adminToggleCollection('${aid}', false)" title="Pause collection">⏸</button>`;
    // Update button is foregrounded only when the manager has a newer
    // version than what this agent reports. Otherwise it lives in the
    // overflow menu so it doesn't shout for attention on every row.
    const updateBtn = a.update_available
      ? `<button class="adm-btn primary" onclick="adminUpdate('${aid}')" title="Deploy v${adminEsc(_latestAgentVersion || '?')} (current: ${adminEsc(a.version || '?')})">Update</button>`
      : '';
    const updateMenu = a.update_available
      ? ''
      : `<button onclick="adminUpdate('${aid}');_adminMenuClose();">Re-deploy current version</button><hr>`;
    return `
      ${updateBtn}
      ${pauseIcon}
      <button class="adm-btn-icon" onclick="adminRestart('${aid}')" title="Restart agent">↻</button>
      <button class="adm-btn-icon" onclick="adminPing('${aid}')" title="Ping agent">⟁</button>
      <button class="adm-btn-icon" onclick="adminLogs('${aid}')" title="Stream agent log">📜</button>
      <div class="adm-menu-wrap">
        <button class="adm-btn-icon" onclick="_adminMenuToggle('${aid}', event)" title="More actions">⋮</button>
        <div class="adm-menu" id="adminMenu-${aid}">
          ${updateMenu}
          <button onclick="adminEditConfig('${aid}');_adminMenuClose();">Edit config…</button>
          <button onclick="adminDisable('${aid}');_adminMenuClose();">Disable agent</button>
          <hr>
          <button class="danger" onclick="adminDelete('${aid}');_adminMenuClose();">Delete agent</button>
        </div>
      </div>`;
  }
  return '<span class="adm-muted">—</span>';
}

// Overflow menu open/close. One menu open at a time.
let _adminOpenMenuId = null;
function _adminMenuToggle(aid, evt) {
  if (evt) { evt.stopPropagation(); }
  _adminMenuClose();
  const m = document.getElementById('adminMenu-' + aid);
  if (m) { m.classList.add('open'); _adminOpenMenuId = aid; }
}
function _adminMenuClose() {
  if (!_adminOpenMenuId) return;
  const m = document.getElementById('adminMenu-' + _adminOpenMenuId);
  if (m) m.classList.remove('open');
  _adminOpenMenuId = null;
}
document.addEventListener('click', () => _adminMenuClose());

async function adminApprove(aid) {
  const name = _adminAgentName(aid);
  const r = await fetch(`/api/agents/${aid}/approve`, {method:'POST'});
  if (r.ok) _adminLog(`✓ ${name} approved`);
  else      _adminLog(`✗ approve ${name} failed (HTTP ${r.status})`, 'err');
  adminLoadAgents();
}
async function adminDisable(aid) {
  const name = _adminAgentName(aid);
  const ok = await _themedConfirm({
    title:        `Disable ${adminEsc(name)}?`,
    bodyHtml:     'It will stop accepting manager calls until re-enabled.',
    confirmLabel: 'Disable',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  const r = await fetch(`/api/agents/${aid}/disable`, {method:'POST'});
  if (r.ok) _adminLog(`✓ ${name} disabled`);
  else      _adminLog(`✗ disable ${name} failed (HTTP ${r.status})`, 'err');
  adminLoadAgents();
}
async function adminDelete(aid) {
  const name = _adminAgentName(aid);
  const ok = await _themedConfirm({
    title:        `Delete registration for ${adminEsc(name)}?`,
    bodyHtml:     'This cannot be undone. The agent will be removed from the registry.',
    confirmLabel: 'Delete',
    cancelLabel:  'Cancel',
    danger:       true,
  });
  if (!ok) return;
  const r = await fetch(`/api/agents/${aid}`, {method:'DELETE'});
  if (r.ok) _adminLog(`✓ ${name} deleted from registry`);
  else      _adminLog(`✗ delete ${name} failed (HTTP ${r.status})`, 'err');
  adminLoadAgents();
}
// Find the agent's hostname for nicer log messages — we use the short id
// as a fallback if the agent isn't in the cached list.
let _adminAgentsCache = [];
let _adminGlobal = {};
let _latestAgentVersion = null;
function _adminAgentName(aid) {
  const a = _adminAgentsCache.find(x => x.agent_id === aid);
  return a ? (a.hostname || aid.slice(0, 8)) : aid.slice(0, 8);
}

async function adminPing(aid) {
  const name = _adminAgentName(aid);
  _adminLog(`pinging ${name}…`);
  const r = await fetch(`/api/agents/${aid}/status-check`, {method:'POST'});
  const d = await r.json().catch(() => ({}));
  const body = d.data || {};
  if (r.ok && d.ok && typeof body === 'object') {
    const caps = body.capabilities
      ? Object.keys(body.capabilities).filter(k => body.capabilities[k]).join(', ') || 'none'
      : '?';
    _adminLog(`✓ ${name} alive (${d.latency_ms}ms) — ${body.os}/${body.role}, user=${body.agent_user}, caps=${caps}, collection=${body.collection_enabled ? 'on' : 'off'}`);
  } else {
    const tried = (d.tried || []).join(' → ');
    _adminLog(`✗ ping ${name} failed (HTTP ${r.status}) — ${d.error || 'unknown error'}${tried ? '   tried: ' + tried : ''}`, 'err');
  }
}
async function adminRestart(aid) {
  const name = _adminAgentName(aid);
  const ok = await _themedConfirm({
    title: `Restart ${adminEsc(name)}?`,
    bodyHtml: 'The agent will exit and systemd / launchd will bring it back within a few seconds.',
    confirmLabel: 'Restart',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  _adminLog(`asking ${name} to restart…`);
  const r = await fetch(`/api/agents/${aid}/restart`, {method:'POST'});
  const d = await r.json().catch(() => ({}));
  if (r.ok && d.ok) {
    _adminLog(`✓ ${name} restart requested — should reappear in ~3s`);
    setTimeout(adminLoadAgents, 5000);
  } else {
    const tried = (d.tried || []).join(' → ');
    _adminLog(`✗ restart ${name} failed (HTTP ${r.status}) — ${d.error || 'unknown error'}${tried ? '   tried: ' + tried : ''}`, 'err');
    setTimeout(adminLoadAgents, 1500);
  }
}
const _BACKUP_LABELS = {manager: 'Manager', alarm_engine: 'Alarm Engine'};
const _BACKUP_ENDPOINTS = {
  manager:      {
    export:        '/api/admin/export/manager',
    preview:       '/api/admin/import/manager/preview',
    apply:         '/api/admin/import/manager/apply',
    restartHint:   'sudo systemctl restart llm-systems-manager',
    extraSteps:    null,
  },
  alarm_engine: {
    export:        '/api/alarm/admin/export',
    preview:       '/api/alarm/admin/import/preview',
    apply:         '/api/alarm/admin/import/apply',
    restartHint:   'sudo systemctl restart llm-systems-alarm-engine',
    extraSteps:    null,
  },
};
const _BACKUP_MIN_PW = 12;

async function adminAuthLoad() {
  const status = document.getElementById('adminAuthStatus');
  try {
    const r = await fetch('/api/admin/auth');
    if (!r.ok) { if (status) status.textContent = 'admin-gated (not allowed from this IP)'; return; }
    const d = await r.json();
    if (!d.ok) return;
    const sel = document.getElementById('adminAuthMode');
    // Under the 'auto' policy the live mode comes from app state — show it so
    // it can be changed instantly; otherwise show the pinned config value.
    if (sel) sel.value = (d.policy === 'auto') ? d.mode : d.policy;
    const hint = document.getElementById('adminAuthModeHint');
    if (hint) hint.textContent = d.instant
      ? 'UI-managed (config policy: auto) — mode changes apply instantly.'
      : 'Pinned in config — changing the mode rewrites the config file and needs a manager restart.';
    if (status) status.textContent =
      `mode: ${d.mode}` +
      (d.is_default ? ' · ⚠ default password (llmadmin) — change it in the Account menu' : '');
  } catch (e) { if (status) status.textContent = ''; }
}

async function adminAuthSave() {
  const res = document.getElementById('adminAuthResult');
  const mode = document.getElementById('adminAuthMode').value;
  res.style.color = 'var(--fg-muted)'; res.textContent = 'saving…';
  try {
    const r = await fetch('/api/admin/auth', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ mode })});
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { res.style.color = 'var(--crit)'; res.textContent = '✗ ' + (d.error || ('HTTP ' + r.status)); return; }
    if (d.restart_required) {
      // The mode was written to the config file but only loads at startup, so
      // it isn't live yet. The manager can't restart itself (no privilege), so
      // surface the command for the operator to run.
      res.style.color = 'var(--warn)';
      res.innerHTML = '✓ saved to config — <b>restart required</b> to apply the new mode:<br>' +
        `<code style="display:inline-block;margin-top:6px;padding:4px 8px;background:var(--bg);border:1px solid var(--border);border-radius:4px;user-select:all;">${adminEsc(d.restart_cmd || 'sudo systemctl restart llm-systems-manager')}</code>`;
    } else {
      res.style.color = 'var(--ok)';
      res.textContent = `✓ saved — mode: ${d.mode}`;
    }
    adminAuthLoad();
  } catch (e) { res.style.color = 'var(--crit)'; res.textContent = '✗ ' + e.message; }
}

function _adminBackupLog(msg, cls) {
  const el = document.getElementById('adminBackupResult');
  if (!el) return;
  const ts = new Date().toTimeString().slice(0, 8);
  const color = cls === 'err' ? 'var(--crit)' : (cls === 'ok' ? 'var(--ok)' : 'var(--fg-muted)');
  el.innerHTML = `<span style="color:${color};">[${ts}] ${msg}</span>`;
}

async function adminExportArchive(component) {
  const label = _BACKUP_LABELS[component] || component;
  const ep = _BACKUP_ENDPOINTS[component];
  if (!ep) return;
  const password = await _adminBackupPasswordPrompt({
    title:   `Export ${label} archive`,
    intro:   `The archive contains secrets (config tokens, agent bearer tokens, internal CA private key). A password is strongly recommended — it encrypts the file with AES-256-GCM using a scrypt-derived key.`,
    minLen:  _BACKUP_MIN_PW,
    confirm: 'Export',
    allowBlank: true,
  });
  if (password === null) return;  // cancelled
  _adminBackupLog(`exporting ${label}…`);
  let resp;
  try {
    resp = await fetch(ep.export, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password}),
    });
  } catch (e) {
    _adminBackupLog(`✗ ${label} export failed — ${e.message}`, 'err');
    return;
  }
  if (!resp.ok) {
    const txt = await resp.text();
    let err = txt;
    try { err = (JSON.parse(txt).error || JSON.parse(txt).detail || txt); } catch (_) {}
    _adminBackupLog(`✗ ${label} export failed — ${err}`, 'err');
    return;
  }
  const blob = await resp.blob();
  const cd   = resp.headers.get('Content-Disposition') || '';
  const m    = /filename="([^"]+)"/.exec(cd);
  const fname = m ? m[1] : `lsm-${component}.lsmenc`;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = fname;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
  const note = password ? 'encrypted' : 'NOT encrypted (no password)';
  _adminBackupLog(`✓ ${label} export downloaded — ${fname} (${blob.size} bytes, ${note})`, 'ok');
}

async function adminImportArchive(component) {
  const label = _BACKUP_LABELS[component] || component;
  const ep    = _BACKUP_ENDPOINTS[component];
  if (!ep) return;
  const picked = await _adminBackupFilePrompt({
    title:    `Import ${label} archive`,
    intro:    `Pick a previously-exported .lsmenc file. If encrypted, enter the password used at export. Nothing is written to disk until you confirm the preview.`,
  });
  if (!picked) return;
  const {file, password} = picked;
  _adminBackupLog(`previewing ${file.name}…`);

  const fd = new FormData();
  fd.append('file', file);
  fd.append('password', password);

  let resp, payload;
  try {
    resp = await fetch(ep.preview, {method:'POST', body: fd});
    payload = await resp.json();
  } catch (e) {
    _adminBackupLog(`✗ preview failed — ${e.message}`, 'err');
    return;
  }
  if (!resp.ok || !payload.ok) {
    const err = (payload && (payload.error || payload.detail)) || `HTTP ${resp.status}`;
    _adminBackupLog(`✗ preview failed — ${err}`, 'err');
    await _themedAlert({title:'Preview failed', bodyHtml: adminEsc(err), danger:true});
    return;
  }

  const confirmed = await _adminBackupConfirmImport({label, ep, payload});
  if (confirmed === null) { _adminBackupLog('import cancelled'); return; }
  const overrides = confirmed.overrides || {};
  const hostRemap = confirmed.hostRemap || {};
  const categories = confirmed.categories;  // null when archive has no category info

  _adminBackupLog(`applying ${label} import…`);
  let resp2, payload2;
  try {
    const fd2 = new FormData();
    fd2.append('file', file);
    fd2.append('password', password);
    if (Object.keys(overrides).length) {
      fd2.append('topology_overrides', JSON.stringify(overrides));
    }
    if (Object.keys(hostRemap).length) {
      fd2.append('host_remap', JSON.stringify(hostRemap));
    }
    if (Array.isArray(categories)) {
      // Send even an empty array — backend interprets [] as "import nothing"
      // (config files would also be skipped). Operator confirmed this in the
      // checkbox dialog, so honor their selection.
      fd2.append('categories', JSON.stringify(categories));
    }
    resp2 = await fetch(ep.apply, {method:'POST', body: fd2});
    payload2 = await resp2.json();
  } catch (e) {
    _adminBackupLog(`✗ apply failed — ${e.message}`, 'err');
    return;
  }
  if (!resp2.ok || !payload2.ok) {
    const err = (payload2 && (payload2.error || payload2.detail)) || `HTTP ${resp2.status}`;
    _adminBackupLog(`✗ apply failed — ${err}`, 'err');
    await _themedAlert({title:'Apply failed', bodyHtml: adminEsc(err), danger:true});
    return;
  }
  const patched = payload2.patched_toml_keys || [];
  const patchedNote = patched.length
    ? ` · patched ${patched.length} TOML key(s): ${patched.join(', ')}`
    : '';
  const hr = payload2.host_remap_applied || {};
  const hrNote = (hr.rules || hr.configs)
    ? ` · remapped hosts in ${hr.rules || 0} rule(s), ${hr.configs || 0} config(s)`
    : '';
  const written = payload2.written || [];
  const backups = payload2.backups || [];
  _adminBackupLog(`✓ ${label} import applied — ${written.length} files written${patchedNote}${hrNote}.`, 'ok');

  const writtenList = written.map(p =>
    `<li style="font-family:monospace;font-size:0.82em;">${adminEsc(p)}</li>`).join('');
  const backupList  = backups.map(p =>
    `<li style="font-family:monospace;font-size:0.78em;color:var(--fg-muted);">${adminEsc(p)}</li>`).join('');
  const patchedBlock = patched.length
    ? `<div style="margin:10px 0;font-size:0.85em;">` +
      `<strong>TOML keys patched:</strong> <code>${adminEsc(patched.join(', '))}</code></div>`
    : '';
  const extraStepsBlock = ep.extraSteps
    ? `<li>${ep.extraSteps.split('\n').map(line =>
        line.startsWith('sudo ')
          ? `<code style="display:block;margin-top:4px;background:var(--bg);padding:6px 8px;border-radius:4px;font-size:0.82em;">${adminEsc(line)}</code>`
          : adminEsc(line)
      ).join('<br>')}</li>`
    : '';
  await _themedAlert({
    title: `${label} import succeeded — next steps`,
    dismissable: false,
    bodyHtml:
      `<div style="font-size:0.88em;line-height:1.55;">` +
      `<p style="margin:0 0 10px;">The archive was unpacked and written to disk. ` +
      `Backups of the pre-import files are kept alongside each target.</p>` +
      patchedBlock +
      `<ol style="margin:8px 0;padding-left:22px;">` +
      `<li>Restart the service so the new state loads:<br>` +
      `<code style="display:block;margin-top:4px;background:var(--bg);padding:6px 8px;border-radius:4px;font-size:0.82em;">${adminEsc(ep.restartHint)}</code></li>` +
      extraStepsBlock +
      `<li>Verify the imported data is visible in the UI; if anything looks ` +
      `wrong, the <code>.preimport.${adminEsc(payload2.ts || '<ts>')}.bak</code> ` +
      `files below can be copied back into place.</li>` +
      `</ol>` +
      `<details style="margin-top:10px;"><summary style="cursor:pointer;font-size:0.85em;">Files written (${written.length})</summary>` +
      `<ul style="margin:6px 0;padding-left:22px;">${writtenList}</ul></details>` +
      (backupList ? `<details style="margin-top:4px;"><summary style="cursor:pointer;font-size:0.85em;">Backups created (${backups.length})</summary>` +
        `<ul style="margin:6px 0;padding-left:22px;">${backupList}</ul></details>` : '') +
      `</div>`,
    okLabel: 'Got it',
  });
}

// Confirm dialog for import — shows manifest, entry list, and an
// editable topology section when the server preview returned one
// (manager archives only). Resolves to {overrideKey: newValue} on
// apply, or null on cancel.
function _adminBackupConfirmImport({label, ep, payload}) {
  return new Promise(resolve => {
    const manifest = payload.manifest || {};
    const entries  = payload.entries  || [];
    const topology = payload.topology || {};
    const schema   = payload.topology_schema || [];
    const importCats = payload.import_categories || null;

    const metaLines = [];
    if (manifest.component)       metaLines.push(`<div>Component: <code>${adminEsc(manifest.component)}</code></div>`);
    if (manifest.manager_version) metaLines.push(`<div>Manager version (at export): <code>${adminEsc(manifest.manager_version)}</code></div>`);
    if (manifest.ae_version)      metaLines.push(`<div>AE version (at export): <code>${adminEsc(manifest.ae_version)}</code></div>`);
    if (manifest.hostname)        metaLines.push(`<div>Source host: <code>${adminEsc(manifest.hostname)}</code></div>`);
    if (manifest.created_at)      metaLines.push(`<div>Exported at: <code>${adminEsc(manifest.created_at)}</code></div>`);
    metaLines.push(`<div>Encrypted: <code>${payload.encrypted ? 'yes' : 'no'}</code></div>`);

    const rows = entries.map(e =>
      `<tr><td style="font-family:monospace;">${adminEsc(e.name)}</td>` +
      `<td style="text-align:right;font-variant-numeric:tabular-nums;">${e.size}</td></tr>`
    ).join('');

    // Manager imports come with a category breakdown: which files in the
    // archive belong to "config" (always-safe operator settings) vs
    // "identity" (CA + HMAC + agent registry — replaces this host's
    // cryptographic identity). Default-apply checks only "config" so a
    // routine "copy settings from dev" doesn't silently overwrite this
    // host's freshly-issued CA. Hidden entirely when the archive doesn't
    // declare categories (e.g. AE archives or older manager archives).
    let categoriesHtml = '';
    if (importCats && importCats.available && importCats.available.length) {
      const avail   = importCats.available;
      const apply   = new Set(importCats.default_apply || []);
      const labels  = importCats.labels || {};
      const descs   = importCats.descriptions || {};
      const catCounts = entries.reduce((m, e) => {
        m[e.category] = (m[e.category] || 0) + 1;
        return m;
      }, {});
      const rows2 = avail.map(c => {
        const checked = apply.has(c) ? 'checked' : '';
        const isIdentity = (c === 'identity');
        const accent = isIdentity ? 'var(--warn)' : 'var(--fg)';
        return `
          <label style="display:flex;gap:10px;padding:8px 10px;border:1px solid var(--border);
            border-radius:5px;margin-bottom:8px;cursor:pointer;align-items:flex-start;">
            <input type="checkbox" data-import-cat="${adminEsc(c)}" ${checked}
              style="margin-top:3px;flex-shrink:0;">
            <div style="flex:1;">
              <div style="font-weight:600;font-size:0.88em;color:${accent};">
                ${adminEsc(labels[c] || c)}
                <span style="font-weight:400;color:var(--fg-muted);font-size:0.82em;margin-left:6px;">
                  ${catCounts[c] || 0} file(s)
                </span>
              </div>
              <div style="font-size:0.78em;color:var(--fg-muted);margin-top:3px;line-height:1.4;">
                ${adminEsc(descs[c] || '')}
              </div>
            </div>
          </label>`;
      }).join('');
      categoriesHtml = `
        <details style="margin-top:14px;border:1px solid var(--border);border-radius:5px;padding:10px 12px;" open>
          <summary style="cursor:pointer;font-weight:600;font-size:0.9em;">
            What to import
            <span style="font-weight:400;color:var(--fg-muted);font-size:0.82em;margin-left:6px;">
              by default only Config is applied; Identity is opt-in
            </span>
          </summary>
          <div style="margin-top:10px;">${rows2}</div>
        </details>`;
    }

    let topologyHtml = '';
    if (payload.topology_error) {
      topologyHtml = `<div style="margin-top:12px;padding:10px 12px;border:1px solid var(--warn);` +
        `border-radius:5px;font-size:0.82em;color:var(--warn);">` +
        `Could not parse the captured llm-systems.toml: <code>${adminEsc(payload.topology_error)}</code>. ` +
        `Topology overrides are unavailable for this archive — edit the file by hand after import.</div>`;
    } else if (schema.length) {
      const fields = schema.map(s => {
        const captured = topology[s.key];
        const displayCaptured = captured === undefined ? '(not present in archive)' : String(captured);
        const inputVal = captured === undefined ? '' : String(captured);
        return `
          <div style="margin-bottom:10px;">
            <label style="display:block;font-size:0.80em;color:var(--fg);margin-bottom:3px;">${adminEsc(s.label)}</label>
            <div style="display:flex;align-items:center;gap:8px;">
              <input type="text" data-topkey="${adminEsc(s.key)}"
                value="${adminEsc(inputVal)}"
                placeholder="${adminEsc(displayCaptured)}"
                style="flex:1;padding:6px 8px;font-family:monospace;font-size:0.85em;
                  background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;">
              <span style="font-size:0.72em;color:var(--fg-muted);min-width:80px;">
                captured: <code style="color:var(--fg-muted);">${adminEsc(displayCaptured)}</code>
              </span>
            </div>
          </div>`;
      }).join('');
      topologyHtml = `
        <details style="margin-top:14px;border:1px solid var(--border);border-radius:5px;padding:10px 12px;" open>
          <summary style="cursor:pointer;font-weight:600;font-size:0.9em;">
            Topology overrides
            <span style="font-weight:400;color:var(--fg-muted);font-size:0.82em;margin-left:6px;">
              edit any value to rewrite it in the imported TOML
            </span>
          </summary>
          <div style="margin-top:10px;font-size:0.80em;color:var(--fg-muted);line-height:1.5;">
            These values were captured from the source manager's config. On a split-server migration
            the new AE and DB typically live at different IPs — edit the fields to match the new
            topology, and the TOML will be patched in-place before being written to disk.
          </div>
          <div style="margin-top:12px;" id="aecTopologyFields">${fields}</div>
        </details>`;
    }

    // Rule host remap (alarm-engine archives): rewrite source-host names so
    // imported rules/configs match this system's agents before the DB lands.
    let hostRemapHtml = '';
    const hostRemap = payload.host_remap || [];
    if (hostRemap.length) {
      const rmRows = hostRemap.map(h => {
        const usage = [];
        if (h.rules)   usage.push(`${h.rules} rule${h.rules === 1 ? '' : 's'}`);
        if (h.configs) usage.push(`${h.configs} config${h.configs === 1 ? '' : 's'}`);
        return `
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
            <code style="flex:0 0 38%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
              title="${adminEsc(h.host)}">${adminEsc(h.host)}</code>
            <span style="color:var(--fg-muted);">→</span>
            <input type="text" data-remaphost="${adminEsc(h.host)}" value="${adminEsc(h.host)}"
              style="flex:1;padding:6px 8px;font-family:monospace;font-size:0.85em;
                background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;">
            <span style="font-size:0.72em;color:var(--fg-muted);min-width:110px;">${usage.join(', ') || 'unused'}</span>
          </div>`;
      }).join('');
      hostRemapHtml = `
        <details style="margin-top:14px;border:1px solid var(--border);border-radius:5px;padding:10px 12px;" open>
          <summary style="cursor:pointer;font-weight:600;font-size:0.9em;">
            Rule host remap
            <span style="font-weight:400;color:var(--fg-muted);font-size:0.82em;margin-left:6px;">
              rewrite source-host names so imported rules match this system
            </span>
          </summary>
          <div style="margin-top:10px;font-size:0.80em;color:var(--fg-muted);line-height:1.5;">
            These host names are referenced by the imported rules and notification configs.
            Edit any that differ on this system (e.g. the manager / AE / DB agent names) —
            the rules database is rewritten before import. Leave a value unchanged to keep it as-is.
          </div>
          <div style="margin-top:12px;">${rmRows}</div>
        </details>`;
    }

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;'
      + 'display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px);';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
      + 'padding:20px 22px;width:min(680px,94vw);max-height:90vh;overflow:auto;color:var(--fg);'
      + 'font-family:system-ui,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    box.innerHTML = `
      <div style="font-size:1.05em;font-weight:600;margin-bottom:10px;">Apply ${adminEsc(label)} import?</div>
      <div style="font-size:0.85em;line-height:1.6;margin-bottom:10px;">${metaLines.join('')}</div>
      <div style="max-height:180px;overflow:auto;border:1px solid var(--border);border-radius:5px;">
        <table style="width:100%;border-collapse:collapse;font-size:0.85em;">
          <thead><tr style="background:var(--bg-card-alt);">
            <th style="text-align:left;padding:4px 8px;">Path</th>
            <th style="text-align:right;padding:4px 8px;">Bytes</th>
          </tr></thead>
          <tbody>${rows || '<tr><td colspan="2" style="padding:8px;color:var(--fg-muted);">(no entries)</td></tr>'}</tbody>
        </table>
      </div>
      ${categoriesHtml}
      ${topologyHtml}
      ${hostRemapHtml}
      <div style="margin-top:12px;font-size:0.82em;color:var(--warn);">
        Each file will be backed up to <code>&lt;path&gt;.preimport.&lt;ts&gt;.bak</code> before being overwritten.
        Restart required afterwards: <code>${adminEsc(ep.restartHint)}</code>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
        <button id="aecCancel" style="background:var(--bg-card-alt);color:var(--fg);border:1px solid var(--border);
          border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;">Cancel</button>
        <button id="aecApply" style="background:var(--crit);color:#fff;border:1px solid var(--border);
          border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">Apply import</button>
      </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const cleanup = (v) => { document.removeEventListener('keydown', key); overlay.remove(); resolve(v); };
    const key = (e) => { if (e.key === 'Escape') cleanup(null); };
    document.addEventListener('keydown', key);
    box.querySelector('#aecCancel').addEventListener('click', () => cleanup(null));
    box.querySelector('#aecApply').addEventListener('click', () => {
      const out = {};
      box.querySelectorAll('input[data-topkey]').forEach(inp => {
        const k = inp.getAttribute('data-topkey');
        const v = inp.value.trim();
        const captured = topology[k];
        // Only send fields the operator actually changed from the
        // captured value. Empty input + nothing captured = leave alone.
        if (v !== '' && String(captured ?? '') !== v) out[k] = v;
      });
      // Host remap: only send entries the operator changed from the old name.
      const remap = {};
      box.querySelectorAll('input[data-remaphost]').forEach(inp => {
        const oldHost = inp.getAttribute('data-remaphost');
        const v = inp.value.trim();
        if (v !== '' && v !== oldHost) remap[oldHost] = v;
      });
      // Category checkboxes: send the operator's selection so the backend
      // knows whether to write the identity files. Omitted when the archive
      // didn't carry any category info (AE archives, older manager archives).
      const cats = [];
      box.querySelectorAll('input[data-import-cat]').forEach(inp => {
        if (inp.checked) cats.push(inp.getAttribute('data-import-cat'));
      });
      cleanup({overrides: out, hostRemap: remap,
               categories: importCats ? cats : null});
    });
  });
}

function _adminBackupPasswordPrompt({title, intro, minLen, confirm, allowBlank}) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;'
      + 'display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px);';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
      + 'padding:20px 22px;min-width:420px;max-width:520px;color:var(--fg);'
      + 'font-family:system-ui,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    box.innerHTML = `
      <div style="font-size:1.05em;font-weight:600;margin-bottom:10px;">${title}</div>
      <div style="font-size:0.85em;color:var(--fg);margin-bottom:14px;line-height:1.5;">${intro}</div>
      <input id="bpwPw" type="password" autocomplete="new-password"
        placeholder="password (≥ ${minLen} chars)" style="width:100%;padding:8px 10px;
        background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:5px;
        font-family:monospace;font-size:0.95em;box-sizing:border-box;">
      <label style="display:block;margin-top:8px;font-size:0.82em;color:var(--fg-muted);">
        <input type="checkbox" id="bpwShow" style="margin-right:4px;"> show password
      </label>
      <div id="bpwHint" style="font-size:0.80em;color:var(--warn);min-height:1.2em;margin-top:6px;"></div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
        <button id="bpwCancel" style="background:var(--bg-card-alt);color:var(--fg);border:1px solid var(--border);
          border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;">Cancel</button>
        <button id="bpwOk" style="background:var(--accent);color:#fff;border:1px solid var(--border);
          border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">${confirm}</button>
      </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const pw = box.querySelector('#bpwPw');
    const sh = box.querySelector('#bpwShow');
    const hint = box.querySelector('#bpwHint');
    sh.addEventListener('change', () => { pw.type = sh.checked ? 'text' : 'password'; });
    pw.addEventListener('input', () => {
      if (!pw.value) {
        hint.textContent = allowBlank ? 'Leaving blank exports WITHOUT encryption.' : '';
        hint.style.color = allowBlank ? 'var(--warn)' : 'var(--fg-muted)';
      } else if (pw.value.length < minLen) {
        hint.textContent = `${minLen - pw.value.length} more characters required.`;
        hint.style.color = 'var(--warn)';
      } else {
        hint.textContent = '';
      }
    });
    const cleanup = (v) => { document.removeEventListener('keydown', key); overlay.remove(); resolve(v); };
    const key = (e) => {
      if (e.key === 'Escape') cleanup(null);
      else if (e.key === 'Enter') box.querySelector('#bpwOk').click();
    };
    document.addEventListener('keydown', key);
    box.querySelector('#bpwCancel').addEventListener('click', () => cleanup(null));
    box.querySelector('#bpwOk').addEventListener('click', () => {
      const v = pw.value;
      if (!v && !allowBlank) { hint.textContent = 'Password required.'; return; }
      if (v && v.length < minLen) { hint.textContent = `Password must be at least ${minLen} characters.`; return; }
      cleanup(v);
    });
    setTimeout(() => pw.focus(), 0);
  });
}

function _adminBackupFilePrompt({title, intro}) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;'
      + 'display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px);';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
      + 'padding:20px 22px;min-width:440px;max-width:560px;color:var(--fg);'
      + 'font-family:system-ui,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    box.innerHTML = `
      <div style="font-size:1.05em;font-weight:600;margin-bottom:10px;">${title}</div>
      <div style="font-size:0.85em;margin-bottom:14px;line-height:1.5;">${intro}</div>
      <input id="bfpFile" type="file" accept=".lsmenc,application/octet-stream"
        style="width:100%;padding:8px 0;color:var(--fg);font-size:0.88em;">
      <input id="bfpPw" type="password" autocomplete="off"
        placeholder="password (if encrypted)" style="width:100%;margin-top:10px;padding:8px 10px;
        background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:5px;
        font-family:monospace;font-size:0.95em;box-sizing:border-box;">
      <label style="display:block;margin-top:8px;font-size:0.82em;color:var(--fg-muted);">
        <input type="checkbox" id="bfpShow" style="margin-right:4px;"> show password
      </label>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px;">
        <button id="bfpCancel" style="background:var(--bg-card-alt);color:var(--fg);border:1px solid var(--border);
          border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;">Cancel</button>
        <button id="bfpOk" style="background:var(--accent);color:#fff;border:1px solid var(--border);
          border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">Preview</button>
      </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const fi = box.querySelector('#bfpFile');
    const pw = box.querySelector('#bfpPw');
    const sh = box.querySelector('#bfpShow');
    sh.addEventListener('change', () => { pw.type = sh.checked ? 'text' : 'password'; });
    const cleanup = (v) => { document.removeEventListener('keydown', key); overlay.remove(); resolve(v); };
    const key = (e) => {
      if (e.key === 'Escape') cleanup(null);
      else if (e.key === 'Enter') box.querySelector('#bfpOk').click();
    };
    document.addEventListener('keydown', key);
    box.querySelector('#bfpCancel').addEventListener('click', () => cleanup(null));
    box.querySelector('#bfpOk').addEventListener('click', () => {
      if (!fi.files || !fi.files[0]) return;
      cleanup({file: fi.files[0], password: pw.value || ''});
    });
    setTimeout(() => fi.focus(), 0);
  });
}

// Open a modal editor for the agent's on-disk agent_config.yaml. Reads
// the current text via GET /api/agents/<id>/config-file, lets the
// operator edit it in a textarea, then PUTs the new text back (which
// triggers a server-side backup + atomic rewrite). Changes do NOT take
// effect until the agent is restarted — the modal offers a Restart
// button on successful save.
async function adminEditConfig(aid) {
  const name = _adminAgentName(aid);
  _adminLog(`loading ${name} config…`);
  let initial;
  try {
    const r = await fetch(`/api/agents/${aid}/config-file`);
    initial = await r.json();
    if (!r.ok || !initial.ok) {
      const err = initial && initial.error ? initial.error : `HTTP ${r.status}`;
      _adminLog(`✗ load ${name} config failed — ${err}`, 'err');
      await _themedAlert({title:'Could not load config', bodyHtml: adminEsc(err), danger:true});
      return;
    }
  } catch (e) {
    _adminLog(`✗ load ${name} config failed — ${e.message}`, 'err');
    return;
  }

  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);'
    + 'z-index:9999;display:flex;align-items:center;justify-content:center;'
    + 'backdrop-filter:blur(4px);';
  const box = document.createElement('div');
  box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
    + 'padding:18px 20px;width:min(1200px,95vw);height:min(80vh,720px);display:flex;flex-direction:column;'
    + 'color:var(--fg);font-family:system-ui,-apple-system,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
  box.innerHTML = `
    <div style="font-size:1.05em;font-weight:600;margin-bottom:6px;">Edit ${adminEsc(name)} agent_config.yaml</div>
    <div style="font-size:0.80em;color:var(--fg-muted);margin-bottom:10px;font-family:monospace;">
      ${adminEsc(initial.path)} · ${initial.size} bytes
    </div>
    <textarea id="aecText" spellcheck="false" wrap="off" style="flex:1;width:100%;resize:none;
      font-family:'SFMono-Regular',ui-monospace,monospace;font-size:12.5px;line-height:1.45;
      background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;
      padding:10px;tab-size:2;overflow:auto;white-space:pre;"></textarea>
    <div id="aecStatus" style="font-size:0.80em;color:var(--fg-muted);margin-top:8px;min-height:1.2em;"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:10px;">
      <button id="aecCancel" style="background:var(--bg-card-alt);color:var(--fg);border:1px solid var(--border);
        border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;">Close</button>
      <button id="aecSave" style="background:var(--accent);color:#fff;border:1px solid var(--border);
        border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">Save (backup + write)</button>
    </div>`;
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  const ta     = box.querySelector('#aecText');
  const status = box.querySelector('#aecStatus');
  ta.value = initial.text || '';

  const cleanup = () => { document.removeEventListener('keydown', keyHandler); overlay.remove(); };
  const keyHandler = (e) => { if (e.key === 'Escape') cleanup(); };
  document.addEventListener('keydown', keyHandler);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(); });
  box.querySelector('#aecCancel').addEventListener('click', cleanup);
  box.querySelector('#aecSave').addEventListener('click', async () => {
    if (ta.value === initial.text) { status.textContent = 'No changes to save.'; return; }
    status.textContent = 'saving…';
    let resp, payload;
    try {
      resp = await fetch(`/api/agents/${aid}/config-file`, {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({text: ta.value, expected_mtime: initial.mtime}),
      });
      payload = await resp.json();
    } catch (e) {
      status.textContent = `✗ ${e.message}`;
      return;
    }
    if (!resp.ok || !payload.ok) {
      const err = payload && (payload.error || payload.detail) || `HTTP ${resp.status}`;
      status.innerHTML = `<span style="color:var(--crit);">✗ ${adminEsc(err)}</span>`;
      _adminLog(`✗ save ${name} config failed — ${err}`, 'err');
      return;
    }
    _adminLog(`✓ ${name} config saved (backup ${payload.backup_path}). Restart to apply.`);
    cleanup();
    const restart = await _themedConfirm({
      title:        `Restart ${adminEsc(name)} now?`,
      bodyHtml:     `Config saved. Backup at <code>${adminEsc(payload.backup_path)}</code>.<br>` +
                    `Changes take effect after restart.`,
      confirmLabel: 'Restart agent',
      cancelLabel:  'Later',
    });
    if (restart) adminRestart(aid);
  });
  setTimeout(() => ta.focus(), 0);
}

// Reusable themed yes/no modal. Resolves true on confirm, false on
// cancel/Escape/backdrop. bodyHtml is interpolated as innerHTML — callers
// are responsible for escaping any user-supplied substrings (use adminEsc()).
function _themedConfirm({ title, bodyHtml, confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false }) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);'
      + 'z-index:9999;display:flex;align-items:center;justify-content:center;'
      + 'backdrop-filter:blur(4px);';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
      + 'padding:20px 22px;min-width:380px;max-width:520px;color:var(--fg);'
      + 'font-family:system-ui,-apple-system,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    const confirmBg = danger ? 'var(--crit)' : 'var(--accent)';
    box.innerHTML = `
      <div style="font-size:1.05em;font-weight:600;margin-bottom:10px;color:var(--fg);">${title}</div>
      <div style="font-size:0.88em;color:var(--fg);margin-bottom:18px;line-height:1.5;">${bodyHtml}</div>
      <div style="display:flex;justify-content:flex-end;gap:8px;">
        <button id="tcCancel" style="background:var(--bg-card-alt);color:var(--fg);border:1px solid var(--border);
                border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;">${cancelLabel}</button>
        <button id="tcConfirm" style="background:${confirmBg};color:#fff;border:1px solid var(--border);
                border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">${confirmLabel}</button>
      </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const cleanup = (v) => {
      document.removeEventListener('keydown', keyHandler);
      overlay.remove();
      resolve(v);
    };
    const keyHandler = (e) => {
      if (e.key === 'Escape') cleanup(false);
      else if (e.key === 'Enter') cleanup(true);
    };
    document.addEventListener('keydown', keyHandler);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(false); });
    box.querySelector('#tcCancel').addEventListener('click', () => cleanup(false));
    box.querySelector('#tcConfirm').addEventListener('click', () => cleanup(true));
    // Focus the confirm button so Enter works without an explicit tab.
    setTimeout(() => box.querySelector('#tcConfirm').focus(), 0);
  });
}

// Themed alert (single OK button). Shares the look of _themedConfirm so
// error/info popups stop falling back to the unstyled native alert().
function _themedAlert({ title, bodyHtml, okLabel = 'OK', danger = false, dismissable = true }) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);'
      + 'z-index:9999;display:flex;align-items:center;justify-content:center;'
      + 'backdrop-filter:blur(4px);';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
      + 'padding:20px 22px;min-width:380px;max-width:520px;color:var(--fg);'
      + 'font-family:system-ui,-apple-system,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    const okBg = danger ? 'var(--crit)' : 'var(--accent)';
    box.innerHTML = `
      <div style="font-size:1.05em;font-weight:600;margin-bottom:10px;color:var(--fg);">${title}</div>
      <div style="font-size:0.88em;color:var(--fg);margin-bottom:18px;line-height:1.5;white-space:pre-wrap;">${bodyHtml}</div>
      <div style="display:flex;justify-content:flex-end;gap:8px;">
        <button id="taOk" style="background:${okBg};color:#fff;border:1px solid var(--border);
                border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">${okLabel}</button>
      </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const cleanup = () => {
      document.removeEventListener('keydown', keyHandler);
      overlay.remove();
      resolve();
    };
    const keyHandler = (e) => {
      if (e.key === 'Escape' || e.key === 'Enter') cleanup();
    };
    document.addEventListener('keydown', keyHandler);
    if (dismissable) {
      overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(); });
    }
    box.querySelector('#taOk').addEventListener('click', cleanup);
    setTimeout(() => box.querySelector('#taOk').focus(), 0);
  });
}

// Themed text-input prompt. Resolves to the trimmed string, or null on
// cancel/escape/empty. Replaces the unstyled native prompt(). Pass a static or
// pre-escaped `title`/`bodyHtml` (set via innerHTML); `value` is set DOM-safe.
function _themedPrompt({ title, bodyHtml = '', value = '', placeholder = '', confirmLabel = 'OK', cancelLabel = 'Cancel', maxLength = 64, inputType = 'text' }) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);'
      + 'z-index:9999;display:flex;align-items:center;justify-content:center;'
      + 'backdrop-filter:blur(4px);';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
      + 'padding:20px 22px;min-width:380px;max-width:520px;color:var(--fg);'
      + 'font-family:system-ui,-apple-system,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    box.innerHTML = `
      <div style="font-size:1.05em;font-weight:600;margin-bottom:10px;color:var(--fg);">${title}</div>
      ${bodyHtml ? `<div style="font-size:0.88em;color:var(--fg);margin-bottom:12px;line-height:1.5;">${bodyHtml}</div>` : ''}
      <input id="tpInput" type="text" maxlength="${maxLength}"
             style="width:100%;box-sizing:border-box;background:var(--bg-card-alt);color:var(--fg);
             border:1px solid var(--border);border-radius:5px;padding:8px 10px;font-size:0.9em;margin-bottom:16px;">
      <div style="display:flex;justify-content:flex-end;gap:8px;">
        <button id="tpCancel" style="background:var(--bg-card-alt);color:var(--fg);border:1px solid var(--border);
                border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;">${cancelLabel}</button>
        <button id="tpConfirm" style="background:var(--accent);color:#fff;border:1px solid var(--border);
                border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">${confirmLabel}</button>
      </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const input = box.querySelector('#tpInput');
    input.value = value;
    input.placeholder = placeholder;
    input.type = inputType;  // DOM-safe set; 'password' masks credential prompts
    const cleanup = (v) => { document.removeEventListener('keydown', keyHandler); overlay.remove(); resolve(v); };
    const submit = () => cleanup(input.value.trim() || null);
    const keyHandler = (e) => {
      if (e.key === 'Escape') cleanup(null);
      else if (e.key === 'Enter') submit();
    };
    document.addEventListener('keydown', keyHandler);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(null); });
    box.querySelector('#tpCancel').addEventListener('click', () => cleanup(null));
    box.querySelector('#tpConfirm').addEventListener('click', submit);
    setTimeout(() => { input.focus(); input.select(); }, 0);
  });
}

// Brief, non-blocking, auto-dismissing themed notification (success/info/error).
// message is set via textContent — safe to pass user-supplied text.
function _themedToast(message, { kind = 'ok', ms = 2600 } = {}) {
  const accent = kind === 'err' ? 'var(--crit)' : (kind === 'warn' ? 'var(--warn)' : 'var(--accent)');
  const t = document.createElement('div');
  t.style.cssText = 'position:fixed;bottom:22px;left:50%;transform:translateX(-50%);z-index:10000;'
    + 'background:var(--bg-card);color:var(--fg);border:1px solid var(--border);border-left:3px solid '
    + accent + ';border-radius:6px;padding:10px 16px;font-family:system-ui,-apple-system,sans-serif;'
    + 'font-size:0.88em;box-shadow:0 6px 24px rgba(0,0,0,0.4);max-width:80vw;opacity:0;transition:opacity 0.15s;';
  t.textContent = message;
  document.body.appendChild(t);
  requestAnimationFrame(() => { t.style.opacity = '1'; });
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 200); }, ms);
}

// Self-update — opens a floating panel, streams the install.sh output
// via SSE, and refreshes the agent list when the agent comes back from
// its restart. Uses fetch+ReadableStream rather than EventSource so we
// can issue POST (EventSource only does GET).
async function adminUpdate(aid) {
  const name = _adminAgentName(aid);
  const agent = (_adminAgentsCache || []).find(a => a.agent_id === aid) || {};
  const curV = agent.version || '?';
  const newV = _latestAgentVersion || '?';
  const ok = await _themedConfirm({
    title: `Self-update ${adminEsc(name)}?`,
    bodyHtml:
      `<div style="font-family:monospace;background:var(--bg);border:1px solid var(--border);` +
      `border-radius:6px;padding:10px 12px;margin-bottom:12px;">` +
      `<div><span style="color:var(--fg-muted);">Current:</span> ${adminEsc(curV)}</div>` +
      `<div><span style="color:var(--fg-muted);">New:</span>     ${adminEsc(newV)}</div>` +
      `</div>` +
      `<div>The agent will fetch the new code, install it, and restart (~5s).</div>`,
    confirmLabel: 'Update',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;

  _adminUpdateOpen(name);
  _adminUpdateLog(`Updating ${curV} → ${newV}`, 'stage');

  let r;
  try {
    r = await fetch(`/api/agents/${aid}/self-update`, { method: 'POST' });
  } catch (e) {
    _adminUpdateLog(`✗ request failed: ${e.message}`, 'err');
    return;
  }
  if (!r.ok) {
    let body = '';
    try { body = await r.text(); } catch {}
    _adminUpdateLog(`✗ HTTP ${r.status}: ${body.slice(0, 500)}`, 'err');
    return;
  }
  if (!r.body) {
    _adminUpdateLog('✗ no response body — proxy did not stream', 'err');
    return;
  }

  // Parse SSE frames out of the streamed body.
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let doneOk = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = frame.split('\n').filter(l => l.startsWith('data:'))
                        .map(l => l.slice(5).trim()).join('');
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      if (msg.line) {
        // Suppress version_before/version_to passthrough lines — the
        // version transition is already shown once at the top of the
        // panel. Agent emits both lines; manager proxy also synthesizes
        // a version_to line for older agents (3 lines, 1 duplicate).
        if (/^\s*version_(before|to):/i.test(msg.line)) continue;
        _adminUpdateLog(msg.line);
      } else if (msg.blank) {
        _adminUpdateLog('', 'blank');
      } else if (msg.stage === 'done') {
        doneOk = msg.ok;
        // Versions were already shown at the top of the panel; don't repeat
        // them. Just report success/failure + rc and any backend message.
        const head = msg.ok ? '✓ done' : '✗ done';
        const tail = msg.msg ? ` — ${msg.msg}` : '';
        _adminUpdateLog(`${head} (rc=${msg.rc ?? '?'})${tail}`, msg.ok ? 'ok' : 'err');
      } else if (msg.stage) {
        _adminUpdateLog(`── ${msg.stage}: ${msg.msg || ''}`, 'stage');
      }
    }
  }
  if (doneOk === true) {
    _adminUpdateLog('agent SIGTERM-ing for restart; refreshing agent list in 5s…');
    // Refresh the agent list but leave the panel open so the operator
    // can read the full output. Operator closes it via the X.
    setTimeout(() => { adminLoadAgents(); }, 5000);
  } else if (doneOk === false) {
    _adminUpdateLog('install failed; agent kept running with old code', 'err');
  } else {
    _adminUpdateLog('stream ended without a `done` frame', 'err');
  }
}

// ── Agent log viewer (streams /api/agents/<id>/log/stream) ────────────
let _adminLogEventSrc = null;
let _adminLogPaused = false;

async function adminLogs(aid) {
  const name = _adminAgentName(aid);
  _adminLogsClose();  // close any existing stream
  _adminLogsOpen(name);

  // Seed with tail
  _adminLogsAppend('── fetching tail…', 'meta');
  try {
    const r = await fetch(`/api/agents/${aid}/log/tail`);
    if (r.ok) {
      const d = await r.json();
      if (d.path) _adminLogsAppend(`── ${d.path}`, 'meta');
      if (d.note) _adminLogsAppend(`── ${d.note}`, 'meta');
      (d.lines || []).forEach(l => _adminLogsAppend(l));
      _adminLogsAppend('── streaming new lines (Pause to stop scroll) ──', 'meta');
    } else {
      _adminLogsAppend(`tail fetch HTTP ${r.status}`, 'err');
    }
  } catch (e) {
    _adminLogsAppend(`tail fetch failed: ${e.message}`, 'err');
  }

  // Open SSE stream
  try {
    const es = new EventSource(`/api/agents/${aid}/log/stream`);
    _adminLogEventSrc = es;
    es.onmessage = (ev) => {
      if (_adminLogPaused) return;
      try {
        const msg = JSON.parse(ev.data);
        if (msg.line !== undefined) _adminLogsAppend(msg.line);
        else if (msg.error) _adminLogsAppend(`stream error: ${msg.error}`, 'err');
      } catch { /* keepalives etc. */ }
    };
    es.onerror = () => {
      _adminLogsAppend('── stream disconnected', 'err');
    };
  } catch (e) {
    _adminLogsAppend(`stream open failed: ${e.message}`, 'err');
  }
}

function _adminLogsClose() {
  if (_adminLogEventSrc) {
    try { _adminLogEventSrc.close(); } catch {}
    _adminLogEventSrc = null;
  }
  const p = document.getElementById('adminLogsPanel');
  if (p) p.style.display = 'none';
}

function _adminLogsTogglePause() {
  _adminLogPaused = !_adminLogPaused;
  const btn = document.getElementById('adminLogsPauseBtn');
  if (btn) btn.textContent = _adminLogPaused ? 'Resume' : 'Pause';
}

function _adminLogsClear() {
  const body = document.getElementById('adminLogsBody');
  if (body) body.textContent = '';
}

function _adminLogsOpen(label) {
  let p = document.getElementById('adminLogsPanel');
  if (!p) {
    p = document.createElement('div');
    p.id = 'adminLogsPanel';
    p.style.cssText = `
      position:fixed; left:24px; bottom:24px; width:760px; max-width:90vw;
      max-height:60vh; background:var(--bg); border:1px solid var(--border);
      border-radius:6px; box-shadow:0 8px 30px color-mix(in srgb, var(--bg) 60%, transparent); z-index:999;
      display:flex; flex-direction:column; font-family:monospace; font-size:12px;
    `;
    p.innerHTML = `
      <div style="background:var(--bg-card); padding:8px 12px; display:flex; align-items:center; gap:10px; border-radius:6px 6px 0 0;">
        <div id="adminLogsTitle" style="flex:1; color:var(--fg); font-weight:600;">Agent log</div>
        <button id="adminLogsPauseBtn" onclick="_adminLogsTogglePause()"
                style="background:var(--bg-card-alt); border:none; color:var(--fg); cursor:pointer; padding:4px 10px; border-radius:3px;">Pause</button>
        <button onclick="_adminLogsClear()"
                style="background:var(--bg-card-alt); border:none; color:var(--fg); cursor:pointer; padding:4px 10px; border-radius:3px;">Clear</button>
        <button onclick="_adminLogsClose()" style="background:none; border:none; color:var(--fg-muted); cursor:pointer; font-size:16px;">×</button>
      </div>
      <div id="adminLogsBody" style="flex:1; overflow-y:auto; padding:10px 12px; color:var(--fg); white-space:pre-wrap; word-break:break-all; line-height:1.4;"></div>`;
    document.body.appendChild(p);
  }
  p.style.display = 'flex';
  document.getElementById('adminLogsTitle').textContent = `Agent log — ${label}`;
  document.getElementById('adminLogsBody').textContent = '';
  _adminLogPaused = false;
  const btn = document.getElementById('adminLogsPauseBtn');
  if (btn) btn.textContent = 'Pause';
}

function _adminLogsAppend(text, level) {
  const body = document.getElementById('adminLogsBody');
  if (!body) return;
  const line = document.createElement('div');
  line.style.color = level === 'err' ? 'var(--crit)'
                    : level === 'meta' ? 'var(--accent-2)' : 'var(--fg)';
  line.textContent = text;
  body.appendChild(line);
  // Auto-scroll only if user is already near the bottom — preserves their
  // scroll position when they're reviewing earlier lines.
  const nearBottom = (body.scrollHeight - body.scrollTop - body.clientHeight) < 60;
  if (nearBottom) body.scrollTop = body.scrollHeight;
}

// ── Self-update output panel (one shared instance) ────────────────────
let _adminUpdatePanel = null;
function _adminUpdateOpen(label) {
  let p = document.getElementById('adminUpdatePanel');
  if (!p) {
    p = document.createElement('div');
    p.id = 'adminUpdatePanel';
    p.style.cssText = `
      position:fixed; right:24px; bottom:24px; width:760px; max-width:90vw;
      max-height:60vh; background:var(--bg); border:1px solid var(--border);
      border-radius:6px; box-shadow:0 8px 30px rgba(0,0,0,0.6); z-index:1000;
      display:flex; flex-direction:column; font-family:monospace; font-size:12px;
      color:var(--fg);
    `;
    p.innerHTML = `
      <div style="background:var(--bg-card); padding:8px 12px; display:flex; align-items:center; gap:10px; border-radius:6px 6px 0 0; border-bottom:1px solid var(--border);">
        <div id="adminUpdateTitle" style="flex:1; color:var(--fg); font-weight:600;">Self-update</div>
        <button onclick="_adminUpdateClose()" style="background:none; border:none; color:var(--fg-muted); cursor:pointer; font-size:16px;">×</button>
      </div>
      <div id="adminUpdateLog" style="flex:1; overflow-y:auto; padding:10px 12px; color:var(--fg); white-space:pre-wrap; word-break:break-all;"></div>`;
    document.body.appendChild(p);
  }
  p.style.display = 'flex';
  document.getElementById('adminUpdateTitle').textContent = `Self-update — ${label}`;
  document.getElementById('adminUpdateLog').textContent = '';
  _adminUpdatePanel = p;
}
function _adminUpdateClose() {
  if (_adminUpdatePanel) _adminUpdatePanel.style.display = 'none';
}
function _adminUpdateLog(msg, level) {
  const log = document.getElementById('adminUpdateLog');
  if (!log) return;
  // Blank-line frames from install.sh become visual separators with no
  // timestamp prefix — keeps the section spacing legible.
  if (level === 'blank') {
    const sep = document.createElement('div');
    sep.innerHTML = '&nbsp;';
    log.appendChild(sep);
    log.scrollTop = log.scrollHeight;
    return;
  }
  // Auto-highlight the new-config-keys banner emitted by install.sh so
  // operators don't miss newly-added agent_config.yaml options.
  if (!level && typeof msg === 'string' && /^\s*(╔|║|╚|\+\+\+ added \d+ new key|⚠ commented out \d+ key)/.test(msg)) {
    level = 'stage';
  }
  const ts = new Date().toLocaleTimeString();
  const color = level === 'err' ? 'var(--crit)'
              : level === 'stage' ? 'var(--accent)'
              : level === 'ok' ? 'var(--ok)'
              : 'var(--fg)';
  const line = document.createElement('div');
  line.style.color = color;
  if (level === 'stage' && /^\s*(╔|║|╚|\+\+\+)/.test(msg)) {
    line.style.fontWeight = '600';
  }
  line.textContent = `[${ts}] ${msg}`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}
async function adminToggleCollection(aid, enabled) {
  const name = _adminAgentName(aid);
  const verb = enabled ? 'resuming' : 'pausing';
  _adminLog(`${verb} collection on ${name}…`);
  const r = await fetch(`/api/agents/${aid}/collection`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({enabled})
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok && d.ok) {
    _adminLog(`✓ ${name} collection is now ${enabled ? 'ON' : 'PAUSED'}`);
  } else {
    const tried = (d.tried || []).join(' → ');
    _adminLog(`✗ collection toggle on ${name} failed (HTTP ${r.status}) — ${d.error || 'unknown error'}${tried ? '   tried: ' + tried : ''}`, 'err');
  }
  setTimeout(adminLoadAgents, 800);
}
async function adminToggleAuth(disabled) {
  const r = await fetch('/api/agents/global', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({auth_disabled: !!disabled})
  });
  if (r.ok) {
    _adminLog(`✓ agent auth ${disabled ? 'disabled (trusted-LAN mode)' : 'enabled'} globally`);
  } else {
    _adminLog(`✗ global auth toggle failed (HTTP ${r.status})`, 'err');
  }
}

async function adminPushCaToAgents() {
  // Confirm because this rewrites the cert + key + CA on every approved
  // agent (via heartbeat ack), even ones where TLS is already working.
  // Cheap-but-not-free.
  const ok = await _themedConfirm({
    title: 'Push CA to all approved agents?',
    bodyHtml: 'Every approved agent will receive a fresh cert + key + CA ' +
              'bundle on its next heartbeat (≤60s). Use this after rotating ' +
              'the manager\'s internal CA so agents pick up the new trust ' +
              'root without manual revoke + re-approve.<br><br>' +
              'No downtime; agents stay running. The ↔ TLS badge confirms ' +
              'each one has the new CA AND re-probed the manager\'s HTTPS.',
    confirmLabel: 'Push CA',
  });
  if (!ok) return;
  let r, payload;
  try {
    r = await fetch('/api/admin/push-ca-to-agents', {method: 'POST'});
    payload = await r.json();
  } catch (e) {
    _adminLog(`✗ push-CA failed — ${e.message}`, 'err');
    return;
  }
  if (!r.ok || !payload.ok) {
    const err = (payload && (payload.error || payload.detail)) || `HTTP ${r.status}`;
    _adminLog(`✗ push-CA failed — ${err}`, 'err');
    return;
  }
  const fp = (payload.ca_fingerprint_sha256 || '').slice(0, 16) || '?';
  _adminLog(`✓ marked ${payload.marked_count} agent(s) for CA refresh ` +
            `(CA fp=${fp}); bundles land on next heartbeat (≤60s)`);
}

// ── Users management (multi-user, #125) ─────────────────────────────────────
async function adminUsersLoad() {
  const st = document.getElementById('adminUsersStatus');
  const tb = document.getElementById('adminUsersTbody');
  _adminUsersBindOnce();
  try {
    const r = await fetch('/api/admin/users');
    if (!r.ok) { if (st) st.textContent = 'admin-only'; return; }
    const d = await r.json();
    if (st) st.textContent = (d.users || []).length + ' user(s)';
    tb.innerHTML = (d.users || []).map(_adminUserRow).join('') ||
      '<tr><td colspan="5" style="padding:14px;text-align:center;color:var(--fg-muted);">No users</td></tr>';
  } catch (e) { if (st) st.textContent = 'load failed'; }
}

// Delegated handler: action buttons carry data-* attrs (no inline onclick), so a
// username can never reach a JS-eval context — the row builder stays injection-free.
function _adminUsersBindOnce() {
  const tb = document.getElementById('adminUsersTbody');
  if (!tb || tb._uBound) return;
  tb._uBound = true;
  tb.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-uact]');
    if (!btn) return;
    const user = btn.getAttribute('data-user') || '';
    const arg = btn.getAttribute('data-arg') || '';
    switch (btn.getAttribute('data-uact')) {
      case 'role':    adminUserSetRole(user, arg); break;
      case 'disable': adminUserToggleDisabled(user, arg === 'true'); break;
      case 'resetpw': adminUserResetPw(user); break;
      case 'unlock':  adminUserUnlock(user); break;
      case 'delete':  adminUserDelete(user); break;
    }
  });
}

function _adminUserRow(u) {
  const name = adminEsc(u.username);
  const role = u.role === 'admin' ? 'Admin' : 'Operator';
  const status = u.disabled ? '<span class="status status--muted status--square">disabled</span>'
                            : (u.locked ? '<span class="status status--crit status--square">locked</span>'
                                        : '<span class="status status--ok status--square">active</span>');
  const last = u.last_login ? adminEsc(String(u.last_login).replace('T', ' ').slice(0, 19)) : '—';
  const toggleRole = u.role === 'admin' ? 'operator' : 'admin';
  const dis = u.disabled ? 'false' : 'true';
  return `<tr>
    <td>${name}</td><td>${role}</td><td>${status}</td><td>${last}</td>
    <td style="text-align:right;white-space:nowrap;">
      <button class="adm-mini" data-uact="role" data-user="${name}" data-arg="${toggleRole}">→ ${toggleRole === 'admin' ? 'Admin' : 'Operator'}</button>
      <button class="adm-mini" data-uact="disable" data-user="${name}" data-arg="${dis}">${u.disabled ? 'Enable' : 'Disable'}</button>
      <button class="adm-mini" data-uact="resetpw" data-user="${name}">Reset pw</button>
      ${u.locked ? `<button class="adm-mini" data-uact="unlock" data-user="${name}">Unlock</button>` : ''}
      <button class="adm-mini danger" data-uact="delete" data-user="${name}">Delete</button>
    </td></tr>`;
}

async function _adminUsersApi(url, opts, okMsg) {
  try {
    const r = await fetch(url, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { _themedToast(d.error || ('HTTP ' + r.status), { kind: 'err' }); return false; }
    if (okMsg) _themedToast(okMsg, { kind: 'ok' });
    adminUsersLoad();
    return true;
  } catch (e) { _themedToast('request failed', { kind: 'err' }); return false; }
}

async function adminUserCreate() {
  const u = document.getElementById('adminUserNew').value.trim();
  const pw = document.getElementById('adminUserNewPw').value;
  const role = document.getElementById('adminUserNewRole').value;
  if (!u || !pw) { _themedToast('username and password required', { kind: 'warn' }); return; }
  const ok = await _adminUsersApi('/api/admin/users',
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: pw, role }) },
    'User "' + u + '" created');
  if (ok) { document.getElementById('adminUserNew').value = ''; document.getElementById('adminUserNewPw').value = ''; }
}

function adminUserSetRole(name, role) {
  _adminUsersApi('/api/admin/users/' + encodeURIComponent(name),
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role }) },
    name + ' → ' + role);
}

function adminUserToggleDisabled(name, disabled) {
  _adminUsersApi('/api/admin/users/' + encodeURIComponent(name),
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ disabled }) },
    name + (disabled ? ' disabled' : ' enabled'));
}

async function adminUserResetPw(name) {
  const pw = await _themedPrompt({ title: 'Reset password', bodyHtml: 'New password for ' + adminEsc(name) + ' (min 8):', placeholder: 'new password', inputType: 'password' });
  if (pw === null) return;
  if (pw.length < 8) { _themedToast('password too short', { kind: 'warn' }); return; }
  _adminUsersApi('/api/admin/users/' + encodeURIComponent(name),
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw }) },
    'Password reset for ' + name);
}

function adminUserUnlock(name) {
  _adminUsersApi('/api/admin/users/' + encodeURIComponent(name) + '/unlock', { method: 'POST' }, name + ' unlocked');
}

async function adminUserDelete(name) {
  const ok = await _themedConfirm({ title: 'Delete user?', bodyHtml: 'Delete ' + adminEsc(name) + '? This cannot be undone.', confirmLabel: 'Delete', danger: true });
  if (!ok) return;
  _adminUsersApi('/api/admin/users/' + encodeURIComponent(name), { method: 'DELETE' }, name + ' deleted');
}
