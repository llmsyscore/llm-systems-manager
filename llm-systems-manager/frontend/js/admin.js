// Admin tab auto-refresh — only ticks when the tab is visible.
let _adminRefreshTimer = null;
function adminStartAutoRefresh() {
  // The gateway poll follows the Gateway sub-tab (key 'routing'); the stop mirrors it.
  if (window.GatewayView && typeof _subTabState !== 'undefined' && _subTabState.admin === 'routing') {
    GatewayView.start();
  }
  if (_adminRefreshTimer) return;
  // 20s cadence (was 10s) — paired with the backend's anti-flap
  // (requires 2 consecutive failed alarm-engine probes before flipping to
  // DOWN). Slower polling reduces the chance of a transient slow probe
  // landing on the dashboard while still surfacing real outages quickly.
  _adminRefreshTimer = LivePause.every(_adminRefreshTick, 20000);
}

// One auto-refresh tick; the audit ledger refreshes in place while its
// sub-tab is visible (the module skips while a detail panel is open).
function _adminRefreshTick() {
  if (_activeTab !== 'admin') return;
  adminRefreshNow();
}
// Refresh every Admin panel now; also the header ↻ stamp's click handler.
function adminRefreshNow() {
  adminLoadAgents(); adminLoadHealth();
  if (typeof _subTabState !== 'undefined' && _subTabState.admin === 'audit'
      && typeof adminAuditLoad === 'function') adminAuditLoad();
}
function adminStopAutoRefresh() {
  if (window.GatewayView) GatewayView.stop();
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
  if (typeof _themedToast === 'function') {
    _themedToast(msg, { kind: level === 'err' ? 'err' : level === 'warn' ? 'warn' : 'ok', ms: level === 'err' ? 9000 : 4500 });
  }
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
    const [r, rel] = await Promise.all([
      fetch('/api/admin/system-health'), _adminFetchRelease(),
    ]);
    if (!r.ok) return;
    if (window.HealthView) HealthView.render(await r.json(), rel);
  } catch (e) {
    /* keep last successful render */
  }
}

// Release info via the companion endpoint (server caches GitHub for 24 h);
// cached 5 min here — failures included — and deduped across callers.
let _adminRelCache = { at: 0, data: null };
let _adminRelInflight = null;
function _adminFetchRelease() {
  if (Date.now() - _adminRelCache.at < 300000) return Promise.resolve(_adminRelCache.data);
  if (!_adminRelInflight) {
    _adminRelInflight = (async () => {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 10000);
      // A failed fetch keeps the last good payload, else marks the endpoint unreachable.
      const markUnreachable = () => {
        _adminRelCache = { at: Date.now(), data: _adminRelCache.data || { unreachable: true } };
      };
      try {
        const r = await fetch('/api/companion/release', { signal: ctl.signal });
        if (r.ok) _adminRelCache = { at: Date.now(), data: await r.json() };
        else markUnreachable();
      } catch (_) {
        markUnreachable();
      } finally {
        clearTimeout(t);
        _adminRelInflight = null;
      }
      return _adminRelCache.data;
    })();
  }
  return _adminRelInflight;
}
function _adminUpdateAvailable(rel) {
  return !!(rel && rel.enabled && rel.update_available === true);
}

// ── Tab status dots (Events / Admin) ──────────────────────────────────
// Driven globally so the dots reflect live state on any tab. Events turns
// red while a critical alert is active (cleared → green); Admin turns red
// when the system-health roll-up is anything but "ok". A failed/forbidden
// fetch leaves the dot at its prior state rather than flapping to muted.
function _setTabDot(id, state) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('ok', 'alert', 'warn');
  if (state === 'ok' || state === 'alert' || state === 'warn') el.classList.add(state);
}
async function refreshTabIndicators() {
  // Events dot + Overall alerts strip share one active-alerts pull (#565).
  // status=active excludes acknowledged (being handled ≠ red dot).
  (async () => {
    try {
      const r = await fetch('/api/alarm/alerts/?status=active&limit=50');
      if (!r.ok) return;
      const arr = await r.json();
      window._activeAlerts = Array.isArray(arr) ? arr : [];
      const crit = window._activeAlerts.some(a => a && a.severity === 'critical');
      _setTabDot('tabDotEvents', crit ? 'alert' : 'ok');
    } catch (_) { /* keep prior state */ }
  })();
  // Admin — system-health roll-up mapped to the dot (down → red,
  // warn → amber, ok → green), and amber when a newer release is available.
  (async () => {
    if (window._me && window._me.admin_access === false) { _setTabDot('tabDotAdmin', 'ok'); return; }
    try {
      const r = await fetch('/api/admin/system-health');
      if (!r.ok) return;
      const d = await r.json();
      if (d.overall === 'warn') { _setTabDot('tabDotAdmin', 'warn'); return; }
      if (d.overall !== 'ok') { _setTabDot('tabDotAdmin', 'alert'); return; }
      const rel = await _adminFetchRelease();
      _setTabDot('tabDotAdmin', _adminUpdateAvailable(rel) ? 'warn' : 'ok');
    } catch (_) { /* keep prior state */ }
  })();
}

// Muted info row when the release check is unreachable, errored, or returned
// no verdict; empty when disabled, up to date, or an update is available.
function _adminReleaseInfoText(rel) {
  if (!rel) return '';
  if (rel.unreachable) return 'Release check: endpoint unreachable';
  if (!rel.enabled || rel.update_available === true) return '';
  if (rel.error) return `Release check: check failed: ${rel.error}`;
  if (rel.update_available === null) return `Release check: ${rel.note || 'no verdict'}`;
  return '';
}

// Same verdict, rendered as the System Health card's `.w.info` row.
function _adminReleaseInfoHtml(rel) {
  const msg = _adminReleaseInfoText(rel);
  if (!msg) return '';
  return `<div class="w info"><span class="g">i</span><div>${adminEsc(msg)}</div></div>`;
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

// One mc-seg drives the pool order and the pins card (#797).
function adminRenderProviderSeg() {
  const el = document.getElementById('rtProviderSeg');
  if (!el) return;
  el.innerHTML = _adminPoolProviders.map(p =>
    `<button type="button" class="${p.name === _adminProvSel ? 'on' : ''}" data-prov="${adminEsc(p.name)}">${adminEsc(p.label || p.name)}</button>`
  ).join('');
  if (!el._provBound) {
    el._provBound = true;
    el.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-prov]');
      if (b) adminSelectProvider(b.getAttribute('data-prov'));
    });
  }
}

function adminSelectProvider(name) {
  if (!name || name === _adminProvSel) return;
  _adminProvSel = name;
  adminRenderPoolOrder();
  adminRenderPins();
  adminLoadProviderModels();
  adminRenderRoutingSummary();
}

// Routing header summary: gateway/autopilot state plus pool, pin and proposal counts.
function adminRenderRoutingSummary() {
  const el = document.getElementById('rtSummary');
  if (!el) return;
  const gw = (window.GatewayView && GatewayView.last()) || null;
  const ap = (window.AP && AP.state && AP.state()) || ((_adminGlobal || {}).autopilot) || null;
  const props = (window.AP && AP.proposals && AP.proposals().length) || 0;
  const prov = _adminPoolProviders.find(p => p.name === _adminProvSel) || _adminPoolProviders[0] || {};
  const pool = ((_adminGlobal || {})[_adminProvSel + '_pool'] || []).length;
  const pins = Object.keys((prov.pin_key && (_adminGlobal || {})[prov.pin_key]) || {}).length;
  const onOff = v => `<b class="${v ? 'ok' : 'warn'}">${v ? 'on' : 'off'}</b>`;
  const parts = [];
  if (gw) parts.push(`<span>gateway ${onOff(gw.enabled)}</span>`);
  parts.push(`<span>autopilot ${onOff(ap && ap.enabled)}</span>`);
  parts.push(`<span><b>${props}</b> proposal${props === 1 ? '' : 's'}</span>`);
  parts.push(`<span><b>${pool}</b> in pool</span>`);
  parts.push(`<span><b>${pins}</b> pin${pins === 1 ? '' : 's'}</span>`);
  el.innerHTML = parts.join('');
}

