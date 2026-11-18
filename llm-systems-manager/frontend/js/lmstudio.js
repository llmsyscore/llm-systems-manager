// ---------------------------------------------------------------------------
// LM Studio metrics — from agent
// ---------------------------------------------------------------------------
let _lmsMetrics = {};

// Normalize for matching: replace slashes + hyphens with underscores, lowercase.
// Keep the @quant suffix so q4_k_m != iq4_xs.
function normId(id) { return (id || '').replace(/[\/\-]/g, '_').toLowerCase(); }

function _setEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '—';
}

// True when an LM Studio SUB-tab is the active view (the only place the LMS
// cards + log live). Used to gate LMS polls so agent-scoped pages don't fire
// cross-provider calls (issue #115).
function _lmsLogViewActive() {
  const t = _activeTab;
  const ds = _subTabState && _subTabState['dashboard'];
  const ls = _subTabState && _subTabState['llm'];
  return (t === 'dashboard' && ds === 'lmstudio') || (t === 'llm' && ls === 'lmstudio');
}
// Full metrics feed LLM Control · LM Studio and Overall; on Dashboards · LM Studio the host
// page polls its own data, so there only the header pill is kept current (light fetch).
function _lmsMetricsViewActive() {
  return _activeTab === 'overall' || (_activeTab === 'llm' && !!_subTabState && _subTabState.llm === 'lmstudio');
}

// Header LMS state pill from a /api/lmstudio/metrics payload.
function _updateLmsHeaderPill(d) {
  const lmsBanner = document.getElementById('lmsStateBanner');
  const lmsText   = document.getElementById('lmsStateText');
  if (!lmsBanner || !lmsText) return;
  if (d.agent_online !== true) {
    lmsBanner.className = 'state-banner state-unknown';
    lmsText.textContent  = 'LMS · offline';
    return;
  }
  // Any loaded model (in ps) regardless of active/idle.
  const ps = d.ps || [];
  const loadedRow = ps.find(p => p.status && p.status !== 'STOPPED') || d.active || null;
  if (!loadedRow) {
    lmsBanner.className = 'state-banner state-sleeping';
    lmsText.textContent  = 'LMS · no model loaded';
    return;
  }
  const modelShort = (loadedRow.model || loadedRow.identifier || '').split('@')[0].split('_').slice(-2).join('_') || 'model';
  const lmsStatus = ['IDLE', ''].includes((loadedRow.status || '').toUpperCase()) ? 'Idle' : 'Active';
  lmsBanner.className = lmsStatus === 'Active' ? 'state-banner state-awake' : 'state-banner state-sleeping';
  lmsText.textContent  = `LMS · ${lmsStatus} · ${modelShort}`;
}