// Autopilot desired-state entries from the /api/agents global blob (#476).
function _adminApEntries() {
  return ((_adminGlobal || {}).autopilot || {}).entries || [];
}

// Model ids discovered per provider, keyed by hostname for the pool rows.
let _adminProviderModelList = [];
function _adminModelForHost(host) {
  for (const m of _adminProviderModelList) {
    if ((m.agents || []).includes(host)) return m.id;
  }
  return '';
}

// Provider primary id: default_<p>_id first, legacy primary_<p>_id second (backend precedence).
function _adminPrimaryOf(g, p) {
  return (g || {})['default_' + p + '_id'] || (g || {})['primary_' + p + '_id'] || '';
}

function adminRenderPoolOrder() {
  adminRenderProviderSeg();
  const ul = document.getElementById('adminPoolOrderList');
  if (!ul) return;
  // Autopilot manages pool membership for replicated entries (#476);
  // manual reordering is disabled while it does (#500).
  const managed = _adminApEntries().some(e =>
    e.provider === _adminProvSel && (e.max_replicas || 1) > 1);
  const apBadge = document.getElementById('adminPoolApBadge');
  if (apBadge) apBadge.style.display = managed ? '' : 'none';
  const dragHint = document.getElementById('adminPoolDragHint');
  if (dragHint) dragHint.style.display = managed ? 'none' : '';
  const pool = ((_adminGlobal && _adminGlobal[_adminProvSel + '_pool']) || []).slice();
  const primaryId = _adminPrimaryOf(_adminGlobal, _adminProvSel);
  const idToAgent = {};
  for (const a of (_adminAgentsCache || [])) idToAgent[a.agent_id] = a;

  if (pool.length === 0) {
    ul.innerHTML = `<li class="rt-empty">The ${adminEsc(_adminProvSel)} pool is empty — turn on <b>in pool</b> for an agent in Agents › row drawer.</li>`;
    if (_adminPoolSortable) { try { _adminPoolSortable.destroy(); } catch(e){} _adminPoolSortable = null; }
    return;
  }

  ul.innerHTML = pool.map((aid, i) => {
    const unknown = !idToAgent[aid];
    const a = idToAgent[aid] || { hostname: '(unknown agent ' + aid.slice(0,8) + '…)', liveness: null, version: '' };
    const dotCls = a.liveness === 'live' ? 'ok' : a.liveness === 'stale' ? 'warn' : unknown ? 'crit' : 'crit';
    const model = unknown ? '' : _adminModelForHost(a.hostname);
    const meta = [unknown ? '' : _adminAgentIP(a), a.version || '', model || 'idle']
      .filter(Boolean).join(' · ');
    const act = unknown
      ? `<button type="button" class="ib crith" data-tip="Remove this deleted agent from the pool" onclick="adminTogglePool('${adminEsc(_adminProvSel)}','${adminEsc(aid)}',false)">✕</button>`
      : `<button type="button" class="ib" data-tip="Open in Dashboard" onclick="_jumpToDashboard('${adminEsc(aid)}','${adminEsc(_adminProvSel)}')">↗</button>`;
    return `<li class="rt-pr" data-agent-id="${adminEsc(aid)}">
      ${managed ? '<span class="hdl"></span>' : '<span class="hdl pool-handle" title="Drag to reorder">⠿</span>'}
      <span class="pos">${i + 1}</span>
      <div class="who">
        <div class="n">${adminEsc(a.hostname || aid.slice(0,8))}${aid === primaryId ? ' <span class="pill info">primary</span>' : ''}</div>
        <div class="m">${adminEsc(meta)}</div>
      </div>
      <span class="dot ${dotCls}"></span>
      ${act}
    </li>`;
  }).join('');

  // Tear down any previous Sortable instance and re-attach to the
  // freshly-rendered list so element refs aren't stale. Autopilot-managed
  // pools render without handles and get no Sortable at all.
  if (_adminPoolSortable) { try { _adminPoolSortable.destroy(); } catch(e){} _adminPoolSortable = null; }
  if (!managed) {
    _adminPoolSortable = Sortable.create(ul, {
      animation: 150,
      handle: '.pool-handle',
      ghostClass: 'dragging',
      onEnd: adminPoolReorderCommit,
    });
  }
  adminRenderRoutingSummary();
}

// Called by Sortable when a drag completes. Read the new order out of
// the DOM, POST each moved agent to /api/agents/<id>/<provider>-pool with
// its new position. Reload on completion so backend truth wins.
async function adminPoolReorderCommit() {
  const ul = document.getElementById('adminPoolOrderList');
  const resultEl = document.getElementById('adminPoolResult');
  const newOrder = Array.from(ul.querySelectorAll('li[data-agent-id]')).map(li => li.dataset.agentId);
  const oldOrder = (_adminGlobal && _adminGlobal[_adminProvSel + '_pool']) || [];
  if (JSON.stringify(newOrder) === JSON.stringify(oldOrder)) return;

  if (resultEl) resultEl.textContent = 'saving new order…';
  // Only agents whose position changed need a POST; the backend's
  // <provider>-pool endpoint is "remove if present, insert at position".
  for (let i = 0; i < newOrder.length; i++) {
    if (newOrder[i] === oldOrder[i]) continue;   // unchanged
    try {
      await fetch(`/api/agents/${encodeURIComponent(newOrder[i])}/${_adminProvSel}-pool`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ in_pool: true, position: i }),
      });
    } catch (e) {
      if (resultEl) resultEl.textContent = 'reorder failed at index ' + i + ': ' + e.message;
      adminLoadAgents();
      return;
    }
  }
  if (resultEl) resultEl.textContent = 'reordered ✓';
  adminLoadAgents();
}

// Fetch the union of model IDs across the selected provider's pool agents;
// feeds the pin add-row select, the pool rows and the autopilot catalog.
async function adminLoadProviderModels() {
  const provider = _adminProvSel;
  try {
    const r = await fetch(`/api/admin/${provider}-models`);
    // A slow fan-out can resolve after the operator switched providers — drop it.
    if (!r.ok || provider !== _adminProvSel) return;
    const d = await r.json();
    _adminProviderModelList = d.models || [];
    adminRenderPinModelSelect();
  } catch (e) {
    // Best-effort; the pin editor still works with whatever was cached.
  }
}

function adminRenderPinModelSelect() {
  const sel = document.getElementById('adminPinModelSelect');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">choose a model from the pool</option>' +
    _adminProviderModelList.map(m =>
      `<option value="${adminEsc(m.id)}">${adminEsc(m.id)}</option>`).join('');
  if (current) sel.value = current;
}

// Render the model-pins table. Called from adminLoadAgents so every agent-list
// refresh keeps it in sync (and repopulates the host select).
function adminRenderPins() {
  const tbody = document.getElementById('adminPinsTbody');
  const select = document.getElementById('adminPinAgentSelect');
  if (!tbody || !select) return;
  const prov = _adminPoolProviders.find(p => p.name === _adminProvSel) || _adminPoolProviders[0];
  const pins = (_adminGlobal && prov.pin_key && _adminGlobal[prov.pin_key]) || {};
  const pool = (_adminGlobal && _adminGlobal[prov.name + '_pool']) || [];
  const idToAgent = {};
  for (const a of (_adminAgentsCache || [])) idToAgent[a.agent_id] = a;

  const entries = Object.entries(pins).sort(([m1], [m2]) => m1.localeCompare(m2));
  if (entries.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3"><div class="empty">No pins set — every model round-robins the pool.</div></td></tr>';
  } else {
    tbody.innerHTML = entries.map(([model, aid]) => {
      const a = idToAgent[aid];
      const host = a
        ? `${adminEsc(a.hostname || '')} <span class="t">· ${adminEsc(_adminAgentIP(a))}</span>`
        : `${adminEsc(aid).slice(0,8)}… <span class="t">· unknown agent</span>`;
      // Autopilot entries route via this pin while single-placed (#476, #500).
      const apManaged = _adminApEntries().some(e =>
        e.provider === prov.name && e.model === model);
      const apBadge = apManaged
        ? ' <span class="pill info ap-managed-badge" title="Managed by a Model Autopilot entry — manual edits may be overridden on the next reconcile.">autopilot</span>'
        : '';
      return `<tr>
        <td class="n mono">${adminEsc(model)}${apBadge}</td>
        <td>${host}</td>
        <td class="r"><div class="act">
          <button type="button" class="ib crith" data-tip="Unpin" onclick="adminClearPin('${adminEsc(model)}')">✕</button>
        </div></td>
      </tr>`;
    }).join('');
  }

  // Host select: agents in the pool, or approved + provider-capable.
  const eligible = (_adminAgentsCache || []).filter(a =>
    a.status === 'approved' &&
    ((a.capabilities || {})[prov.name]) &&
    (pool.includes(a.agent_id) || pool.length === 0)
  );
  const current = select.value;
  select.innerHTML = '<option value="">choose host</option>' +
    eligible.map(a => `<option value="${adminEsc(a.agent_id)}">${adminEsc(a.hostname)}${pool.includes(a.agent_id) ? ' · pool #' + (pool.indexOf(a.agent_id) + 1) : ''}</option>`).join('');
  if (current) select.value = current;
  adminRenderPinModelSelect();
  adminRenderRoutingSummary();
}

async function adminLoadPins() {
  // Pin state lives inside _adminGlobal (loaded by /api/agents). Just
  // re-render — no separate endpoint needed.
  adminRenderPins();
}

async function adminAddPin() {
  const modelEl = document.getElementById('adminPinModelSelect');
  const agentEl = document.getElementById('adminPinAgentSelect');
  const resultEl = document.getElementById('adminPinsResult');
  const model = (modelEl.value || '').trim();
  const aid = agentEl.value || '';
  if (!model) { resultEl.textContent = 'Choose a model'; return; }
  if (!aid)   { resultEl.textContent = 'Choose a host'; return; }
  try {
    const r = await fetch(`/api/admin/${_adminProvSel}-pins`, {
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
    const r = await fetch(`/api/admin/${_adminProvSel}-pins`, {
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
  let d;
  try {
    const r = await fetch('/api/agents');
    if (!r.ok) {
      _adminLog('GET /api/agents failed: ' + r.status + ' (admin gate denies this IP)', 'err');
      if (window.AgentsView) AgentsView.stamp({ ok: false });
      return;
    }
    d = await r.json();
  } catch (e) {
    _adminLog('error: ' + e.message, 'err');
    if (window.AgentsView) AgentsView.stamp({ ok: false, unreachable: true });
    return;
  }
  try {
    _adminGlobal = d.global || {};
    if (Array.isArray(d.pool_providers) && d.pool_providers.length) {
      _adminPoolProviders = d.pool_providers;
      if (!_adminPoolProviders.some(p => p.name === _adminProvSel)) _adminProvSel = _adminPoolProviders[0].name;
    }
    if (Array.isArray(d.providers) && d.providers.length) {
      _adminProviders = d.providers;
    }
    _adminHostAutoDetected = !!d.host_auto_detected;
    _latestAgentVersion = d.latest_agent_version || null;
    _adminManagerVersion = d.manager_version || null;
    _adminCollectInterval = d.collect_interval_s || null;
    _adminAgentsCache = (d.agents || []).slice().sort((a,b) => (a.hostname||'').localeCompare(b.hostname||''));
    if (window.AgentsView) { AgentsView.stamp({ ok: true }); AgentsView.render(); }
    // Keep the pin editor + pool order + model datalist in sync with every refresh.
    adminRenderPins();
    adminRenderPoolOrder();
    adminLoadProviderModels();   // fire-and-forget; populates datalist
  } catch(e) {
    _adminLog('error: ' + e.message, 'err');
  }
}

// True when holderId exists in the loaded agent list (empty list = unknown → true).
function _adminAgentKnown(aid) {
  const list = _adminAgentsCache || [];
  return !list.length || list.some(a => a.agent_id === aid);
}

// Single-select role: offered while unheld, then only on the holder;
// autoHidden hides it everywhere. A dangling holder counts as unheld.
function _singleSelectShow(agentId, holderId, autoHidden) {
  if (autoHidden) return false;
  return !holderId || holderId === agentId || !_adminAgentKnown(holderId);
}

// Jump to the dashboard with this agent selected for the provider. Only pins a
// selection when there's more than one agent of that provider — a single-agent
// install stays byte-identical (no ?agent= persisted).
function _jumpToDashboard(agentId, provider) {
  const list = (window._agentsByProvider && window._agentsByProvider[provider]) || [];
  if (list.length > 1 && typeof _selectAgent === 'function') {
    _selectAgent(provider, agentId);
  }
  const spec = _adminProviders.find(p => p.name === provider);
  const sub = (spec && spec.sub_tab) || provider;
  if (typeof switchTab === 'function') switchTab('dashboard');
  if (typeof switchSubTab === 'function') switchSubTab('dashboard', sub);
}

// Phase 4 #4 / #359 — POST /api/agents/<id>/<provider>-pool to add/remove.
async function adminTogglePool(provider, aid, inPool) {
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(aid)}/${provider}-pool`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ in_pool: inPool }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      _adminLog(provider + '-pool ' + (inPool ? 'add' : 'remove') + ' failed: ' + (d.error || r.status));
    }
  } catch (e) {
    _adminLog(provider + '-pool request failed: ' + e.message);
  }
  adminLoadAgents();
}

// #412 — POST /api/agents/<id>/host-role to designate (or clear) the manager
// host agent. Updates window.__MGR_AGENT so the manager-host cards repoint on
// the next poll without a full page reload.
async function adminToggleHostAgent(aid, isHost) {
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(aid)}/host-role`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ set: isHost }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) {
      window.__MGR_AGENT = d.host_agent_id || null;
    } else {
      _adminLog('host-agent ' + (isHost ? 'set' : 'clear') + ' failed: ' + (d.error || r.status));
    }
  } catch (e) {
    _adminLog('host-agent request failed: ' + e.message);
  }
  adminLoadAgents();
}

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
let _adminHostAutoDetected = false;
let _latestAgentVersion = null;
let _adminManagerVersion = null;
let _adminCollectInterval = null;
// Pool-picker providers from /api/agents, plus per-card chip selections.
let _adminPoolProviders = [{ name: 'llama', label: 'llama.cpp', pin_key: 'llama_model_pins' }];
let _adminProvSel = 'llama';
// All registered providers from /api/agents — drives primary checkboxes,
// view-dashboard buttons, and provider→sub-tab jump routing.
let _adminProviders = [
  { name: 'llama', label: 'llama.cpp', capability_key: 'llama', sub_tab: 'llamacpp' },
  { name: 'lms',   label: 'LM Studio', capability_key: 'lms',   sub_tab: 'lmstudio' },
  { name: 'vllm',  label: 'vLLM',      capability_key: 'vllm',  sub_tab: 'vllm' },
];
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

let _adminAuthState = null;

const _AC_MODES = [
  { v: 'required', label: 'Required',
    d: 'Every browser signs in. Recommended whenever the dashboard is reachable beyond your own machine.' },
  { v: 'trusted_cidr', label: 'Trusted network', d: '' },
  { v: 'disabled', label: 'Off',
    d: 'No sign-in at all. Only for a dashboard that is never exposed beyond a trusted LAN.' },
];

async function adminAuthLoad() {
  try {
    const r = await fetch('/api/admin/auth');
    if (!r.ok) { adminRenderAuth({ ok: false }); return; }
    const d = await r.json();
    if (!d.ok) return;
    _adminAuthState = d;
    adminRenderAuth(d);
  } catch (e) { /* keep the last good render */ }
}

// Login card: mc-seg mirrors the (hidden) #adminAuthMode select the save path reads.
function adminRenderAuth(d) {
  const sel = document.getElementById('adminAuthMode');
  if (sel && d && d.ok !== false) sel.value = (d.policy === 'auto') ? d.mode : d.policy;
  const meta = document.getElementById('adminAuthMeta');
  if (meta) {
    meta.innerHTML = d && d.instant
      ? 'managed here · policy <b>auto</b> · changes apply instantly'
      : 'pinned in config · <b>restart required</b>';
  }
  const notice = document.getElementById('adminAuthDefaultNotice');
  if (notice) notice.hidden = !(d && d.is_default);
  adminRenderAuthModes();
  adminRenderAccessSummary();
}

function adminRenderAuthModes() {
  const seg = document.getElementById('adminAuthSeg');
  const box = document.getElementById('adminAuthModes');
  const sel = document.getElementById('adminAuthMode');
  const cur = (sel && sel.value) || 'required';
  if (seg) {
    seg.innerHTML = _AC_MODES.map(m =>
      `<button type="button" class="${m.v === cur ? 'on' : ''}" data-mode="${m.v}">${m.label}</button>`).join('');
    if (!seg._acBound) {
      seg._acBound = true;
      seg.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-mode]');
        if (!b) return;
        if (sel) sel.value = b.getAttribute('data-mode');
        adminRenderAuthModes();
      });
    }
  }
  if (!box) return;
  const cidrs = ((_adminAuthState || {}).admin_cidrs) || [];
  const role = ((_adminAuthState || {}).bypass_role) || 'operator';
  box.innerHTML = _AC_MODES.map(m => {
    const body = m.v === 'trusted_cidr'
      ? 'Browsers on the admin networks skip sign-in and get the '
        + `<span class="tag">${adminEsc(role)}</span> role; everyone else signs in. Networks: `
        + (cidrs.length ? cidrs.map(c => `<span class="tag">${adminEsc(c)}</span>`).join(' ')
                        : '<span class="tag">none configured</span>')
      : adminEsc(m.d);
    return `<div class="ac-mode ${m.v === cur ? 'on' : ''}"><b>${m.label}</b><span>${body}</span></div>`;
  }).join('');
}