let _lmsPillTick = 0;
async function fetchLMStudioMetrics() {
  if (document.hidden) return;
  // Full render only on Overall and LLM Control · LM Studio;
  // off-view only the header pill is kept current, on every 5th tick.
  const pillOnly = !_lmsMetricsViewActive();
  _lmsPillTick = (_lmsPillTick + 1) % 5;
  if (pillOnly && (_lmsPillTick !== 0 || _pillHidden('lmsStateBanner'))) return;
  const _lk = _agentClaimKey(pillOnly ? 'fetchLMStudioMetrics:pill' : 'fetchLMStudioMetrics', 'lms');
  if (!_claim(_lk)) return;
  const agentAtStart = typeof _selectedAgent === 'function' ? _selectedAgent('lms') : null;
  try {
    const d = await _fetchT(pillOnly ? '/api/lmstudio/metrics?light=1' : '/api/lmstudio/metrics', {}, 10000).then(r => r.json());
    // A reply for a previously selected agent must not paint (or seed) the new one.
    if (typeof _selectedAgent === 'function' && _selectedAgent('lms') !== agentAtStart) return;
    if (pillOnly) { _updateLmsHeaderPill(d); return; }
    _lmsMetrics = d;
    // Tuned load options (#916): refreshed on a host change or every 30 s.
    if (_lmsTunedAgent !== agentAtStart || Date.now() - _lmsTunedAt > 30000) {
      _lmsTunedAgent = agentAtStart; _lmsTunedAt = Date.now();
      await lmsLoadTuned();
    }

    const ps     = d.ps || [];
    const models = d.models || [];
    const online = d.agent_online === true;

    // LLM Control — LMS model cards
    renderLMSModelCards(ps, models);

    // LMS server control status
    // Use models presence as authoritative "server is up" indicator —
    // lms server status output format varies and can report false negatives.
    const srvOn = d.server?.on === true || (models && models.length > 0);
    _setLmsBtns(online && srvOn);
    renderLmsSettingsNotices(d, document.getElementById('lmsSettingsBanner'), document.getElementById('lmsServiceNote'));
    const _lmsPillEl = document.getElementById('lmsCtrlStatus');
    if (_lmsPillEl) _lmsPillEl.className = 'mc-pill ' + (srvOn ? 'p-active' : 'p-unloaded') + ' llm-sec-pill';
    _setEl('lmsCtrlStatus', srvOn ? `Server ON · port ${d.server?.port || 1234}` : 'Server OFF');
    document.querySelectorAll('#lmsCtrlBadge').forEach(el => {
      el.className = `status ${online ? 'status--ok' : 'status--crit'}`;
      el.innerHTML = '<span class="status__dot"></span>' + (online ? 'Agent online' : 'Agent offline');
    });

    _updateLmsHeaderPill(d);
  } catch(e) {
    console.warn('fetchLMStudioMetrics:', e);
  } finally {
    _release(_lk);
  }
}

function onLmsModelSortChange(v) {
  if (!VALID_MODEL_SORTS.includes(v)) return;
  if (typeof layout !== 'object' || !layout) layout = {};
  layout.lmsModelSort = v;
  try { saveLayout(); } catch (_) {}
  // Re-render with whatever ps/models the last fetch produced.
  if (_lmsLastPs || _lmsLastModels) renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
}
function _currentLmsModelSort() {
  const v = (layout && layout.lmsModelSort) || 'loaded_first';
  return VALID_MODEL_SORTS.includes(v) ? v : 'loaded_first';
}
let _lmsLastPs = null, _lmsLastModels = null, _lmsAliasesHydrated = false;
let _lmsTuned = {};   // model id → saved load preferences on the selected host (#916)
let _lmsTunedAt = 0, _lmsTunedAgent = null;

// Tools tab deep link for this host: Benchmark · Live or Autotune on the LM Studio provider (#916).
function lmsOpenTool(tool, modelId, opts) {
  if (typeof toolsDeepLink !== 'function') return;
  const agent = typeof _selectedAgent === 'function' ? _selectedAgent('lms') : null;
  toolsDeepLink(tool, modelId, { provider: 'lms', agent, ...(opts || {}) });
}

async function lmsLoadTuned() {
  try {
    const d = await _fetchT('/api/lmstudio/load-prefs', {}, 8000).then(r => r.json());
    _lmsTuned = (d && d.ok && d.models) || {};
  } catch (_) { _lmsTuned = {}; }
}

async function lmsForgetTuned(modelId) {
  const ok = await _themedConfirm({
    title: `Forget the tuned load options for "${adminEsc(modelId)}"?`,
    bodyHtml: 'Future loads from this dashboard use LM Studio\'s own defaults again. The model stays loaded as it is.',
    confirmLabel: 'Forget', cancelLabel: 'Cancel',
  });
  if (!ok) return;
  try {
    await _fetchT('/api/lmstudio/load-prefs?model=' + encodeURIComponent(modelId), { method: 'DELETE' }, 8000);
  } catch (e) { _toastErr('Error: ' + e); return; }
  await lmsLoadTuned();
  renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
}