function adminRenderAccessSummary() {
  const el = document.getElementById('acSummary');
  if (!el) return;
  const d = _adminAuthState || {};
  const mode = d.mode === 'trusted_cidr' ? 'trusted network'
    : d.mode === 'disabled' ? 'off' : 'login required';
  const users = _adminUsersCache || [];
  const locked = users.filter(u => u.locked).length;
  el.innerHTML = `<span>${adminEsc(mode)}</span>`
    + `<span><b>${users.length}</b> user${users.length === 1 ? '' : 's'}</span>`
    + `<span><b class="${locked ? 'warn' : ''}">${locked}</b> locked</span>`;
}

async function adminAuthSave() {
  const res = document.getElementById('adminAuthResult');
  const mode = document.getElementById('adminAuthMode').value;
  res.className = 'msg'; res.textContent = 'saving…';
  try {
    const r = await fetch('/api/admin/auth', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ mode })});
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { res.className = 'msg err'; res.textContent = '✗ ' + (d.error || ('HTTP ' + r.status)); return; }
    if (d.restart_required) {
      // The mode was written to the config file but only loads at startup, so
      // it isn't live yet. The manager can't restart itself (no privilege), so
      // surface the command for the operator to run.
      res.className = 'msg';
      res.innerHTML = '✓ saved to config — <b>restart required</b>: '
        + `<code>${adminEsc(d.restart_cmd || 'sudo systemctl restart llm-systems-manager')}</code>`;
    } else {
      res.className = 'msg ok';
      res.textContent = `✓ saved — mode: ${d.mode}`;
    }
    adminAuthLoad();
  } catch (e) { res.className = 'msg err'; res.textContent = '✗ ' + e.message; }
}

function _adminBackupLog(msg, cls) {
  const el = document.getElementById('adminBackupResult');
  if (!el) return;
  const ts = new Date().toTimeString().slice(0, 8);
  const color = cls === 'err' ? 'var(--crit)' : (cls === 'ok' ? 'var(--ok)' : 'var(--fg-muted)');
  el.innerHTML = `<span style="color:${color};">[${ts}] ${adminEsc(msg)}</span>`;
}

async function adminExportArchive(component) {
  const label = _BACKUP_LABELS[component] || component;
  const ep = _BACKUP_ENDPOINTS[component];
  if (!ep) return;
  const password = await _adminBackupPasswordPrompt({
    title:   `Back up ${label}`,
    intro:   `The archive contains secrets (config tokens, agent bearer tokens, internal CA private key). A password is strongly recommended — it encrypts the file with AES-256-GCM using a scrypt-derived key.`,
    minLen:  _BACKUP_MIN_PW,
    confirm: 'Back up',
    allowBlank: true,
  });
  if (password === null) return;  // cancelled
  _adminBackupLog(`backing up ${label}…`);
  let resp;
  try {
    resp = await fetch(ep.export, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password}),
    });
  } catch (e) {
    _adminBackupLog(`✗ ${label} backup failed — ${e.message}`, 'err');
    return;
  }
  if (!resp.ok) {
    const txt = await resp.text();
    let err = txt;
    try { err = (JSON.parse(txt).error || JSON.parse(txt).detail || txt); } catch (_) {}
    _adminBackupLog(`✗ ${label} backup failed — ${err}`, 'err');
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
  _adminBackupLog(`✓ ${label} backup downloaded — ${fname} (${blob.size} bytes, ${note})`, 'ok');
}

async function adminImportArchive(component) {
  const label = _BACKUP_LABELS[component] || component;
  const ep    = _BACKUP_ENDPOINTS[component];
  if (!ep) return;
  const picked = await _adminBackupFilePrompt({
    title:    `Restore ${label}`,
    intro:    `Pick a previously-created .lsmenc archive. If encrypted, enter the password it was made with. Nothing is written to disk until you confirm the preview.`,
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
  if (confirmed === null) { _adminBackupLog('restore cancelled'); return; }
  const overrides = confirmed.overrides || {};
  const hostRemap = confirmed.hostRemap || {};
  const categories = confirmed.categories;  // null when archive has no category info

  _adminBackupLog(`applying ${label} restore…`);
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
  _adminBackupLog(`✓ ${label} restore applied — ${written.length} files written${patchedNote}${hrNote}.`, 'ok');

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
    title: `${label} restore succeeded — next steps`,
    dismissable: false,
    bodyHtml:
      `<div style="font-size:0.88em;line-height:1.55;">` +
      `<p style="margin:0 0 10px;">The archive was unpacked and written to disk. ` +
      `Copies of the pre-restore files are kept alongside each target.</p>` +
      patchedBlock +
      `<ol style="margin:8px 0;padding-left:22px;">` +
      `<li>Restart the service so the new state loads:<br>` +
      `<code style="display:block;margin-top:4px;background:var(--bg);padding:6px 8px;border-radius:4px;font-size:0.82em;">${adminEsc(ep.restartHint)}</code></li>` +
      extraStepsBlock +
      `<li>Verify the restored data is visible in the UI; if anything looks ` +
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
    if (manifest.manager_version) metaLines.push(`<div>Manager version (at backup): <code>${adminEsc(manifest.manager_version)}</code></div>`);
    if (manifest.ae_version)      metaLines.push(`<div>AE version (at backup): <code>${adminEsc(manifest.ae_version)}</code></div>`);
    if (manifest.hostname)        metaLines.push(`<div>Source host: <code>${adminEsc(manifest.hostname)}</code></div>`);
    if (manifest.created_at)      metaLines.push(`<div>Backed up at: <code>${adminEsc(manifest.created_at)}</code></div>`);
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
            What to restore
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
        `Topology overrides are unavailable for this archive — edit the file by hand after restoring.</div>`;
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
              rewrite source-host names so restored rules match this system
            </span>
          </summary>
          <div style="margin-top:10px;font-size:0.80em;color:var(--fg-muted);line-height:1.5;">
            These host names are referenced by the restored rules and notification configs.
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
      <div style="font-size:1.05em;font-weight:600;margin-bottom:10px;">Apply ${adminEsc(label)} restore?</div>
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
          border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">Apply restore</button>
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
        hint.textContent = allowBlank ? 'Leaving blank saves the archive WITHOUT encryption.' : '';
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
  overlay.className = 'adm-overlay';
  const box = document.createElement('div');
  box.className = 'adm-modal';
  box.innerHTML = `
    <div class="adm-modal-h">
      <h3>Edit agent config <b>${adminEsc(name)}</b></h3>
      <span class="path">${adminEsc(initial.path)} · ${adminEsc(String(initial.size))} bytes</span>
    </div>
    <textarea id="aecText" class="adm-code" spellcheck="false" wrap="off"></textarea>
    <div class="adm-modal-f">
      <span class="msg" id="aecStatus"></span>
      <button type="button" id="aecCancel" class="mcbtn mcbtn-ghost">Close</button>
      <button type="button" id="aecSave" class="mcbtn mcbtn-pri">Save (backup + write)</button>
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
    status.classList.remove('err');
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
      status.textContent = `✗ ${err}`; status.classList.add('err');
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

// Themed notification; auto-dismisses unless sticky (then click dismisses).
// message is set via textContent — safe to pass user-supplied text.
function _themedToast(message, { kind = 'ok', ms = 2600, sticky = false } = {}) {
  const accent = kind === 'err' ? 'var(--crit)' : (kind === 'warn' ? 'var(--warn)' : 'var(--accent)');
  const t = document.createElement('div');
  t.style.cssText = 'position:fixed;bottom:22px;left:50%;transform:translateX(-50%);z-index:10000;'
    + 'background:var(--bg-card);color:var(--fg);border:1px solid var(--border);border-left:3px solid '
    + accent + ';border-radius:6px;padding:10px 16px;font-family:system-ui,-apple-system,sans-serif;'
    + 'font-size:0.88em;box-shadow:0 6px 24px rgba(0,0,0,0.4);max-width:80vw;opacity:0;transition:opacity 0.15s;';
  t.textContent = message;
  if (sticky) {
    document.querySelectorAll('.themed-toast.sticky').forEach(o => o.remove());
    t.className = 'themed-toast sticky';
    t.style.cursor = 'pointer';
    t.title = 'click to dismiss';
    const x = document.createElement('span');
    x.textContent = '✕';
    x.style.cssText = 'margin-left:12px;color:var(--fg-muted,#9aa);font-size:0.9em;';
    t.appendChild(x);
    t.addEventListener('click', () => t.remove());
  }
  document.body.appendChild(t);
  requestAnimationFrame(() => { t.style.opacity = '1'; });
  if (!sticky) setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 200); }, ms);
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
  const res = await _adminStreamUpdate(aid);
  if (res.transport) return;
  if (res.ok === true) {
    _adminUpdateLog(res.noRestart
      ? 'agent already up to date; no restart needed'
      : 'agent SIGTERM-ing for restart; refreshing agent list in 5s…');
    // Refresh the agent list but leave the panel open so the operator
    // can read the full output. Operator closes it via the X.
    setTimeout(() => { adminLoadAgents(); }, 5000);
  } else if (res.ok === false) {
    _adminUpdateLog('install failed; agent kept running with old code', 'err');
  } else {
    _adminUpdateLog('stream ended without a `done` frame', 'err');
  }
}

// One self-update stream into the open panel (#637 refactor). Returns
// {ok: true|false|null, noRestart, transport} — transport = failed
// before any SSE frame (already logged); ok null = no `done` frame.
async function _adminStreamUpdate(aid) {
  let r;
  try {
    r = await fetch(`/api/agents/${aid}/self-update`, { method: 'POST' });
  } catch (e) {
    _adminUpdateLog(`✗ request failed: ${e.message}`, 'err');
    return { ok: false, noRestart: false, transport: true };
  }
  if (!r.ok) {
    let body = '';
    try { body = await r.text(); } catch {}
    _adminUpdateLog(`✗ HTTP ${r.status}: ${body.slice(0, 500)}`, 'err');
    return { ok: false, noRestart: false, transport: true };
  }
  if (!r.body) {
    _adminUpdateLog('✗ no response body — proxy did not stream', 'err');
    return { ok: false, noRestart: false, transport: true };
  }

  // Parse SSE frames out of the streamed body.
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let doneOk = null;
  let doneNoRestart = false;
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
        // Frozen agents report an ok no-op ("already up to date") with no
        // restart_eta_s — don't announce a restart that isn't coming.
        doneNoRestart = msg.ok === true && msg.restart_eta_s == null
          && /no restart/i.test(msg.msg || '');
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
  return { ok: doneOk, noRestart: doneNoRestart, transport: false };
}

// Poll /api/agents until the agent reports targetV; true on success (#637).
async function _adminAwaitAgentVersion(aid, targetV, timeoutMs = 120000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await new Promise(res => setTimeout(res, 3000));
    try {
      const r = await fetch('/api/agents');
      if (!r.ok) continue;
      const d = await r.json();
      const a = (d.agents || []).find(x => x.agent_id === aid);
      if (a && a.version === targetV) return true;
    } catch {}
  }
  return false;
}

// #637: sequential fleet-wide update — one agent at a time, each verified
// back on the new version before the next starts; stops on first failure.
let _adminUpdateAllRunning = false;
async function adminUpdateAll() {
  if (_adminUpdateAllRunning) {
    _themedToast('Update all is already running', { kind: 'warn' });
    return;
  }
  const newV = _latestAgentVersion;
  const todo = (_adminAgentsCache || []).filter(a => a.update_available);
  if (!todo.length || !newV) {
    _themedToast('All agents are already up to date');
    return;
  }
  const listHtml = todo.map(a =>
    `<div>${adminEsc(a.hostname || a.agent_id.slice(0, 8))}: ` +
    `${adminEsc(a.version || '?')} → ${adminEsc(newV)}</div>`).join('');
  const ok = await _themedConfirm({
    title: `Update ${todo.length} agent${todo.length > 1 ? 's' : ''}?`,
    bodyHtml:
      `<div style="font-family:monospace;background:var(--bg);border:1px solid var(--border);` +
      `border-radius:6px;padding:10px 12px;margin-bottom:12px;max-height:200px;overflow-y:auto;">${listHtml}</div>` +
      `<div>Agents update one at a time; each must come back on the new version ` +
      `before the next starts. The sequence stops on the first failure.</div>`,
    confirmLabel: 'Update all',
    cancelLabel: 'Cancel',
  });
  if (!ok) return;

  _adminUpdateAllRunning = true;
  _adminUpdateOpen(`all agents (${todo.length})`);
  const results = [];
  try {
    for (let i = 0; i < todo.length; i++) {
      const a = todo[i];
      const name = a.hostname || a.agent_id.slice(0, 8);
      if (i) _adminUpdateLog('', 'blank');
      _adminUpdateLog(`── [${i + 1}/${todo.length}] ${name}: ${a.version || '?'} → ${newV}`, 'stage');
      const res = await _adminStreamUpdate(a.agent_id);
      let okAgent = res.ok === true;
      if (okAgent && !res.noRestart) {
        _adminUpdateLog(`waiting for ${name} to come back on ${newV}…`);
        okAgent = await _adminAwaitAgentVersion(a.agent_id, newV);
        _adminUpdateLog(okAgent
          ? `✓ ${name} is back on ${newV}`
          : `✗ ${name} did not report ${newV} within 2 minutes`,
          okAgent ? 'ok' : 'err');
      } else if (okAgent) {
        _adminUpdateLog(`✓ ${name} already up to date; no restart needed`, 'ok');
      }
      results.push({ name, state: okAgent ? 'updated' : 'failed' });
      if (!okAgent) {
        for (const rest of todo.slice(i + 1)) {
          results.push({ name: rest.hostname || rest.agent_id.slice(0, 8),
                         state: 'skipped' });
        }
        break;
      }
    }
  } finally {
    _adminUpdateAllRunning = false;
  }
  const updated = results.filter(r => r.state === 'updated').map(r => r.name);
  const failed = results.filter(r => r.state === 'failed').map(r => r.name);
  const skipped = results.filter(r => r.state === 'skipped').map(r => r.name);
  _adminUpdateLog('', 'blank');
  _adminUpdateLog(`── Update all finished: ${updated.length} updated`
    + (failed.length ? `, failed: ${failed.join(', ')}` : '')
    + (skipped.length ? `, skipped: ${skipped.join(', ')}` : ''),
    failed.length ? 'err' : 'ok');
  adminLoadAgents();
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
      if (_adminLogPaused || LivePause.on) return;
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
  if (btn) { btn.textContent = _adminLogPaused ? '▸ Resume' : '‖ Pause'; btn.classList.toggle('on', _adminLogPaused); }
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
    p.className = 'adm-dock left';
    p.innerHTML = `
      <div class="adm-dock-h">
        <span class="microlbl">Agent log</span>
        <span class="name" id="adminLogsTitle"></span>
        <button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" id="adminLogsPauseBtn" onclick="_adminLogsTogglePause()">‖ Pause</button>
        <button type="button" class="mcbtn mcbtn-ghost mcbtn-sm" onclick="_adminLogsClear()">Clear</button>
        <button type="button" class="adm-ib" onclick="_adminLogsClose()" aria-label="Close">×</button>
      </div>
      <div class="adm-dock-b" id="adminLogsBody"></div>`;
    document.body.appendChild(p);
  }
  p.style.display = 'flex';
  document.getElementById('adminLogsTitle').textContent = label;
  document.getElementById('adminLogsBody').textContent = '';
  _adminLogPaused = false;
  const btn = document.getElementById('adminLogsPauseBtn');
  if (btn) { btn.textContent = '‖ Pause'; btn.classList.remove('on'); }
}