function renderLMSModelCards(ps, models) {
  const container = document.getElementById('lmsModelCards');
  if (!container) return;
  // Skip while an inline alias edit is in progress — the 6s metrics
  // poll would otherwise wipe the input mid-keystroke. We still update
  // the cached ps/models below so the next render after commit reflects
  // the latest state.
  if (container.querySelector('.mc-name input, .mc-rowname input')) {
    _lmsLastPs = ps; _lmsLastModels = models;
    return;
  }

  // One-shot alias hydration: refreshLLMTab loads /api/llm/aliases on the
  // llama tab, but the user may land on the LMS tab first. Without this,
  // aliases set previously wouldn't render until they pop over to llama.
  if (!_lmsAliasesHydrated) {
    _lmsAliasesHydrated = true;
    fetch('/api/llm/aliases').then(r => r.json()).then(ar => {
      _llmAliases = (ar && typeof ar === 'object') ? ar : (_llmAliases || {});
      renderLMSModelCards(_lmsLastPs || ps, _lmsLastModels || models);
    }).catch(() => {});
  }

  // Cache the inputs so the sort dropdown can re-render without an extra fetch.
  _lmsLastPs = ps;
  _lmsLastModels = models;
  const sortSel = document.getElementById('lmsModelSortSel');
  if (sortSel && sortSel.value !== _currentLmsModelSort()) sortSel.value = _currentLmsModelSort();

  // Filter out embedding models
  const displayModels = (models || []).filter(m => !m.id.toLowerCase().includes('embed'));

  // Any ps rows not matched by a /v1/models entry (edge case)
  const psOnly = (ps || []).filter(p => {
    const pn = normId(p.identifier || p.model);
    return !displayModels.some(m => normId(m.id) === pn);
  }).filter(p => !(p.identifier || p.model || '').toLowerCase().includes('embed'));

  const allEntries = [
    ...displayModels.map(m => ({ modelId: m.id, psRow: ps.find(p => normId(p.identifier || p.model) === normId(m.id)) || null })),
    ...psOnly.map(p => ({ modelId: p.identifier || p.model, psRow: p })),
  ];

  if (!allEntries.length) {
    container.innerHTML = '<div style="color:var(--fg-dim);font-size:0.85em;">Waiting for agent...</div>';
    return;
  }

  // Apply the same grouping/sorting the llama cards use. Build the status
  // lookup the helper expects, then map sorted IDs back to entries.
  const entryById   = new Map(allEntries.map(e => [e.modelId, e]));
  const statusLookup = {};
  for (const e of allEntries) {
    const raw = ((e.psRow?.status) || '').toUpperCase();
    const loaded     = e.psRow != null;
    const processing = loaded && !['IDLE', 'STOPPED', ''].includes(raw);
    statusLookup[e.modelId] = { value: processing ? 'loaded' : (loaded ? 'sleeping' : 'unloaded') };
  }
  const groups = _buildSortedGroups([...entryById.keys()], _currentLmsModelSort(), statusLookup, _authorOfLms);

  const descriptor = (modelId) => {
    const { psRow } = entryById.get(modelId) || {};
    const rawStatus    = ((psRow?.status) || '').toUpperCase();
    const isLoaded     = psRow != null;
    const isProcessing = isLoaded && !['IDLE', 'STOPPED', ''].includes(rawStatus);
    const busy         = MC.busyOf('lms', modelId);

    let pill;
    if      (busy)         pill = { state: 'busy',   label: busy };
    else if (isProcessing) pill = { state: 'active', label: rawStatus === 'PROCESSINGPROMPT' ? 'Processing' : rawStatus.charAt(0) + rawStatus.slice(1).toLowerCase() };
    else if (isLoaded)     pill = { state: 'idle',   label: 'Loaded' };
    else                   pill = { state: 'unloaded', label: 'Unloaded' };

    const specs = psRow ? [
      psRow.size             && { k: 'Size',     v: psRow.size },
      psRow.quant            && { k: 'Quant',    v: psRow.quant },
      psRow.params           && { k: 'Params',   v: psRow.params },
      psRow.context          && { k: 'Context',  v: Number(psRow.context).toLocaleString() },
      psRow.parallel != null && { k: 'Parallel', v: psRow.parallel },
      psRow.device           && { k: 'Device',   v: psRow.device },
    ].filter(Boolean) : [];

    const csub = [
      _lmsTuned[modelId] ? 'tuned' : null,
      psRow?.quant ? MC.esc(psRow.quant) : null,
      psRow?.context ? 'ctx ' + MC.esc(Math.round(Number(psRow.context) / 1024) + 'k') : null,
      psRow?.device ? MC.esc(psRow.device) : null,
    ].filter(Boolean).join(' · ');

    return {
      id: modelId, actAttr: 'data-lmsact', renameAct: 'rename',
      name: aliasOrShort(modelId), repo: modelId,
      pill, specs, stats: [], fresh: null,
      primary: isLoaded ? { act: 'unload', icon: '⏹', label: 'Unload' } : { act: 'load', icon: '▶', label: 'Load' },
      buttons: [],
      menu: [...(isLoaded ? [{ act: 'reload', icon: '↺', label: 'Reload' }] : []),
             { act: 'bench', icon: '◷', label: 'Benchmark' }, { act: 'autotune', icon: '⌖', label: 'Autotune' },
             ...(_lmsTuned[modelId] ? [{ act: 'untune', icon: '↩', label: 'Forget tuned load options' }] : [])],
      transition: !!busy, csub,
      open: MC.isOpen('lms', modelId),
    };
  };

  const view = MC.viewOf(typeof layout === 'object' ? layout : null, 'lms');
  MC.syncSeg('lms');
  const filtered = groups
    .map(g => ({ ...g, ids: g.ids.filter(id => MC.filterMatch('lms', id, aliasOrShort(id))) }))
    .filter(g => g.ids.length);

  if (!filtered.length) {
    container.className = 'model-cards';
    container.innerHTML = '<div style="color:var(--fg-dim);font-size:0.85em;">No models match the filter.</div>';
    return;
  }

  if (view === 'list') {
    container.className = '';
    const rows = filtered.map(g => {
      const head = g.header ? MC.groupRow(g.header, g.ids.length, MC.isCollapsed('lms', g.header)) : '';
      if (g.header && MC.isCollapsed('lms', g.header)) return head;
      return head + g.ids.map(id => MC.row(descriptor(id))).join('');
    }).join('');
    container.innerHTML = `<div class="mc-listwrap"><div class="mc-list">${MC.rowHeader('', '')}${rows}</div></div>`;
  } else {
    const compactView = view === 'compact';
    container.className = 'mc-grid' + (compactView ? ' mc-compactgrid' : '');
    container.innerHTML = filtered.map(g => {
      const head = g.header ? MC.groupHeader(g.header, g.ids.length, MC.isCollapsed('lms', g.header)) : '';
      if (g.header && MC.isCollapsed('lms', g.header)) return head;
      return head + g.ids.map(id => compactView ? MC.compact(descriptor(id)) : MC.card(descriptor(id))).join('');
    }).join('');
  }

  MC.bindContainer(container, 'lms', () => renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []));
  if (!container._actBound) {
    container._actBound = true;
    container.addEventListener('click', ev => {
      const nameDiv = ev.target.closest('[data-lmsact="rename"]');
      if (nameDiv) { ev.stopPropagation(); startLmsCardRename(nameDiv, nameDiv.dataset.id); return; }
      const btn = ev.target.closest('button[data-lmsact]');
      if (!btn) return;
      const id  = btn.dataset.id;
      const act = btn.dataset.lmsact;
      if (act === 'load')   lmsLoad(id);
      else if (act === 'unload') lmsUnload(id);
      else if (act === 'reload') { MC.closeMenus(); lmsReload(id); }
      else if (act === 'bench') { MC.closeMenus(); lmsOpenTool('benchmark', id, { mode: 'live' }); }
      else if (act === 'autotune') { MC.closeMenus(); lmsOpenTool('autotune', id); }
      else if (act === 'untune') { MC.closeMenus(); lmsForgetTuned(id); }
      else if (act === 'rename-name') { MC.closeMenus(); startLmsCardRename(MC.nameElOf(btn), id); }
    });
  }
}