function _adminLogsAppend(text, level) {
  const body = document.getElementById('adminLogsBody');
  if (!body) return;
  const line = document.createElement('div');
  if (level === 'err' || level === 'meta') line.className = 'l-' + level;
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
    p.className = 'adm-dock right';
    p.innerHTML = `
      <div class="adm-dock-h">
        <span class="microlbl">Self-update</span>
        <span class="name" id="adminUpdateTitle"></span>
        <button type="button" class="adm-ib" onclick="_adminUpdateClose()" aria-label="Close">×</button>
      </div>
      <div class="adm-dock-b" id="adminUpdateLog"></div>`;
    document.body.appendChild(p);
  }
  p.style.display = 'flex';
  document.getElementById('adminUpdateTitle').textContent = label;
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
  const line = document.createElement('div');
  const cls = [];
  if (level === 'err' || level === 'stage' || level === 'ok') cls.push('l-' + level);
  if (level === 'stage' && /^\s*(╔|║|╚|\+\+\+)/.test(msg)) cls.push('l-bold');
  if (cls.length) line.className = cls.join(' ');
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
    _adminLog(`✓ agent security ${disabled ? 'off — agents accept unauthenticated control calls' : 'on'}`);
  } else {
    _adminLog(`✗ agent security toggle failed (HTTP ${r.status})`, 'err');
  }
  adminLoadAgents();
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
let _adminUsersCache = [];

const _SVG_KEY = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="5.5" cy="10.5" r="3"/><path d="M8 8l6-6M11 5l2 2M9.5 6.5l2 2"/></svg>';
const _SVG_UNLOCK = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="3" y="7.5" width="10" height="7" rx="1.5"/><path d="M5.5 7.5V5a2.5 2.5 0 0 1 5 0"/></svg>';

// "Sep 2 · 1:18 PM" — built by hand so the ledger reads the same everywhere.
const _ADM_MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function _adminStamp(v) {
  if (!v) return '—';
  const dt = new Date(typeof v === 'number' ? v * 1000 : v);
  if (isNaN(dt.getTime())) return '—';
  let h = dt.getHours();
  const ap = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  const mm = String(dt.getMinutes()).padStart(2, '0');
  return `${_ADM_MONTHS[dt.getMonth()]} ${dt.getDate()} · <b>${h}:${mm} ${ap}</b>`;
}

async function adminUsersLoad() {
  const tb = document.getElementById('adminUsersTbody');
  if (!tb) return;
  _adminUsersBindOnce();
  try {
    const r = await fetch('/api/admin/users');
    if (!r.ok) { tb.innerHTML = '<tr><td colspan="5"><div class="empty">Admin-only.</div></td></tr>'; return; }
    const d = await r.json();
    _adminUsersCache = d.users || [];
    tb.innerHTML = _adminUsersCache.map(_adminUserRow).join('') ||
      '<tr><td colspan="5"><div class="empty">No users.</div></td></tr>';
    const meta = document.getElementById('adminUsersMeta');
    if (meta) {
      const admins = _adminUsersCache.filter(u => u.role === 'admin').length;
      const ops = _adminUsersCache.length - admins;
      meta.innerHTML = `<b>${admins}</b> admin${admins === 1 ? '' : 's'} · <b>${ops}</b> operator${ops === 1 ? '' : 's'}`;
    }
    adminRenderAccessSummary();
  } catch (e) {
    tb.innerHTML = '<tr><td colspan="5"><div class="empty">Load failed.</div></td></tr>';
  }
}

// Delegated handler: action buttons carry data-* attrs (no inline onclick), so a
// username can never reach a JS-eval context — the row builder stays injection-free.
function _adminUsersBindOnce() {
  const tb = document.getElementById('adminUsersTbody');
  if (!tb || tb._uBound) return;
  tb._uBound = true;
  tb.addEventListener('click', (e) => {
    const kb = e.target.closest('button[data-menu]');
    if (kb) { _adminUserMenu(kb); return; }
    const btn = e.target.closest('button[data-uact]');
    if (!btn) return;
    _adminUserMenuClose();
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
  const add = document.getElementById('adminUserAddBtn');
  const row = document.getElementById('adminUserAddRow');
  const cancel = document.getElementById('adminUserCancelBtn');
  if (add && row) add.addEventListener('click', () => { row.hidden = false; });
  if (cancel && row) cancel.addEventListener('click', () => { row.hidden = true; });
  document.addEventListener('click', (ev) => {
    if (!ev.target.closest('#adminUsersTbody .act')) _adminUserMenuClose();
  });
  // Close on scrolls that move the anchor (page or a container holding the menu), not inner log boxes.
  document.addEventListener('scroll', (ev) => {
    const t = ev.target;
    if (_adminUserMenuEl && (t === document || t.contains(_adminUserMenuEl))) _adminUserMenuClose();
  }, true);
  window.addEventListener('resize', _adminUserMenuClose);
}

// Row menus are position:fixed — the table's overflow box clips absolute ones.
let _adminUserMenuEl = null;
function _adminUserMenuClose() {
  if (!_adminUserMenuEl) return;
  _adminUserMenuEl = null;
  document.querySelectorAll('#adminUsersTbody .mc-menu.open').forEach(m => {
    m.classList.remove('open'); m.style.cssText = '';
  });
}
function _adminUserMenu(btn) {
  const m = document.getElementById(btn.getAttribute('data-menu'));
  if (!m) return;
  const was = m.classList.contains('open');
  _adminUserMenuClose();
  if (was) return;
  m.classList.add('open', 'fixed');
  _adminUserMenuEl = m;
  _adminPlaceFixedMenu(btn, m);
}
// Pins an open .mc-menu below its button (right-aligned), or above it when it would
// run past the viewport bottom. Shared with the Agents roster menus.
function _adminPlaceFixedMenu(btn, m) {
  const r = btn.getBoundingClientRect();
  const mh = m.offsetHeight || 0;
  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const up = vh && mh && r.bottom + 6 + mh > vh - 8;
  const top = up ? Math.max(8, Math.round(r.top - 6 - mh)) : Math.round(r.bottom + 6);
  m.style.cssText = `position:fixed;top:${top}px;left:auto;right:${Math.max(8, Math.round(vw - r.right))}px;z-index:1200`;
}

function _adminUserRow(u, i) {
  const name = adminEsc(u.username);
  const role = u.role === 'admin' ? 'Admin' : 'Operator';
  const me = (_adminAuthState && _adminAuthState.current_user)
    || (window._me && window._me.user) || '';
  const you = u.username === me ? '<span class="you">you</span>' : '';
  let status;
  if (u.locked) {
    const detail = [u.failed_count ? `${u.failed_count} failed` : '',
                    u.lock_minutes_left ? `${u.lock_minutes_left} min left` : ''].filter(Boolean).join(' · ');
    status = '<span class="pill crit">locked</span>'
      + (detail ? ` <span class="t sm">${adminEsc(detail)}</span>` : '');
  } else if (u.disabled) {
    status = '<span class="pill dim">disabled</span>';
  } else {
    status = '<span class="pill ok">active</span>';
  }
  const toggleRole = u.role === 'admin' ? 'operator' : 'admin';
  const dis = u.disabled ? 'false' : 'true';
  const mid = `adminUserMenu${i}`;
  return `<tr>
    <td class="n">${name}${you}</td><td>${role}</td><td>${status}</td>
    <td class="t">${_adminStamp(u.last_login)}</td>
    <td class="r"><div class="act">
      ${u.locked ? `<button type="button" class="ib on warnh" data-tip="Unlock now" data-uact="unlock" data-user="${name}">${_SVG_UNLOCK}</button>` : ''}
      <button type="button" class="ib" data-tip="Reset password" data-uact="resetpw" data-user="${name}">${_SVG_KEY}</button>
      <button type="button" class="ib" data-tip="${u.disabled ? 'Enable sign-in' : 'Disable sign-in'}" data-uact="disable" data-user="${name}" data-arg="${dis}">${u.disabled ? '▸' : '‖'}</button>
      <div class="mc-menuwrap"><!-- wrapper keeps the shared .mc-menu click closer from resetting this menu -->
        <button type="button" class="ib" data-tip="More" data-menu="${mid}">⋯</button>
        <div class="mc-menu" id="${mid}">
          <button type="button" data-uact="role" data-user="${name}" data-arg="${toggleRole}"><span class="mi">${toggleRole === 'admin' ? '↥' : '↧'}</span>Make ${toggleRole}</button>
          <hr>
          <button type="button" class="danger" data-uact="delete" data-user="${name}"><span class="mi">✕</span>Delete user</button>
        </div>
      </div>
    </div></td></tr>`;
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
  if (ok) {
    document.getElementById('adminUserNew').value = '';
    document.getElementById('adminUserNewPw').value = '';
    const row = document.getElementById('adminUserAddRow');
    if (row) row.hidden = true;
  }
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
  const ok = await _adminUsersApi('/api/admin/users/' + encodeURIComponent(name),
    { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw }) },
    'Password reset for ' + name);
  if (ok && name === ((_adminAuthState || {}).default_user || 'llmadmin')) adminAuthLoad();
}