// Mirror of startCardRename but scoped to the LMS card. Reuses the same
// /api/llm/aliases store + _llmAliases cache. Operations on LMS models
// (load/unload/reload) always key off the underlying model ID, never
// the alias.
function startLmsCardRename(nameDiv, modelId) {
  if (!nameDiv || nameDiv.querySelector('input')) return;
  const input = document.createElement('input');
  input.type  = 'text';
  input.value = (_llmAliases && _llmAliases[modelId]) || '';
  input.placeholder = shortName(modelId);
  input.title = 'Editing alias (blank = use Model ID)';
  input.style.cssText = 'width:100%;background:var(--bg);border:1px solid var(--accent);border-radius:3px;color:var(--fg);font-size:0.85em;padding:2px 4px;box-sizing:border-box;';
  input.onclick = e => e.stopPropagation();

  let committed = false;
  // Pull the input out of the DOM before triggering a re-render so the
  // "skip while editing" guard in renderLMSModelCards (which checks for
  // .model-card-name input) doesn't bail on the commit pass.
  const _detach = () => { try { input.remove(); } catch (_) {} };
  const finish = async () => {
    if (committed) return;
    committed = true;
    const newAlias = _sanitizeAlias(input.value);
    const oldAlias = ((_llmAliases && _llmAliases[modelId]) || '').trim();
    _detach();
    if (newAlias === oldAlias) { renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []); return; }
    try {
      const r = await fetch('/api/llm/aliases', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({model_id: modelId, alias: newAlias}),
      }).then(r => r.json());
      if (r && r.aliases) _llmAliases = r.aliases;
      else if (newAlias) _llmAliases[modelId] = newAlias;
      else delete _llmAliases[modelId];
    } catch (_) {
      if (newAlias) _llmAliases[modelId] = newAlias;
      else delete _llmAliases[modelId];
    }
    renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
  };

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); finish(); }
    if (e.key === 'Escape') { committed = true; _detach(); renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []); }
  });
  input.addEventListener('blur', finish);

  nameDiv.textContent = '';
  nameDiv.appendChild(input);
  input.focus();
  input.select();
}