function adminUserUnlock(name) {
  _adminUsersApi('/api/admin/users/' + encodeURIComponent(name) + '/unlock', { method: 'POST' }, name + ' unlocked');
}

async function adminUserDelete(name) {
  const ok = await _themedConfirm({ title: 'Delete user?', bodyHtml: 'Delete ' + adminEsc(name) + '? This cannot be undone.', confirmLabel: 'Delete', danger: true });
  if (!ok) return;
  _adminUsersApi('/api/admin/users/' + encodeURIComponent(name), { method: 'DELETE' }, name + ' deleted');
}

// Audit log lives in admin-audit.js (#794); adminAuditLoad is its global entry.

// ─────────────────────────────────────────────────────────────────────────
// Backups sub-tab (#218, redesigned #797)
// ─────────────────────────────────────────────────────────────────────────
let _adminBackupData = null;
let _adminBackupShowAll = false;
const _ADM_BACKUP_ROWS = 5;

function _adminAgoShort(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function _adminInShort(ts) {
  if (!ts) return '—';
  const s = Math.round(ts - Date.now() / 1000);
  if (s <= 0) return 'due now';
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

async function adminLoadBackupStatus() {
  try {
    const r = await fetch('/api/admin/backup-status');
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    _adminBackupData = d;
    adminRenderBackup();
  } catch (e) {
    const body = document.getElementById('adminSchedBackupBody');
    if (body) body.innerHTML = `<div class="empty">Status unavailable: ${adminEsc(e.message)}</div>`;
  }
  adminLoadBackupSettings();
}

function adminRenderBackup() {
  const d = _adminBackupData || {};
  const last = d.last || {};
  const files = d.backups || [];

  const sum = document.getElementById('bkSummary');
  if (sum) {
    const mirror = !d.mirror_dir ? null
      : last.mirrored === true ? ['ok', 'ok'] : last.mirrored === false ? ['crit', 'failed'] : ['warn', 'pending'];
    sum.innerHTML = `<span>last backup <b class="${last.ok ? 'ok' : 'warn'}">${adminEsc(_adminAgoShort(last.ts))}</b></span>`
      + `<span>next in <b>${adminEsc(_adminInShort(d.next_due_ts))}</b></span>`
      + `<span><b>${files.length}</b> kept</span>`
      + (mirror === null ? '' : `<span>mirror <b class="${mirror[0]}">${mirror[1]}</b></span>`);
  }

  // Archives card — last manual backup per component.
  const ex = d.last_export || {};
  for (const comp of ['manager', 'alarm_engine']) {
    const el = document.getElementById('bkLastExport_' + comp);
    if (!el) continue;
    const e = ex[comp];
    el.innerHTML = e && e.ts
      ? `last backup <b>${_adminStamp(e.ts)}</b> · ${adminEsc(_fmtBytesShort(e.bytes))}`
      : 'last backup <b>never</b>';
  }

  // Scheduled card — pill, meta, kv block and the retained-archive ledger.
  const pill = document.getElementById('adminSchedBackupPill');
  if (pill) {
    let cls = 'dim', label = 'disabled';
    if (d.enabled && !d.scheduler_running) { cls = 'crit'; label = 'not running'; }
    else if (d.enabled) {
      if (last.ok === true) { cls = 'ok'; label = 'on schedule'; }
      else if (last.error) { cls = 'crit'; label = 'failed'; }
      else { cls = 'warn'; label = 'pending'; }
    }
    pill.className = 'pill ' + cls;
    pill.textContent = label;
  }
  const meta = document.getElementById('adminSchedBackupMeta');
  if (meta) {
    meta.innerHTML = d.enabled
      ? `every <b>${d.interval_hours} h</b> · keep <b>${d.keep_last}</b> · <b>${d.encrypted ? 'encrypted' : 'unencrypted'}</b>`
      : 'set an interval under Backup settings to enable';
  }
  const body = document.getElementById('adminSchedBackupBody');
  if (body) {
    if (!d.enabled) {
      body.innerHTML = '<div class="empty">Scheduled backups are off — set an interval under Backup settings.</div>';
    } else if (!d.scheduler_running) {
      body.innerHTML = `<div class="empty">Scheduler is not running: ${adminEsc(d.disabled_reason || 'unknown reason')} — fix it under Backup settings below.</div>`;
    } else {
      const lastLine = last.ts
        ? (last.ok
          ? `${_adminStamp(last.ts)} · ${adminEsc(last.file || '')} · ${adminEsc(_fmtBytesShort(last.bytes))} · ${last.files || '?'} files`
          : `<span class="critc">FAILED: ${adminEsc(last.error || 'unknown error')}</span>`)
        : 'no backup recorded yet';
      const mstate = last.mirrored === true ? ['okc', 'copied']
        : last.mirrored === false ? ['critc', 'copy failed'] : ['dim', 'not copied yet'];
      const mirror = d.mirror_dir
        ? `${adminEsc(d.mirror_dir)} <span class="${mstate[0]}">· ${mstate[1]}</span>`
        : '<span class="dim">not configured</span>';
      const folderBytes = d.folder_bytes != null ? d.folder_bytes : files.reduce((a, b) => a + (b.bytes || 0), 0);
      body.innerHTML = '<div class="bk-sched"><dl class="bk-kv">'
        + `<dt>Last backup</dt><dd>${lastLine}</dd>`
        + `<dt>Next due</dt><dd>${_adminStamp(d.next_due_ts)} <span class="dim">(in ${adminEsc(_adminInShort(d.next_due_ts))})</span></dd>`
        + `<dt>Mirror</dt><dd>${mirror}</dd>`
        + `<dt>Folder</dt><dd>data/backups/ <span class="dim">· ${adminEsc(_fmtBytesShort(folderBytes))} across ${files.length} archive${files.length === 1 ? '' : 's'}</span></dd>`
        + '</dl></div>';
    }
  }

  const tb = document.getElementById('adminSchedBackupTbody');
  if (tb) {
    const shown = _adminBackupShowAll ? files : files.slice(0, _ADM_BACKUP_ROWS);
    tb.innerHTML = shown.length
      ? shown.map(b => `<tr>
          <td class="n mono">${adminEsc(b.file)}</td>
          <td class="r">${adminEsc(_fmtBytesShort(b.bytes))}</td>
          <td class="t">${_adminStamp(b.mtime)}</td>
          <td>${_adminMirrorPill(d, last, b)}</td>
          <td class="r"><button type="button" class="mcbtn mcbtn-ghost mcbtn-sm"
            data-bk-dl="${adminEsc(b.file)}" title="Download this archive">⤓ Download</button></td>
        </tr>`).join('')
      : '<tr><td colspan="5"><div class="empty">No archives retained yet.</div></td></tr>';
  }
  const more = document.getElementById('adminSchedBackupMore');
  if (more) {
    const hidden = Math.max(0, files.length - _ADM_BACKUP_ROWS);
    more.textContent = hidden && !_adminBackupShowAll ? `… and ${hidden} older archive${hidden === 1 ? '' : 's'}` : '';
  }
  const showAll = document.getElementById('adminSchedBackupShowAll');
  if (showAll) {
    showAll.hidden = files.length <= _ADM_BACKUP_ROWS;
    showAll.textContent = _adminBackupShowAll ? 'Show fewer' : 'Show all';
    if (!showAll._bkBound) {
      showAll._bkBound = true;
      showAll.addEventListener('click', () => { _adminBackupShowAll = !_adminBackupShowAll; adminRenderBackup(); });
    }
  }
  const now = document.getElementById('adminBackupNowBtn');
  if (now && !now._bkBound) { now._bkBound = true; now.addEventListener('click', adminBackupNow); }
  if (tb && !tb._bkDlBound) {
    tb._bkDlBound = true;
    tb.addEventListener('click', ev => {
      const el = ev.target.closest('[data-bk-dl]');
      if (el) adminDownloadArchive(el.dataset.bkDl);
    });
  }
}

// Fetches the archive and saves the blob only on 200; a plain download link
// would write an error response to disk under the archive's own file name.
async function adminDownloadArchive(file) {
  _adminBackupLog(`downloading ${file}…`);
  let resp;
  try {
    resp = await fetch(`/api/admin/backup-archive/${encodeURIComponent(file)}`);
  } catch (e) {
    _adminBackupLog(`✗ ${file} download failed — ${e.message}`, 'err');
    return;
  }
  if (!resp.ok) {
    let err = await resp.text();
    try { err = JSON.parse(err).error || err; } catch (_) {}
    _adminBackupLog(`✗ ${file} download failed — ${err}`, 'err');
    if (resp.status === 404) adminLoadBackupStatus();
    return;
  }
  const blob = await resp.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = file;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
  _adminBackupLog(`✓ ${file} downloaded — ${blob.size} bytes`, 'ok');
}

// "copied" only when the copy is present in the mirror directory; the newest
// archive's recorded failure wins over the listing.
function _adminMirrorPill(d, last, b) {
  if (!d.mirror_dir) return '<span class="t">—</span>';
  if (last && last.file && b.file === last.file && last.mirrored === false) {
    return '<span class="pill warn">copy failed</span>';
  }
  return b.mirrored === true ? '<span class="pill ok">copied</span>'
                             : '<span class="pill dim">not copied</span>';
}

async function adminBackupNow() {
  const btn = document.getElementById('adminBackupNowBtn');
  if (btn) btn.disabled = true;
  _adminBackupLog('running a backup now…');
  try {
    const r = await fetch('/api/admin/backup-now', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) _adminBackupLog('✓ backup complete', 'ok');
    else _adminBackupLog('✗ ' + (d.error || ('HTTP ' + r.status)), 'err');
  } catch (e) {
    _adminBackupLog('✗ ' + e.message, 'err');
  }
  if (btn) btn.disabled = false;
  adminLoadBackupStatus();
}

// Backup settings card — the manager.backup.* catalog fields, rendered by the
// shared settings-field renderer and saved through /api/admin/settings.
let _adminBackupCfg = null;
const _adminBackupDirty = new Map();

async function adminLoadBackupSettings() {
  const host = document.getElementById('adminBackupSettingsBody');
  if (!host || !window.SettingsFields) return;
  try {
    const r = await fetch('/api/admin/settings');
    if (!r.ok) return;
    const d = await r.json();
    if (!d.ok) return;
    _adminBackupCfg = d;
    _adminBackupDirty.clear();
    adminRenderBackupSettings();
  } catch (e) { /* card stays on its last render */ }
}

function adminRenderBackupSettings() {
  const host = document.getElementById('adminBackupSettingsBody');
  if (!host || !_adminBackupCfg) return;
  const entries = _adminBackupCfg.entries.filter(e => e.path.indexOf('manager.backup.') === 0);
  host.innerHTML = SettingsFields.render(entries, _adminBackupCfg.values,
    _adminBackupCfg.defaults || {}, { secrets: _adminBackupCfg.secrets || {} });
  if (host._bkBound) return;
  host._bkBound = true;
  const byPath = () => new Map(_adminBackupCfg.entries.map(e => [e.path, e]));
  const note = (el) => {
    const e = byPath().get(el.dataset.path);
    if (!e) return;
    _adminBackupDirty.set(e.path, e.secret && e.type !== 'list'
      ? el.value : SettingsFields.readInput(el, e));
    const row = el.closest('.settings-row');
    if (row) row.classList.add('dirty');
  };
  host.addEventListener('input', ev => { const el = ev.target.closest('.st-input'); if (el) note(el); });
  host.addEventListener('change', ev => { const el = ev.target.closest('.st-input'); if (el) note(el); });
  host.addEventListener('click', ev => {
    const tg = ev.target.closest('.mc-toggle[data-type="bool"]');
    if (tg) {
      tg.classList.toggle('on');
      tg.setAttribute('aria-pressed', String(tg.classList.contains('on')));
      note(tg);
      return;
    }
    const clr = ev.target.closest('[data-clear]');
    if (clr) { ev.preventDefault(); _adminBackupDirty.set(clr.dataset.clear, null); clr.disabled = true; clr.textContent = 'Clear queued'; return; }
    const rst = ev.target.closest('[data-reset]');
    if (rst) { ev.preventDefault(); _adminBackupDirty.set(rst.dataset.reset, null); adminRenderBackupSettings(); return; }
    const rs = ev.target.closest('[data-restart]');
    if (rs) { ev.preventDefault(); _restartService(rs.dataset.restart); }
  });
}

async function adminSaveBackupSettings() {
  const msg = document.getElementById('adminBackupSettingsMsg');
  if (!_adminBackupDirty.size) { if (msg) { msg.className = 'msg'; msg.textContent = 'no changes'; } return; }
  if (msg) { msg.className = 'msg'; msg.textContent = 'saving…'; }
  let saved = null;
  try {
    const r = await fetch('/api/admin/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ changes: Object.fromEntries(_adminBackupDirty) }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) {
      const first = Object.values(d.errors || {})[0];
      if (msg) { msg.className = 'msg err'; msg.textContent = d.error || first || ('HTTP ' + r.status); }
      return;
    }
    if (msg) { msg.className = 'msg ok'; msg.textContent = '✓ saved'; }
    saved = d;
  } catch (e) {
    if (msg) { msg.className = 'msg err'; msg.textContent = e.message; }
    return;
  }
  await adminLoadBackupSettings();
  adminLoadBackupStatus();
  _adminShowRestartNotice(document.getElementById('adminBackupSettingsBody'), saved,
                          (_adminBackupCfg || {}).entries);
}

// Prepends the shared "restart required" notice to a settings card body after
// a save that changed non-hot fields, and refreshes the System Health pill.
function _adminShowRestartNotice(host, d, entries) {
  if (!host || !d || !(d.restart_required || []).length || !window.SettingsFields) return;
  const byPath = new Map((entries || []).map(e => [e.path, e]));
  host.insertAdjacentHTML('afterbegin',
    SettingsFields.restartNotice(d, p => (byPath.get(p) || { label: p }).label));
  if (typeof adminLoadHealth === 'function') adminLoadHealth();
}

// Queues every backup key as a clear so the server drops it back to its default.
function adminResetBackupSettings() {
  if (!_adminBackupCfg) return;
  _adminBackupCfg.entries
    .filter(e => e.path.indexOf('manager.backup.') === 0)
    .forEach(e => _adminBackupDirty.set(e.path, null));
  adminSaveBackupSettings();
}