// ---------------------------------------------------------------------------
// LM Studio Server Control
// ---------------------------------------------------------------------------
let _lmsLogOpen  = false;
let _lmsLogTimer = null;

function startLmsLogRefresh() {
  fetchLmsLog();
  if (_lmsLogTimer) clearInterval(_lmsLogTimer);
  _lmsLogTimer = LivePause.every(fetchLmsLog, 8000);
}

function stopLmsLogRefresh() {
  if (_lmsLogTimer) {
    clearInterval(_lmsLogTimer);
    _lmsLogTimer = null;
  }
}

let _lmsSectionsInited = false;

function _initLMSSections() {
  if (_lmsSectionsInited) return;
  _lmsSectionsInited = true;
  // Section open/collapse defaults are owned by LLMSections (#767).
  // Open log panel
  const panel = document.getElementById('lmsLogPanel');
  if (panel) {
    panel.style.display = '';
    _lmsLogOpen = true;
    startLmsLogRefresh();
  }
}

// ===== LM Studio settings notices (#1422) begin =====
const [lmsSettingsNotices, renderLmsSettingsNotices] = (function () {
  const NOTE = 'LM Studio is not a system service. Start and Stop run the <code>lms</code> command as the agent\'s user.';
  // Notices for an lms.settings block: [{text, buttons}]; the not-a-service note (small) is always last.
  function notices(settings, server) {
    const out = [];
    const s = (settings && typeof settings === 'object') ? settings : null;
    if (s && 'cli_found' in s) {
      const running = s.running === true || (server && server.on === true);
      if (s.settings_source === 'detected') {
        const cmd = s.lms_cmd ? `, <code>lms</code> at ${_esc(s.lms_cmd)}` : '';
        out.push({ text: `Found LM Studio on this computer (API ${_esc(s.api_url || '')}${cmd}). Save these settings so they are kept after a restart.`,
                   buttons: '<button type="button" class="mcbtn mcbtn-pri" data-act="lms-settings-save">Save</button>' });
      }
      if (s.cli_found && !running) {
        const user = s.process_user || s.agent_user || '';
        out.push({ text: `LM Studio is installed but its server is not running. Press Start, or run <code>lms server start</code>${user ? ` as ${_esc(user)}` : ''}.`, buttons: '' });
      } else if (!s.cli_found && !running) {
        out.push({ text: 'The agent cannot find the <code>lms</code> command. Set its location as <code>LMS_CMD</code> in the agent settings.', buttons: '' });
      }
      if (s.process_user && s.process_user !== s.agent_user) {
        out.push({ text: `LM Studio runs as ${_esc(s.process_user)}; the agent runs as ${_esc(s.agent_user || '')}, so the <code>lms</code> command cannot see its models. Run both as the same user.`, buttons: '' });
      }
    }
    out.push({ text: NOTE, buttons: '', small: true });
    return out;
  }
  async function save() {
    const ok = await _themedConfirm({
      title: 'Save the detected settings?',
      bodyHtml: 'The agent will keep the LM Studio settings it found so they are kept after a restart.',
      confirmLabel: 'Save', cancelLabel: 'Cancel',
    });
    if (!ok) return;
    try {
      const resp = await fetch('/api/lmstudio/settings/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      let j = {};
      try { j = await resp.json(); } catch (_) {}
      if (!resp.ok || j.ok === false) { _toastErr(String(j.detail || j.error || 'Save failed')); return; }
      fetchLMStudioMetrics();
    } catch (e) { _toastErr('Save error: ' + e); }
  }
  function _paint(el, html) {
    el.style.display = html ? '' : 'none';
    if (el.innerHTML !== html) el.innerHTML = html;
  }
  // Draws the notices into container and the small note into noteEl (or container when absent); clears both offline.
  function render(d, container, noteEl) {
    if (!container) return;
    if (!container._lmsBound) {
      container._lmsBound = true;
      container.addEventListener('click', ev => { if (ev.target.closest('[data-act="lms-settings-save"]')) save(); });
    }
    let banner = '', note = '';
    if (d && d.agent_online === true) {
      const list = notices(d.settings, d.server);
      banner = list.filter(n => !n.small).map(n => `<div class="mc-mode-note"><span class="mc-mode-text">${n.text}</span>`
        + (n.buttons ? `<span class="mc-mode-actions">${n.buttons}</span>` : '') + '</div>').join('');
      banner = banner ? `<div class="mc-mode-banner">${banner}</div>` : '';
      note = list.filter(n => n.small).map(n => `<div class="mc-mode-hint">${n.text}</div>`).join('');
    }
    if (noteEl) { _paint(container, banner); _paint(noteEl, note); } else _paint(container, banner + note);
  }
  return [notices, render];
})();
// ===== LM Studio settings notices (#1422) end =====

async function lmsServerAction(action) {
  const _lmsPrompts = {
    stop:    { title: 'Stop the LM Studio server?',    body: 'Any active inference will be interrupted.', label: 'Stop' },
    start:   { title: 'Start the LM Studio server?',   body: '',                                          label: 'Start' },
    restart: { title: 'Restart the LM Studio server?', body: 'Any active inference will be interrupted.', label: 'Restart' },
  };
  if (_lmsPrompts[action]) {
    const p = _lmsPrompts[action];
    const ok = await _themedConfirm({
      title:        p.title,
      bodyHtml:     p.body,
      confirmLabel: p.label,
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
  }
  const statusEl = document.getElementById('lmsCtrlStatus');
  statusEl.style.color = 'var(--warn)';
  statusEl.textContent = action === 'status' ? 'Checking...' : `${action.charAt(0).toUpperCase() + action.slice(1)}ing...`;
  try {
    const r = await fetch(`/api/lmstudio/server/${action}`, {
      method: action === 'status' ? 'GET' : 'POST'
    }).then(r => r.json());
    statusEl.style.color = r.ok ? 'var(--ok)' : 'var(--crit)';
    statusEl.textContent = r.output?.trim().split('\n')[0] || (r.ok ? 'OK' : r.error || 'failed');
    if (_lmsLogOpen && r.output) {
      const box = document.getElementById('lmsLogBox');
      if (box) { box.textContent = r.output; box.scrollTop = box.scrollHeight; }
    }
    if (action !== 'status') setTimeout(fetchLMStudioMetrics, 2000);
  } catch(e) {
    statusEl.style.color = 'var(--crit)';
    statusEl.textContent = String(e);
  }
  setTimeout(() => { statusEl.textContent = ''; statusEl.style.color = 'var(--fg-dim)'; }, 12000);
}

function toggleLmsLog() {
  const panel = document.getElementById('lmsLogPanel');
  _lmsLogOpen = !_lmsLogOpen;
  panel.style.display = _lmsLogOpen ? '' : 'none';
  if (_lmsLogOpen) startLmsLogRefresh();
  else stopLmsLogRefresh();
}

async function fetchLmsLog() {
  // Don't poll the LMS log from non-LMS views (e.g. the llama.cpp sub-tab) —
  // a leftover timer would otherwise hit /api/lmstudio/server/log there (#115).
  if (!_lmsLogViewActive()) return;
  const box = document.getElementById('lmsLogBox');
  if (!box) return;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  try {
    const r = await fetch('/api/lmstudio/server/log').then(r => r.json());
    const lines = r.lines || [];
    const text  = lines.length ? lines.join('\n') : (r.error || '(no log lines found)');
    if (box.textContent !== text) {
      box.textContent = text;
      // Always scroll on first load (empty box), otherwise only if already at bottom
      if (atBottom || box.textContent === '') box.scrollTop = box.scrollHeight;
    }
  } catch(e) {
    box.textContent = 'Error: ' + e;
  }
}

function popOutLmsLog() {
  const box = document.getElementById('lmsLogBox');
  const content = box ? box.textContent : '';
  const win = window.open('', 'lmslog', 'width=900,height=600,resizable=yes,scrollbars=yes,toolbar=no,menubar=no');
  if (!win) { _toastErr('Pop-out blocked — allow pop-ups for this page.'); return; }
  win.document.write(`<!DOCTYPE html><html><head><title>LM Studio Server Log</title>
  <style>*{box-sizing:border-box;margin:0;padding:0;}body{background:#0a0a0a;color:#8a8;font-family:monospace;font-size:0.88em;display:flex;flex-direction:column;height:100vh;}
  #toolbar{background:var(--bg);border-bottom:1px solid var(--bg-card-alt);display:flex;align-items:center;gap:10px;padding:8px 12px;flex-shrink:0;}
  #toolbar span{color:var(--fg-dim);font-size:0.85em;}#log{flex:1;overflow-y:auto;padding:12px;white-space:pre-wrap;word-break:break-all;}</style>
  </head><body><div id="toolbar"><span>LM Studio Server Log</span></div>
  <div id="log">${content.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>
  <script>document.getElementById('log').scrollTop=document.getElementById('log').scrollHeight;<\/script>
  </body></html>`);
}

function fullscreenLmsLog() {
  const box = document.getElementById('lmsLogBox');
  if (!box) return;
  if (box.requestFullscreen) box.requestFullscreen();
  else if (box.webkitRequestFullscreen) box.webkitRequestFullscreen();
}

// LMS log resize handle
(function() {
  let dragging = false, startY = 0, startH = 0;
  document.addEventListener('DOMContentLoaded', () => {
    const handle = document.getElementById('lmsLogResizeHandle');
    const box    = document.getElementById('lmsLogBox');
    if (!handle || !box) return;
    handle.addEventListener('mousedown', e => {
      dragging = true; startY = e.clientY; startH = box.offsetHeight;
      document.body.style.userSelect = 'none'; e.preventDefault();
    });
    document.addEventListener('mousemove', e => {
      if (!dragging) return;
      box.style.height = Math.max(80, Math.min(window.innerHeight * 0.9, startH + (e.clientY - startY))) + 'px';
    });
    document.addEventListener('mouseup', () => { dragging = false; document.body.style.userSelect = ''; });
  });
})();

async function lmsLoad(modelId) {
  {
    const ok = await _themedConfirm({
      title:        `Load "${adminEsc(modelId)}" in LM Studio?`,
      bodyHtml:     '',
      confirmLabel: 'Load',
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
  }
  if (!_actionClaim('lmsLoad:' + modelId)) return;
  MC.setBusy('lms', modelId, 'Loading…');
  renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
  try {
    const r = await _fetchT('/api/lmstudio/load', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 60000).then(r => r.json());
    if (!r.ok) _toastErr('Load failed: ' + (r.error || JSON.stringify(r.response)));
    setTimeout(fetchLMStudioMetrics, 2000);
  } catch(e) {
    _toastErr('Error: ' + e);
  } finally {
    _actionRelease('lmsLoad:' + modelId);
    MC.clearBusy('lms', modelId);
    renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
  }
}

async function lmsUnload(modelId) {
  {
    const ok = await _themedConfirm({
      title:        `Unload "${adminEsc(modelId)}"?`,
      bodyHtml:     '',
      confirmLabel: 'Unload',
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
  }
  if (!_actionClaim('lmsUnload:' + modelId)) return;
  MC.setBusy('lms', modelId, 'Unloading…');
  renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
  try {
    const r = await _fetchT('/api/lmstudio/unload', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 30000).then(r => r.json());
    if (!r.ok) _toastErr('Unload failed: ' + (r.error || JSON.stringify(r.response)));
    setTimeout(fetchLMStudioMetrics, 2000);
  } catch(e) {
    _toastErr('Error: ' + e);
  } finally {
    _actionRelease('lmsUnload:' + modelId);
    MC.clearBusy('lms', modelId);
    renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
  }
}

async function lmsReload(modelId) {
  {
    const ok = await _themedConfirm({
      title:        `Reload "${adminEsc(modelId)}"?`,
      bodyHtml:     'This will unload then reload the model.',
      confirmLabel: 'Reload',
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
  }
  if (!_actionClaim('lmsReload:' + modelId)) return;
  MC.setBusy('lms', modelId, 'Reloading…');
  renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
  try {
    // Unload
    const ur = await _fetchT('/api/lmstudio/unload', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 30000).then(r => r.json());
    if (!ur.ok) { _toastErr('Reload failed on unload: ' + (ur.error || JSON.stringify(ur))); return; }

    // Poll until unloaded (up to 20s). Network errors don't count as "still loaded".
    let unloaded = false;
    let netErrors = 0;
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 1000));
      try {
        await fetchLMStudioMetrics();
        const d = await _fetchT('/api/lmstudio/metrics', {}, 8000).then(r => r.json());
        const ps = d.ps || [];
        if (!ps.find(p => (p.identifier === modelId || p.model === modelId))) {
          unloaded = true; break;
        }
      } catch(e) {
        netErrors++;
        if (netErrors > 5) { _toastErr('Reload: lost connection to backend — aborting.'); return; }
      }
    }
    if (!unloaded) { _toastErr('Reload: model did not unload within 20 seconds.'); return; }

    // Reload
    const lr = await _fetchT('/api/lmstudio/load', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 60000).then(r => r.json());
    if (!lr.ok) _toastErr('Reload failed on load: ' + (lr.error || JSON.stringify(lr.response)));
    setTimeout(fetchLMStudioMetrics, 2000);
  } catch(e) {
    _toastErr('Reload error: ' + e);
  } finally {
    _actionRelease('lmsReload:' + modelId);
    MC.clearBusy('lms', modelId);
    renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []);
  }
}

async function lmsDownloadModel() {
  const modelId = document.getElementById('lmsDlModel')?.value?.trim();
  if (!modelId) return;
  const logEl = document.getElementById('lmsDlLog');
  if (logEl) logEl.textContent = `Requesting download of ${modelId}...`;
  try {
    const r = await fetch('/api/lmstudio/download', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }).then(r => r.json());
    if (logEl) logEl.textContent = JSON.stringify(r, null, 2);
  } catch(e) {
    if (logEl) logEl.textContent = 'Error: ' + e;
  }
}

// Wire the LMS models toolbar (filter, view segment) once at load.
if (typeof MC !== 'undefined' && document.getElementById('lmsMcSeg')) {
  MC.initToolbar('lms', {
    segId: 'lmsMcSeg', filterId: 'lmsMcFilter',
    render: () => renderLMSModelCards(_lmsLastPs || [], _lmsLastModels || []),
  });
}
