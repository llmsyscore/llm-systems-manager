
// ---------------------------------------------------------------------------
// LLM Control
// ---------------------------------------------------------------------------
let _llmModels        = [];   // from /v1/models
let _llmConfig        = {};   // from config.ini
let _llamaActiveSlots = 0;    // current active_slots from llama metrics (drives card color)
let _editingId   = null; // current model being edited (null = new)
let _editorIsDownload = false; // true when editor was opened via addDownloadedModel
let _dlLastRepo  = '';   // last downloaded repo for "add to config"
let _dlLastQuant = '';   // last quant filter used for download
let _dlEventSrc  = null;

let _llmAliases = {};   // model_id → friendly display name (manager-side, UI-only)
let _llmProfiles = {};   // model_id -> {active, profiles:{name:values}}

async function refreshLLMTab() {
  try {
    const [mr, cr, ar] = await Promise.all([
      fetch('/api/llm/models').then(r => r.json()),
      fetch('/api/llm/config').then(r => r.json()),
      fetch('/api/llm/aliases').then(r => r.json()).catch(() => ({})),
      // Refresh per-agent benchmark badges for the selected agent on switch.
      (typeof loadBenchmarkData === 'function' ? loadBenchmarkData() : Promise.resolve()),
    ]);
    _llmModels = mr.data || [];
    _llmConfig = cr;
    _llmAliases = (ar && typeof ar === 'object') ? ar : {};
    try {
      _llmProfiles = await fetch('/api/llm/profiles').then(r => r.json()) || {};
    } catch (_) { _llmProfiles = {}; }
    // Seed a "default" profile (from current config) for any RENDERED model that
    // has none yet — keyed off the config-id set the cards use, not the live
    // model list, so configured-but-offline models still get controls (#118).
    const _cfgIds = Object.keys(_llmConfig || {}).filter(k => k !== '*' && k !== '__DEFAULTS__');
    const _toSeed = _cfgIds.filter(id => !_llmProfiles[id]);
    await Promise.all(_toSeed.map(async id => {
      try {
        const r = await fetch('/api/llm/profiles/' + encodeURIComponent(id) + '/save', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ name: 'default', values: _llmConfig[id] || {}, make_active: true }),
        }).then(r => r.json());
        if (r.ok) _llmProfiles[id] = r.model;
      } catch (_) {}
    }));
    renderModelCards();
    setTimeout(_updateModelPerf, 100);
  } catch(e) { console.error('LLM tab refresh error:', e); }
}

function statusPill(status) {
  const s = (status?.value || 'unloaded').toLowerCase();
  const label = s.charAt(0).toUpperCase() + s.slice(1);
  const MOD_MAP = {
    loaded: 'ok', ready: 'ok',
    loading: 'warn',
    sleeping: 'note',
    unloaded: 'muted', idle: 'muted', 'idle-loaded': 'muted',
  };
  const mod = MOD_MAP[s] || 'muted';
  return `<span class="status status--${mod}"><span class="status__dot"></span>${label}</span>`;
}

function shortName(id) {
  const parts = id.split('/');
  return parts[parts.length - 1];
}

// Pop out a log <div> into a full-viewport themed overlay. We move the
// actual DOM node so every existing 'log.textContent += ...' streamer
// keeps writing into it live; on close the node is restored to its
// original parent at its original position, with its original inline
// style intact.
function popoutLog(logId, title) {
  const log = document.getElementById(logId);
  if (!log || log.dataset.popped === '1') return;
  const origParent  = log.parentNode;
  const origNext    = log.nextSibling;
  const origStyle   = log.getAttribute('style') || '';

  const overlay = document.createElement('div');
  overlay.className = 'log-popped';
  const frame = document.createElement('div');
  frame.className = 'log-popped-frame';
  const toolbar = document.createElement('div');
  toolbar.className = 'log-toolbar';
  const titleEl = document.createElement('span');
  titleEl.className = 'log-popped-title';
  titleEl.textContent = title || logId;
  const closeBtn = document.createElement('button');
  closeBtn.className = 'btn-log';
  closeBtn.textContent = '⤺ Close';
  toolbar.appendChild(titleEl);
  toolbar.appendChild(closeBtn);
  frame.appendChild(toolbar);
  frame.appendChild(log);
  overlay.appendChild(frame);
  document.body.appendChild(overlay);

  log.dataset.popped = '1';
  // Size to content: max-width/max-height let the browser shrink the
  // pre-wrap log to the longest line + line-count instead of forcing the
  // overlay frame to fill the viewport. The min-height keeps short
  // outputs from collapsing to a one-liner.
  log.style.cssText = 'width:auto;height:auto;max-width:92vw;max-height:calc(92vh - 50px);min-height:120px;resize:none;margin:0;border-radius:0;border:none;border-top:1px solid var(--border);overflow:auto;white-space:pre;word-break:normal;';
  log.scrollTop = log.scrollHeight;

  const restore = () => {
    document.removeEventListener('keydown', keyHandler);
    overlay.removeEventListener('click', backdropHandler);
    log.dataset.popped = '';
    log.setAttribute('style', origStyle);
    if (origNext) origParent.insertBefore(log, origNext);
    else          origParent.appendChild(log);
    overlay.remove();
  };
  const keyHandler      = (e) => { if (e.key === 'Escape') restore(); };
  const backdropHandler = (e) => { if (e.target === overlay) restore(); };
  closeBtn.addEventListener('click', restore);
  document.addEventListener('keydown', keyHandler);
  overlay.addEventListener('click', backdropHandler);
}

function aliasOrShort(id) {
  const a = (_llmAliases && _llmAliases[id] || '').trim();
  return a || shortName(id);
}

// Sanitizers for the two operator-supplied strings that hit the dashboard
// + config.ini. _esc()/adminEsc() already HTML-escape on render, but we
// also clamp the raw stored value: aliases never carry control chars or
// tags; Model IDs are restricted to the HF-repo charset so config.ini
// section names stay shell-safe and never collide with TOML special chars.
function _sanitizeAlias(s) {
  return String(s == null ? '' : s)
    .replace(/[\x00-\x1f\x7f<>]/g, '')   // strip control chars + angle brackets
    .trim()
    .slice(0, 80);
}
function _sanitizeModelId(s) {
  return String(s == null ? '' : s)
    .trim()
    .replace(/[^A-Za-z0-9._/:-]/g, '')
    .slice(0, 200);
}

const VALID_MODEL_SORTS = ['group_by_author', 'alphabetical', 'loaded_first'];
function _currentModelSort() {
  const v = (layout && layout.modelSort) || 'group_by_author';
  return VALID_MODEL_SORTS.includes(v) ? v : 'group_by_author';
}
function onModelSortChange(v) {
  if (!VALID_MODEL_SORTS.includes(v)) return;
  if (typeof layout !== 'object' || !layout) layout = {};
  layout.modelSort = v;
  try { saveLayout(); } catch (_) {}
  renderModelCards();
}

function _authorOf(modelId) {
  const i = String(modelId).indexOf('/');
  return i > 0 ? modelId.slice(0, i) : '(no author)';
}

function _statusRank(s) {
  // loaded first, then in-progress states, then everything else
  if (s === 'loaded')   return 0;
  if (s === 'sleeping') return 1;
  if (s === 'loading')  return 2;
  return 3;
}

function _buildSortedGroups(ids, sortMode, statusLookup, authorFn) {
  const _author = authorFn || _authorOf;
  const cmpAlpha = (a, b) => aliasOrShort(a).toLowerCase().localeCompare(aliasOrShort(b).toLowerCase());
  if (sortMode === 'alphabetical') {
    return [{ header: null, ids: [...ids].sort(cmpAlpha) }];
  }
  if (sortMode === 'loaded_first') {
    const sorted = [...ids].sort((a, b) => {
      const r = _statusRank(statusLookup[a]?.value || 'unloaded')
              - _statusRank(statusLookup[b]?.value || 'unloaded');
      if (r !== 0) return r;
      const auth = _author(a).toLowerCase().localeCompare(_author(b).toLowerCase());
      return auth !== 0 ? auth : cmpAlpha(a, b);
    });
    return [{ header: null, ids: sorted }];
  }
  // group_by_author (default)
  const groups = new Map();
  for (const id of ids) {
    const author = _author(id);
    if (!groups.has(author)) groups.set(author, []);
    groups.get(author).push(id);
  }
  const authors = [...groups.keys()].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  return authors.map(author => ({
    header: author,
    ids: groups.get(author).sort(cmpAlpha),
  }));
}

// LMS authors: raw IDs from /v1/models rarely carry a "vendor/" prefix
// (the format is typically "qwen3.5-9b@iq4_xs"). Fall back to the
// displayed name so an alias like "Bartowski / Qwen3.5-9B / IQ4_XS"
// still groups under Bartowski.
function _authorOfLms(id) {
  const raw = _authorOf(id);
  if (raw && raw !== '(no author)') return raw;
  const disp = aliasOrShort(id);
  const i = String(disp).indexOf('/');
  return i > 0 ? disp.slice(0, i).trim() : '(no author)';
}

function renderModelCards() {
  const container = document.getElementById('llmModelCards');
  // Skip re-render while an inline alias edit is in progress — otherwise
  // a background poll (status flip, periodic refresh) would replace
  // innerHTML and steal focus from the input the user is typing into.
  // The render will catch up on the next tick after the edit commits.
  if (container && container.querySelector('.model-card-name input')) return;

  const sortSel = document.getElementById('modelSortSel');
  if (sortSel && sortSel.value !== _currentModelSort()) sortSel.value = _currentModelSort();

  // Use config.ini sections as primary source — excluding global [*]
  const configIds = Object.keys(_llmConfig).filter(k => k !== '*' && k !== '__DEFAULTS__');

  if (!configIds.length) {
    container.innerHTML = '<div style="color:var(--fg-dim);font-size:0.85em;">No models found in config.</div>';
    return;
  }

  // Build a status lookup from llama-server models
  const statusLookup = {};
  (_llmModels || []).forEach(m => { statusLookup[m.id] = m.status; });

  const groups = _buildSortedGroups(configIds, _currentModelSort(), statusLookup);
  const renderCard = (modelId) => {
    const status     = statusLookup[modelId]?.value || 'unloaded';
    const cfg        = _llmConfig[modelId] || {};
    const isLoaded   = status === 'loaded';
    const isLoading  = status === 'loading';
    const isSleeping = status === 'sleeping';
    const safeModelId = modelId.replace(/[^a-z0-9]/gi, '_');
    // Seed perf cells from the last-known metric so a re-render doesn't blank them.
    const perfSeed = (isLoaded && typeof _llamaPerfSeed === 'function')
      ? _llamaPerfSeed(safeModelId) : { gen: '—', ppt: '—', ts: '' };

    let cardClass = '';
    if      (isLoaded && _llamaActiveSlots > 0) cardClass = 'loaded';      // green  — actively inferring
    else if (isLoaded)                          cardClass = 'idle-loaded';  // yellow — loaded but idle
    else if (isLoading)                         cardClass = 'loading';      // amber  — loading
    else if (isSleeping)                        cardClass = 'sleeping';     // yellow — sleeping

    const chips = [
      cfg['ctx-size']                             && { k: 'ctx',  v: Number(cfg['ctx-size']).toLocaleString() },
      (cfg['temperature'] || cfg['temp'])          ? { k: 'temp', v: (cfg['temperature'] || cfg['temp']) } : null,
      cfg['cache-type-k']                         && { k: 'ctk',  v: cfg['cache-type-k'] },
      cfg['reasoning']                            && { k: 'reasoning', v: cfg['reasoning'] },
    ].filter(Boolean).map(c =>
      `<span class="param-chip"><span class="pk">${_esc(String(c.k))}</span><span class="pv">${_esc(String(c.v))}</span></span>`
    ).join('');

    const mid = _esc(modelId);
    // Active config profile: dropdown when >1, static label when exactly 1.
    const prof = _llmProfiles[modelId] || { active: '', profiles: {} };
    const profNames = Object.keys(prof.profiles || {});
    const profSelect = profNames.length > 1
      ? `<select class="profile-select" data-act="profile-activate" data-id="${_esc(modelId)}" title="Active config profile">`
        + profNames.map(n => `<option value="${_esc(n)}" ${n === prof.active ? 'selected' : ''}>${_esc(n)}</option>`).join('')
        + `</select>`
      : (profNames.length === 1 ? `<span class="profile-active" title="Active profile">${_esc(prof.active)}</span>` : '');
    // Rename/delete the active profile; shown only when at least one exists.
    const profActions = (profNames.length >= 1 && prof.active)
      ? `<button class="profile-action-btn" data-act="profile-rename" data-id="${_esc(modelId)}" data-name="${_esc(prof.active)}" title="Rename profile">✎</button>`
        + `<button class="profile-action-btn" data-act="profile-delete" data-id="${_esc(modelId)}" data-name="${_esc(prof.active)}" title="Delete profile">🗑</button>`
      : '';
    return `
    <div class="model-card ${cardClass}" data-id="${mid}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px;">
        <div class="model-card-name" title="Click to edit alias (blank = use Model ID)" data-act="rename" data-id="${mid}">${_esc(aliasOrShort(modelId))}</div>
        <div style="display:flex;align-items:center;gap:6px;">${statusPill({value: status})}</div>
      </div>
      <div class="model-card-params">${chips}</div>
      <div class="model-card-actions">
        ${!isLoaded && !isLoading && !isSleeping ? `<button class="btn btn-stone-muted-gradient" data-act="load"   data-id="${mid}">▶ Load</button>` : ''}
        ${isLoaded || isLoading || isSleeping    ? `<button class="btn btn-amber-muted-gradient" data-act="unload" data-id="${mid}">⏹ Unload</button>` : ''}
        ${isLoaded || isSleeping                 ? `<button class="btn btn-stone-muted-gradient"  data-act="reload" data-id="${mid}">↺ Reload</button>` : ''}
        <button class="btn btn-slate-muted-gradient" data-act="edit"   data-id="${mid}">✎ Edit</button>
        <button class="btn btn-red-muted-gradient"  data-act="delete" data-id="${mid}">✕ Delete</button>
      </div>
      ${(profSelect || profActions) ? `<div class="model-card-profile">${profSelect}${profActions}</div>` : ''}
      ${_benchData[modelId] ? `
      <div class="bench-badge">
        <div style="display:flex;gap:6px;padding:3px 3px;">
          <span class="bench-badge-chip">ppt ${Number(_benchData[modelId].avg_ppt_tps ?? 0).toFixed(0)} t/s</span>
          <span class="bench-badge-chip">gen ${Number(_benchData[modelId].avg_gen_tps ?? 0).toFixed(1)} t/s</span>
        </div>
      </div>` : ''}
      ${isLoaded ? `<div class="model-card-perf" id="perf-${safeModelId}">
        <div style="display:flex;flex-direction:column;gap:4px;padding:1px 1px;flex:1;">
          <div style="display:flex;align-items:center;gap:4px;">
            <span class="perf-val" id="perf-gen-${safeModelId}">${perfSeed.gen}</span>
            <span class="perf-lbl">avg gen t/s</span>
          </div>
          <div style="display:flex;align-items:center;gap:4px;">
            <span class="perf-val" id="perf-ppt-${safeModelId}" style="color:var(--accent);">${perfSeed.ppt}</span>
            <span class="perf-lbl">avg prompt t/s</span>
          </div>
        </div>
        <span style="color:var(--fg-faint);font-size:0.82em;align-self:flex-end;" id="perf-ts-${safeModelId}">${perfSeed.ts}</span>
      </div>` : ''}
    </div>`;
  };

  // Render grouped output. For non-grouping modes header is null and we emit
  // a flat list. For "Group by author" each group gets a small header strip.
  container.innerHTML = groups.map(g => {
    const cards = g.ids.map(renderCard).join('');
    if (!g.header) return cards;
    return `<div class="model-group-header" style="grid-column:1/-1;">
      <span>${_esc(g.header)}</span>
      <span class="rule"></span>
      <span class="count">${g.ids.length}</span>
    </div>${cards}`;
  }).join('');

  // Delegated click handler — avoids string interpolation of modelId into onclick attrs
  if (!container._actBound) {
    container._actBound = true;
    container.addEventListener('click', ev => {
      const el = ev.target.closest('[data-act]');
      if (!el) return;
      const id  = el.dataset.id;
      const act = el.dataset.act;
      if (act === 'load')   confirmLoad(id);
      else if (act === 'unload') confirmUnload(id);
      else if (act === 'reload') confirmReload(id);
      else if (act === 'edit')   openEditModel(id);
      else if (act === 'delete') confirmDelete(id);
      else if (act === 'rename') startCardRename(ev, id);
      else if (act === 'profile-rename') renameProfile(id, el.dataset.name);
      else if (act === 'profile-delete') deleteProfile(id, el.dataset.name);
    });
    container.addEventListener('change', ev => {
      const sel = ev.target.closest('select[data-act="profile-activate"]');
      if (sel) activateProfile(sel.dataset.id, sel.value);
    });
  }
}


// ----- Load / Unload -----

async function confirmLoad(modelId) {
  const loaded = _llmModels.find(m => m.status?.value === 'loaded');
  const title = loaded
    ? `Unload "${adminEsc(shortName(loaded.id))}" and load "${adminEsc(shortName(modelId))}"?`
    : `Load "${adminEsc(shortName(modelId))}"?`;
  const ok = await _themedConfirm({
    title,
    bodyHtml:     loaded ? 'The currently loaded model will be unloaded first.' : '',
    confirmLabel: 'Load',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  loadModel(modelId);
}

async function loadModel(modelId) {
  if (!_actionClaim('load:' + modelId)) return;
  const card = document.querySelector(`[data-id="${CSS.escape(modelId)}"]`);
  if (card) card.style.opacity = '0.5';
  try {
    const resp = await _fetchT('/api/llm/load', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 60000);
    if (typeof _notePinOverride === 'function') _notePinOverride(resp, modelId);
    const r = await resp.json();
    if (!r.ok) alert('Load failed: ' + (r.error || JSON.stringify(r)));
  } catch(e) {
    alert('Load error: ' + e);
  } finally {
    _actionRelease('load:' + modelId);
  }
  await refreshLLMTab();
}

async function confirmUnload(modelId) {
  const ok = await _themedConfirm({
    title:        `Unload "${adminEsc(shortName(modelId))}"?`,
    bodyHtml:     '',
    confirmLabel: 'Unload',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  unloadModel(modelId);
}

async function confirmReload(modelId) {
  const ok = await _themedConfirm({
    title:        `Reload "${adminEsc(shortName(modelId))}"?`,
    bodyHtml:     'This will unload, verify unloaded, then reload.',
    confirmLabel: 'Reload',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  reloadModel(modelId);
}

// Reloading is a multi-step process: unload the model, poll until it's fully unloaded (with a timeout), then load it again. This is useful for applying config changes that require a reload, without having to manually click unload then load.
async function reloadModel(modelId) {
  if (!_actionClaim('reload:' + modelId)) return;
  const card = document.querySelector(`[data-id="${CSS.escape(modelId)}"]`);
  if (card) card.style.opacity = '0.5';
  try {
    const ur = await _fetchT('/api/llm/unload', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 30000);
    if (typeof _notePinOverride === 'function') _notePinOverride(ur, modelId);
    // Poll until unloaded (up to 15s). Don't treat network errors as "still loaded".
    let verified = false;
    let netErrors = 0;
    for (let i = 0; i < 15; i++) {
      await new Promise(r => setTimeout(r, 1000));
      try {
        const mr = await _fetchT('/api/llm/models', {}, 8000).then(r => r.json());
        const m = (mr.data || []).find(m => m.id === modelId);
        if (!m || !['loaded','loading'].includes(m.status?.value)) { verified = true; break; }
      } catch(_) {
        if (++netErrors > 5) { alert('Reload: lost connection to backend — aborting.'); return; }
      }
    }
    if (!verified) {
      alert('Reload failed: model did not unload within 15 seconds.');
    } else {
      const lresp = await _fetchT('/api/llm/load', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({model: modelId})
      }, 60000);
      if (typeof _notePinOverride === 'function') _notePinOverride(lresp, modelId);
      const lr = await lresp.json();
      if (!lr.ok) alert('Reload error on load: ' + (lr.error || 'unknown'));
    }
  } catch(e) {
    alert('Reload error: ' + e);
  } finally {
    _actionRelease('reload:' + modelId);
  }
  if (card) card.style.opacity = '';
  await refreshLLMTab();
}

// Activate a profile: server-side activate, write its values to config.ini,
// then reload-if-loaded (with confirm) else refresh (#118).
// Keep the active profile's values in sync with a config write (#118).
async function _syncActiveProfile(modelId, values) {
  const ap = (_llmProfiles[modelId] || {}).active;
  if (!ap) return;
  try {
    await fetch('/api/llm/profiles/' + encodeURIComponent(modelId) + '/save', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ name: ap, values, make_active: true }),
    });
  } catch (_) {}
}

async function activateProfile(modelId, name) {
  if (!_actionClaim('activate:' + modelId + ':' + name)) return;
  try {
    const r = await fetch('/api/llm/profiles/' + encodeURIComponent(modelId) + '/activate', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ name }),
    }).then(r => r.json());
    if (!r.ok) { await _themedAlert({ title: 'Activate failed', bodyHtml: _esc(r.error || 'unknown error'), danger: true }); return; }
    const cfg = {..._llmConfig};
    cfg[modelId] = r.values || {};
    delete cfg['__DEFAULTS__'];
    const sr = await fetch('/api/llm/config', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(cfg),
    }).then(r => r.json());
    if (!sr.ok) { await _themedAlert({ title: 'Config write failed', bodyHtml: _esc(sr.error || 'unknown error'), danger: true }); return; }
    const m = (_llmModels || []).find(x => x.id === modelId);
    const loaded = m && ['loaded', 'loading'].includes(m.status?.value);
    if (loaded) {
      const ok = await _themedConfirm({
        title: 'Reload model with the "' + _esc(name) + '" profile?',
        bodyHtml: 'This interrupts any active inference on this model.',
        confirmLabel: 'Reload', cancelLabel: 'Later',
      });
      if (ok) { await reloadModel(modelId); return; }
    }
    await refreshLLMTab();
  } finally {
    _actionRelease('activate:' + modelId + ':' + name);
  }
}

// Save the current editor values as a new named profile, then activate it (#118).
async function saveAsNewProfile(modelId) {
  const name = await _themedPrompt({
    title: 'New profile', bodyHtml: 'Name for the new config profile:', placeholder: 'e.g. chat',
  });
  if (!name) return;
  const result = collectEditorValues();
  if (!result) return;
  const r = await fetch('/api/llm/profiles/' + encodeURIComponent(modelId) + '/save', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ name, values: result.values, make_active: true }),
  }).then(r => r.json());
  if (!r.ok) { await _themedAlert({ title: 'Save profile failed', bodyHtml: _esc(r.error || 'unknown error'), danger: true }); return; }
  _llmProfiles[modelId] = r.model;
  await activateProfile(modelId, name);
  // Close the editor so its now-stale values can't be re-saved over the
  // default profile by a follow-up Save click (#162). Matches saveModel /
  // saveAndLoad, which both close on success.
  closeEditor();
  _themedToast('Profile "' + name + '" created and activated', { kind: 'ok' });
}

// Rename a profile (#118).
async function renameProfile(modelId, oldName) {
  const to = await _themedPrompt({ title: 'Rename profile', bodyHtml: 'New name:', value: oldName });
  if (!to || to === oldName) return;
  const r = await fetch('/api/llm/profiles/' + encodeURIComponent(modelId) + '/rename', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ from: oldName, to }),
  }).then(r => r.json());
  if (!r.ok) { await _themedAlert({ title: 'Rename failed', bodyHtml: _esc(r.error || 'unknown error'), danger: true }); return; }
  await refreshLLMTab();
}

// Delete a profile (#118).
async function deleteProfile(modelId, name) {
  const ok = await _themedConfirm({
    title: 'Delete profile "' + _esc(name) + '"?',
    bodyHtml: 'This cannot be undone.', confirmLabel: 'Delete', cancelLabel: 'Cancel', danger: true,
  });
  if (!ok) return;
  const wasActive = ((_llmProfiles[modelId] || {}).active) === name;
  const r = await fetch('/api/llm/profiles/' + encodeURIComponent(modelId) + '/delete', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ name }),
  }).then(r => r.json());
  if (!r.ok) { await _themedAlert({ title: 'Delete failed', bodyHtml: _esc(r.error || 'unknown error'), danger: true }); return; }
  _themedToast('Profile "' + name + '" deleted', { kind: 'ok' });
  if (wasActive && r.model && r.model.active) {
    // Deleting the active profile fell back to another — apply it (config + reload).
    await activateProfile(modelId, r.model.active);
  } else {
    await refreshLLMTab();
  }
}

async function unloadModel(modelId) {
  if (!_actionClaim('unload:' + modelId)) return;
  try {
    const ur = await _fetchT('/api/llm/unload', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model: modelId})
    }, 30000);
    if (typeof _notePinOverride === 'function') _notePinOverride(ur, modelId);
  } catch(e) {
    alert('Unload error: ' + e);
  } finally {
    _actionRelease('unload:' + modelId);
  }
  await refreshLLMTab();
}

// ----- Delete -----

async function confirmDelete(modelId) {
  // One modal with a checkbox for cache deletion, instead of two stacked
  // browser confirm() dialogs. The previous pattern was brittle: clicking
  // Cancel on the cache prompt (intended as "don't delete the file") also
  // aborted the config removal because confirm() returning false ended the
  // whole flow.
  const result = await openDeleteModelModal(modelId);
  if (!result) return;  // user clicked Cancel
  deleteModel(modelId, result.deleteCache);
}

// Builds a one-shot delete-confirmation modal with a "Also delete from disk"
// checkbox. Returns a Promise that resolves to {deleteCache: bool} on Confirm,
// or null on Cancel/Esc/backdrop-click.
function openDeleteModelModal(modelId) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:color-mix(in srgb, var(--bg) 60%, transparent);'
      + 'z-index:9999;display:flex;align-items:center;justify-content:center;'
      + 'backdrop-filter:blur(4px);';

    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
      + 'padding:20px 22px;min-width:380px;max-width:520px;color:var(--fg);'
      + 'font-family:system-ui,-apple-system,sans-serif;box-shadow:0 8px 32px color-mix(in srgb, var(--bg) 50%, transparent);';

    const safeName = shortName(modelId);
    const escName  = String(safeName).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    box.innerHTML = `
      <div style="font-size:1.05em;font-weight:600;margin-bottom:8px;color:var(--fg);">Delete model card</div>
      <div style="font-size:0.88em;color:var(--fg);margin-bottom:16px;line-height:1.4;">
        Remove <span style="color:var(--fg);font-family:monospace;">${escName}</span> from the llama.cpp config.
      </div>
      <label style="display:flex;align-items:flex-start;gap:8px;font-size:0.88em;color:var(--fg);
                    cursor:pointer;padding:10px 12px;background:var(--bg);border:1px solid var(--border);
                    border-radius:6px;margin-bottom:18px;">
        <input type="checkbox" id="delModalCacheCb" style="margin-top:2px;flex-shrink:0;">
        <span>
          <span style="color:var(--fg);">Also delete the model file from the HuggingFace cache</span>
          <span style="display:block;color:var(--fg-muted);font-size:0.82em;margin-top:3px;line-height:1.35;">
            Only this quant is deleted; sibling quants in the same repo (e.g.
            Q4_K_M when you delete Q5_K_M) are kept. Frees disk space.
          </span>
        </span>
      </label>
      <div style="display:flex;justify-content:flex-end;gap:8px;">
        <button id="delModalCancel" style="background:var(--bg-card-alt);color:var(--fg);border:1px solid var(--border);
                border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;">Cancel</button>
        <button id="delModalConfirm" style="background:var(--crit);color:#fff;border:1px solid var(--border);
                border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">Delete</button>
      </div>`;

    overlay.appendChild(box);
    document.body.appendChild(overlay);

    const cleanup = (val) => {
      document.removeEventListener('keydown', keyHandler);
      overlay.remove();
      resolve(val);
    };
    const keyHandler = (e) => {
      if (e.key === 'Escape') cleanup(null);
      else if (e.key === 'Enter') {
        const cb = document.getElementById('delModalCacheCb');
        cleanup({ deleteCache: !!(cb && cb.checked) });
      }
    };
    document.addEventListener('keydown', keyHandler);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(null); });
    document.getElementById('delModalCancel').addEventListener('click', () => cleanup(null));
    document.getElementById('delModalConfirm').addEventListener('click', () => {
      const cb = document.getElementById('delModalCacheCb');
      cleanup({ deleteCache: !!(cb && cb.checked) });
    });
    // Focus the checkbox so keyboard users can toggle it without reaching for the mouse.
    setTimeout(() => {
      const cb = document.getElementById('delModalCacheCb');
      if (cb) cb.focus();
    }, 0);
  });
}

async function deleteModel(modelId, deleteCache = false) {
  const url = '/api/llm/config/' + encodeURIComponent(modelId)
            + (deleteCache ? '?delete_cache=true' : '');
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  fetch('/api/llm/aliases/' + encodeURIComponent(modelId), {method: 'DELETE'}).catch(() => {});
  try {
    const r = await fetch(url, {method: 'DELETE'}).then(r => r.json());
    if (deleteCache) {
      if (r.cache_error) {
        await _themedAlert({
          title: 'Cache delete failed',
          bodyHtml: 'The config was removed, but the cache cleanup hit an issue:\n\n' + esc(r.cache_error),
          danger: true,
        });
      } else if (r.deleted_files && r.deleted_files.length) {
        console.log('Deleted from HF cache:', r.deleted_files);
      }
    }
  } catch (e) {
    await _themedAlert({
      title: 'Delete failed',
      bodyHtml: esc(String(e)),
      danger: true,
    });
  }
  closeEditor();
  await refreshLLMTab();
}
// ----- Rename -----
function startCardRename(evt, modelId) {
  evt.stopPropagation();
  const nameDiv = evt.currentTarget;
  if (nameDiv.querySelector('input')) return; // already editing
  const input = document.createElement('input');
  input.type = 'text';
  // Pre-fill with the current alias (empty if none) — the placeholder shows
  // what the card falls back to when the alias is blank. Editing here NEVER
  // touches the underlying Model ID / config.ini section name.
  input.value = (_llmAliases && _llmAliases[modelId]) || '';
  input.placeholder = shortName(modelId);
  input.title = 'Editing alias (blank = use Model ID)';
  input.style.cssText = 'width:100%;background:var(--bg);border:1px solid var(--accent);border-radius:3px;color:var(--fg);font-size:0.85em;padding:2px 4px;box-sizing:border-box;';
  input.onclick = e => e.stopPropagation();

  let committed = false;
  // Detach the input before triggering the re-render so the polling
  // guard in renderModelCards (which checks for .model-card-name input)
  // doesn't block the commit pass.
  const _detach = () => { try { input.remove(); } catch (_) {} };
  const finish = async () => {
    if (committed) return;
    committed = true;
    const newAlias = _sanitizeAlias(input.value);
    const oldAlias = ((_llmAliases && _llmAliases[modelId]) || '').trim();
    _detach();
    if (newAlias === oldAlias) { renderModelCards(); return; }
    try {
      const r = await fetch('/api/llm/aliases', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({model_id: modelId, alias: newAlias}),
      }).then(r => r.json());
      if (r && r.aliases) _llmAliases = r.aliases;
      else if (newAlias) _llmAliases[modelId] = newAlias;
      else delete _llmAliases[modelId];
    } catch (_) {
      // Network/server error — fall back to optimistic update so the UI
      // doesn't flicker the old value back.
      if (newAlias) _llmAliases[modelId] = newAlias;
      else delete _llmAliases[modelId];
    }
    renderModelCards();
  };

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); finish(); }
    if (e.key === 'Escape') { committed = true; _detach(); renderModelCards(); }
  });
  input.addEventListener('blur', finish);

  nameDiv.textContent = '';
  nameDiv.appendChild(input);
  input.focus();
  input.select();
}

// ----- Editor -----
// Editor fields are populated from EF_DEFAULTS, overridden by any model-specific config, and then by EF_SPECIAL_DEFAULTS for certain fields that should default to "on" or "off" instead of a numeric value.
const EF_DEFAULTS = {
  'temperature':        '0.80',
  'dynatemp-range':     '0.0',
  'dynatemp-exp':       '1.00',
  'top-p':              '0.95',
  'top-k':              '40',
  'min-p':              '0.05',
  'presence-penalty':   '0.0',
  'repeat-penalty':     '1.0',
  'ctx-size':           '65536',
  'batch-size':         '2048',
  'ubatch-size':        '2048',
  'n-gpu-layers':       '99',
  'predict':            '-1',
  'cache-type-k':       'f16',
  'cache-type-v':       'f16',
  'cache-ram':          '0',
  'flash-attn':         'on',
  'reasoning':          'auto',
  'reasoning-budget':   '-1',
  'swa-full':           'off',
  'swa-checkpoints':    '32',
  'fit':                'on',
  'fit-ctx':            '32768',
  'check-tensors':      'off',
};

// Some fields should default to "on" or "off" instead of a numeric value, even if the global default is numeric. This allows them to be toggled on by default for models that support them, without having to set a numeric default that may not make sense for all models.
const EF_SPECIAL_DEFAULTS = { 'mmap': 'on', 'direct-io': 'off' };
// Validation rules for editor fields: min/max values, whether they should be parsed as float or int, etc. Used to validate and clamp values on blur, and to show warnings if values are out of range when saving.
const EF_VALIDATION = {
  'temperature':      { min: 0,    max: 2,   float: true },
  'dynatemp-range':   { min: 0,    max: 2,   float: true },
  'dynatemp-exp':     { min: 0,    max: 2,   float: true },
  'top-p':            { min: 0,    max: 1,   float: true },
  'top-k':            { min: 0,    max: 500, float: false },
  'min-p':            { min: 0,    max: 1,   float: true },
  'presence-penalty': { min: 0,    max: 2,   float: true },
  'repeat-penalty':   { min: 0,    max: 2,   float: true },
  'ctx-size':         { min: 512 },
  'batch-size':       { min: 1 },
  'ubatch-size':      { min: 1 },
  'n-gpu-layers':     { min: 0,    max: 999 },
  'predict':          { min: -1 },
  'cache-ram':        { min: 0 },
  'reasoning-budget': { min: -1 },
  'swa-checkpoints':  { min: 0 },
  'fit-ctx':          { min: 0 },
};
// List of all editor fields, used to populate the editor form and to gather values when saving. Fields that have special default handling (like "mmap" and "direct-io") are still included here, but their defaults are handled in EF_SPECIAL_DEFAULTS instead of EF_DEFAULTS.
const EF_FIELDS = [
  'temperature','dynatemp-range','dynatemp-exp','top-p','top-k','min-p',
  'presence-penalty','repeat-penalty','ctx-size','batch-size','ubatch-size',
  'n-gpu-layers','predict',
  'cache-type-k','cache-type-v','cache-ram',
  'flash-attn','reasoning','reasoning-budget','swa-full','swa-checkpoints',
  'fit','fit-ctx','check-tensors'
];

function efId(key) { return 'ef-' + key; }

function setField(key, val) {
  const el = document.getElementById(efId(key));
  if (!el || val == null) return;
  el.value = val;
}

function getField(key) {
  const el = document.getElementById(efId(key));
  return el ? el.value.trim() : '';
}

function attachFieldValidators() {
  Object.entries(EF_VALIDATION).forEach(([key, rules]) => {
    const el = document.getElementById(efId(key));
    if (!el) return;
    el.addEventListener('blur', () => {
      const raw = el.value.trim();
      const def = EF_DEFAULTS[key] ?? '';
      if (raw === '') { el.value = def; return; }
      const num = rules.float ? parseFloat(raw) : parseInt(raw, 10);
      if (isNaN(num)) { el.value = def; return; }
      let clamped = num;
      if (rules.min !== undefined) clamped = Math.max(rules.min, clamped);
      if (rules.max !== undefined) clamped = Math.min(rules.max, clamped);
      el.value = rules.float ? clamped : Math.round(clamped);
    });
  });
}

function addCustomParam(k = '', v = '') {
  const container = document.getElementById('ef-custom-params');
  if (!container) return;
  const row = document.createElement('div');
  row.className = 'custom-param-row';
  const kInput = document.createElement('input');
  kInput.type = 'text'; kInput.className = 'cp-key'; kInput.placeholder = 'key'; kInput.value = k;
  const eq = document.createElement('span');
  eq.className = 'cp-eq'; eq.textContent = '=';
  const vInput = document.createElement('input');
  vInput.type = 'text'; vInput.className = 'cp-val'; vInput.placeholder = 'value'; vInput.value = v;
  const rm = document.createElement('button');
  rm.className = 'btn btn-gray-muted-gradient'; rm.textContent = '✕';
  rm.style.cssText = 'padding:4px 8px;font-size:0.8em;';
  rm.onclick = () => { row.remove(); return false; };
  row.append(kInput, eq, vInput, rm);
  container.appendChild(row);
}

// Inject the "Save as new profile" sibling into the editor footer (idempotent);
// hidden when adding a new (not-yet-saved) model (#118).
function _ensureSaveAsProfileBtn(modelId) {
  const actions = document.querySelector('#llmEditor .editor-actions');
  if (!actions) return;
  let btn = document.getElementById('saveAsProfileBtn');
  if (!btn) {
    btn = document.createElement('button');
    btn.id = 'saveAsProfileBtn';
    btn.className = 'btn btn-slate-muted-gradient';
    btn.textContent = 'Save as new profile';
    btn.onclick = () => saveAsNewProfile(document.getElementById('ef-id').value);
    const saveBtn = [...actions.querySelectorAll('button')].find(b => /^Save$/.test(b.textContent.trim()));
    if (saveBtn) saveBtn.insertAdjacentElement('afterend', btn);
    else actions.appendChild(btn);
  }
  btn.style.display = modelId ? '' : 'none';
}

function openEditModel(modelId) {
  _editingId = modelId;
  _editorIsDownload = false;
  document.getElementById('llmEditorTitle').textContent = 'Edit — ' + aliasOrShort(modelId);
  document.getElementById('ef-id').value    = modelId;
  document.getElementById('ef-id').disabled = true;
  const _aliasEl = document.getElementById('ef-alias');
  if (_aliasEl) _aliasEl.value = (_llmAliases && _llmAliases[modelId]) || '';
  _populateCopyFromProfile(modelId);
  const _slBtn = document.getElementById('saveAndLoadBtn');
  if (_slBtn) { _slBtn.textContent = 'Save & Load Model'; _slBtn.style.display = ''; }
  _ensureSaveAsProfileBtn(modelId);

  const cfg = _llmConfig[modelId] || {};

  EF_FIELDS.forEach(k => setField(k, cfg[k] ?? EF_DEFAULTS[k] ?? ''));

  const mmapSel = document.getElementById('ef-mmap');
  if (mmapSel) {
    if (cfg['no-mmap'] === 'on') mmapSel.value = 'off';
    else if (cfg['mmap']) mmapSel.value = cfg['mmap'];
    else mmapSel.value = EF_SPECIAL_DEFAULTS['mmap'];
  }

  const dioSel = document.getElementById('ef-direct-io');
  if (dioSel) {
    if (cfg['direct-io'] === 'on') dioSel.value = 'on';
    else if (cfg['no-direct-io'] === 'on') dioSel.value = 'off';
    else dioSel.value = EF_SPECIAL_DEFAULTS['direct-io'];
  }

  const customContainer = document.getElementById('ef-custom-params');
  if (customContainer) customContainer.innerHTML = '';
  const knownKeys = new Set([
    ...EF_FIELDS, 'mmap', 'no-mmap', 'direct-io', 'no-direct-io', 'hf-repo'
  ]);
  Object.entries(cfg).forEach(([k, v]) => { if (!knownKeys.has(k)) addCustomParam(k, v); });

  document.getElementById('llmEditor').style.display = '';
  document.getElementById('llmEditor').scrollIntoView({behavior:'smooth'});
}

function openAddModel() {
  _editingId = null;
  _editorIsDownload = false;
  document.getElementById('llmEditorTitle').textContent = 'Add New Model';
  document.getElementById('ef-id').value    = '';
  document.getElementById('ef-id').disabled = false;
  const _aliasEl2 = document.getElementById('ef-alias');
  if (_aliasEl2) _aliasEl2.value = '';
  const _slBtn = document.getElementById('saveAndLoadBtn');
  if (_slBtn) { _slBtn.textContent = 'Save & Load Model'; _slBtn.style.display = ''; }
  _ensureSaveAsProfileBtn(null);

  EF_FIELDS.forEach(k => setField(k, EF_DEFAULTS[k] ?? ''));

  const mmapSel = document.getElementById('ef-mmap');
  if (mmapSel) mmapSel.value = EF_SPECIAL_DEFAULTS['mmap'];
  const dioSel = document.getElementById('ef-direct-io');
  if (dioSel) dioSel.value = EF_SPECIAL_DEFAULTS['direct-io'];

  const customContainer = document.getElementById('ef-custom-params');
  if (customContainer) customContainer.innerHTML = '';

  _populateCopyFromProfile(null);

  document.getElementById('llmEditor').style.display = '';
  document.getElementById('llmEditor').scrollIntoView({behavior:'smooth'});
}

// Legacy "copy config from another model" control — distinct from the #118
// named config profiles (those use /api/llm/profiles).
function _populateCopyFromProfile(excludeId) {
  const copyRow = document.getElementById('copyFromProfileRow');
  const copySel = document.getElementById('ef-copy-source');
  if (!copyRow || !copySel) return;
  const profiles = Object.keys(_llmConfig || {})
    .filter(n => n !== '*' && n !== '__DEFAULTS__' && n !== excludeId)
    .sort((a, b) => a.localeCompare(b));
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  copySel.value = '';
  copySel.innerHTML = '<option value="">— select a profile —</option>' +
    profiles.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  copyRow.style.display = profiles.length ? '' : 'none';
}

function copyFromProfile() {
  const sel = document.getElementById('ef-copy-source');
  const src = sel ? sel.value : '';
  if (!src) return;
  const cfg = (_llmConfig || {})[src];
  if (!cfg) { alert('Profile not found — refresh and try again.'); return; }

  // Mirror openEditModel's population logic, but never touch ef-id so the
  // operator's new Model ID stays put.
  EF_FIELDS.forEach(k => setField(k, cfg[k] ?? EF_DEFAULTS[k] ?? ''));

  const mmapSel = document.getElementById('ef-mmap');
  if (mmapSel) {
    if (cfg['no-mmap'] === 'on') mmapSel.value = 'off';
    else if (cfg['mmap']) mmapSel.value = cfg['mmap'];
    else mmapSel.value = EF_SPECIAL_DEFAULTS['mmap'];
  }
  const dioSel = document.getElementById('ef-direct-io');
  if (dioSel) {
    if (cfg['direct-io'] === 'on') dioSel.value = 'on';
    else if (cfg['no-direct-io'] === 'on') dioSel.value = 'off';
    else dioSel.value = EF_SPECIAL_DEFAULTS['direct-io'];
  }

  const customContainer = document.getElementById('ef-custom-params');
  if (customContainer) customContainer.innerHTML = '';
  const knownKeys = new Set([
    ...EF_FIELDS, 'mmap', 'no-mmap', 'direct-io', 'no-direct-io', 'hf-repo'
  ]);
  Object.entries(cfg).forEach(([k, v]) => { if (!knownKeys.has(k)) addCustomParam(k, v); });
}

function closeEditor() {
  document.getElementById('llmEditor').style.display = 'none';
  _editingId = null;
}

function collectEditorValues() {
  const rawId   = document.getElementById('ef-id').value;
  const modelId = _sanitizeModelId(rawId);
  if (!modelId) { alert('Model ID is required (letters, digits, . _ / : -).'); return null; }
  if (modelId !== rawId.trim()) {
    document.getElementById('ef-id').value = modelId;
  }

  const values = {};
  EF_FIELDS.forEach(k => {
    const v = getField(k);
    if (v !== '') values[k] = v;
  });

  const mmapVal = document.getElementById('ef-mmap')?.value;
  delete values['mmap']; delete values['no-mmap'];
  if (mmapVal === 'off') values['no-mmap'] = 'on';
  else values['mmap'] = 'on';

  const dioVal = document.getElementById('ef-direct-io')?.value;
  delete values['direct-io']; delete values['no-direct-io'];
  if (dioVal === 'off') values['no-direct-io'] = 'on';
  else values['direct-io'] = 'on';

  document.querySelectorAll('#ef-custom-params .custom-param-row').forEach(row => {
    const k = row.querySelector('.cp-key').value.trim();
    const v = row.querySelector('.cp-val').value.trim();
    if (k) values[k] = v;
  });

  return {modelId, values};
}

attachFieldValidators();

async function saveModel() {
  const result = collectEditorValues();
  if (!result) return;
  const {modelId, values} = result;

  // Read current full config, update this section, save
  const cfg = {..._llmConfig};
  cfg[modelId] = values;
  delete cfg['__DEFAULTS__'];

  const r = await fetch('/api/llm/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(cfg)
  }).then(r => r.json());

  if (r.ok) {
    // Alias persists to a separate manager-side store so it never leaks
    // into config.ini (where llama-server's --alias flag would intercept).
    const aliasVal = _sanitizeAlias(document.getElementById('ef-alias')?.value);
    fetch('/api/llm/aliases', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({model_id: modelId, alias: aliasVal}),
    }).catch(() => {});
    // Keep the active profile in sync with the edited config (#118).
    await _syncActiveProfile(modelId, values);
    await refreshLLMTab();
    closeEditor();
  } else {
    alert('Save failed: ' + (r.error || 'unknown error'));
  }
}

async function saveAndLoad() {
  const result = collectEditorValues();
  if (!result) return;
  const {modelId, values} = result;

  if (_editorIsDownload) {
    {
      const ok = await _themedConfirm({
        title:        'Save config and restart llama.cpp?',
        bodyHtml:     'This will interrupt any active inference.',
        confirmLabel: 'Save & Restart',
        cancelLabel:  'Cancel',
      });
      if (!ok) return;
    }

    const cfg = {..._llmConfig};
    cfg[modelId] = values;
    delete cfg['__DEFAULTS__'];

    const sr = await fetch('/api/llm/config', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(cfg)
    }).then(r => r.json());

    if (!sr.ok) { alert('Save failed: ' + (sr.error || 'unknown')); return; }

    await _syncActiveProfile(modelId, values);
    closeEditor();

    const rr = await fetch('/api/llm/server/restart', {method: 'POST'}).then(r => r.json());
    if (rr.ok) {
      setTimeout(() => refreshLLMTab(), 3000);
    } else {
      alert('Restart failed: ' + (rr.error || 'unknown'));
    }

  } else {
    {
      const ok = await _themedConfirm({
        title:        `Save config and load "${adminEsc(shortName(modelId))}"?`,
        bodyHtml:     'This will unload any currently running model.',
        confirmLabel: 'Save & Load',
        cancelLabel:  'Cancel',
      });
      if (!ok) return;
    }

    const cfg = {..._llmConfig};
    cfg[modelId] = values;
    delete cfg['__DEFAULTS__'];

    const sr = await fetch('/api/llm/config', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(cfg)
    }).then(r => r.json());

    if (!sr.ok) { alert('Save failed: ' + (sr.error || 'unknown')); return; }

    await _syncActiveProfile(modelId, values);
    closeEditor();
    await loadModel(modelId);
  }
}

// ----- HF Downloader -----

async function startDownload() {
  const repo    = document.getElementById('dlRepo').value.trim();
  const include = document.getElementById('dlInclude').value.trim();
  if (!repo) { alert('Repo ID is required.'); return; }

  // Build include patterns from quant filter + checkboxes. The quant field
  // accepts a comma-separated list ("Q4,Q5") — each entry becomes its own
  // glob so hf gets --include for each.
  const patterns = [];
  include.split(',')
    .map(q => q.trim())
    .filter(q => q.length)
    .forEach(q => patterns.push(`*${q}*`));
  if (document.getElementById('dlChkConfig').checked)   patterns.push('config.json');
  if (document.getElementById('dlChkTemplate').checked) patterns.push('chat_template.jinja');
  if (document.getElementById('dlChkMmproj').checked)   patterns.push('mmproj*.gguf');

  const dryRun = document.getElementById('dlChkDryRun').checked;

  document.getElementById('dlLog').textContent  = '';
  document.getElementById('dlBtn').disabled     = true;
  _setDlRunning(true);
  const _addBox = document.getElementById('dlAddBtn');
  _addBox.style.display = 'none';
  _addBox.innerHTML = '';
  _dlLastRepo  = repo;
  _dlLastQuant = include;

  const r = await fetch('/api/llm/download', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({repo, patterns, dry_run: dryRun})
  }).then(r => r.json());

  if (!r.ok) {
    document.getElementById('dlLog').textContent = 'Error: ' + (r.error || 'unknown');
    document.getElementById('dlBtn').disabled = false;
    _setDlRunning(false);
    return;
  }

  if (_dlEventSrc) { try { _dlEventSrc.close(); } catch(_){} _dlEventSrc = null; }
  const src = await openAgentSse(
    '/api/llm/download/stream-info',
    '/api/llm/download/stream',
  );
  _dlEventSrc = src;
  const log = document.getElementById('dlLog');
  let _dlBuffer = '';
  const _isDryRun = dryRun;

  src.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'keepalive') return;
    if (msg.type === 'start') {
      log.textContent = `$ ${msg.cmd}\n\n`;
    } else if (msg.type === 'line') {
      if (!_isDryRun) {
        log.textContent += msg.text + '\n';
      } else {
        _dlBuffer += msg.text + '\n';
      }
      log.scrollTop = log.scrollHeight;
    } else if (msg.type === 'done') {
      if (_isDryRun) {
        // Dry run uses --format json — extract the JSON payload and format it
        let formatted = _dlBuffer;
        try {
          let txt = _dlBuffer.trim();
          const s = txt.search(/[\[{]/);
          const e = Math.max(txt.lastIndexOf(']'), txt.lastIndexOf('}'));
          if (s !== -1 && e > s) txt = txt.slice(s, e + 1);
          const parsed = JSON.parse(txt);
          const files = Array.isArray(parsed) ? parsed : [parsed];
          if (files.length) {
            formatted = 'Files that would be downloaded:\n\n' +
              files.map(f => `  ${f.file}\n    size: ${f.size || '?'}`).join('\n') + '\n';
          }
        } catch(_) {}
        log.textContent += formatted;
        log.textContent += '\n✓ Dry run complete — no files downloaded.\n';
      } else {
        // Real download — output was already streamed line by line
        if (msg.cancelled) {
          log.textContent += '\n✕ Cancelled.\n';
        } else if (msg.ok) {
          log.textContent += '\n✓ Download complete.\n';
          _renderAddDownloadedButtons();
        } else {
          log.textContent += `\n✗ Failed (exit ${msg.rc || msg.error}).\n`;
        }
      }
      document.getElementById('dlBtn').disabled = false;
      _setDlRunning(false);
      try { src.close(); } catch(_){}
      if (_dlEventSrc === src) _dlEventSrc = null;
    }
  };
  src.onerror = () => {
    // Ignore errors from a superseded stream
    if (_dlEventSrc !== src) { try { src.close(); } catch(_){} return; }
    log.textContent += '\n[stream disconnected]\n';
    document.getElementById('dlBtn').disabled = false;
    _setDlRunning(false);
    try { src.close(); } catch(_){}
    _dlEventSrc = null;
  };
}

function _setDlRunning(running) {
  const cancel = document.getElementById('dlCancelBtn');
  if (cancel) {
    cancel.style.display = running ? '' : 'none';
    cancel.disabled = false;
  }
}

async function cancelDownload() {
  const cancel = document.getElementById('dlCancelBtn');
  if (cancel) cancel.disabled = true;
  try {
    const r = await fetch('/api/llm/download/cancel', {method: 'POST'}).then(r => r.json());
    if (!r.ok) {
      const log = document.getElementById('dlLog');
      log.textContent += `\n[cancel failed: ${r.error || 'unknown'}]\n`;
      log.scrollTop = log.scrollHeight;
      if (cancel) cancel.disabled = false;
    }
  } catch (e) {
    if (cancel) cancel.disabled = false;
  }
}

let _llamaBuildEventSrc = null;

function closeLlamaBuildPanel() {
  if (_llamaBuildEventSrc) {
    try { _llamaBuildEventSrc.close(); } catch(_){}
    _llamaBuildEventSrc = null;
  }
  const p = document.getElementById('llamaBuildPanel');
  if (p) p.style.display = 'none';
}

// Reset the editor, HF download, cache, and llama build panels to empty state.
function resetLLMControlPanels() {
  if (typeof closeEditor === 'function') closeEditor();
  if (typeof closeLlamaBuildPanel === 'function') closeLlamaBuildPanel();
  if (_dlEventSrc) { try { _dlEventSrc.close(); } catch(_){} _dlEventSrc = null; }
  _dlLastRepo  = '';
  _dlLastQuant = '';
  const _set = (id, prop, val) => { const el = document.getElementById(id); if (el) el[prop] = val; };
  _set('dlRepo', 'value', '');
  _set('dlInclude', 'value', '');
  _set('dlChkConfig', 'checked', true);
  _set('dlChkTemplate', 'checked', true);
  _set('dlChkMmproj', 'checked', true);
  _set('dlChkDryRun', 'checked', true);
  _set('dlLog', 'textContent', 'Download output will appear here...');
  const _add = document.getElementById('dlAddBtn');
  if (_add) { _add.style.display = 'none'; _add.innerHTML = ''; }
  _set('dlBtn', 'disabled', false);
  if (typeof _setDlRunning === 'function') _setDlRunning(false);
  _set('cacheRmRepo', 'value', '');
  _set('cacheLog', 'textContent', 'Cache info will appear here...');
  // LM Studio download panel.
  _set('lmsDlModel', 'value', '');
  _set('lmsDlLog', 'textContent', 'Download status will appear here...');
}

async function startLlamaBuild() {
  {
    const _method = (typeof _llamaBuildMethod !== 'undefined' && _llamaBuildMethod) ? _llamaBuildMethod : '';
    const _label = _method ? ` via ${_method}` : '';
    const ok = await _themedConfirm({
      title:        `Update llama.cpp${_label}?`,
      bodyHtml:     `This installs/upgrades llama.cpp${_label} on the llama agent host. It can take several minutes.`,
      confirmLabel: 'Update',
      cancelLabel:  'Cancel',
    });
    if (!ok) return;
  }

  const panel = document.getElementById('llamaBuildPanel');
  const log   = document.getElementById('llamaBuildLog');
  const stat  = document.getElementById('llamaBuildStatus');
  const btn   = document.getElementById('llamaBtnBuild');
  if (panel) panel.style.display = '';
  if (log)   log.textContent = '';
  if (stat)  { stat.textContent = 'starting…'; stat.style.color = 'var(--fg-dim)'; }
  if (btn)   btn.disabled = true;

  let r;
  try {
    r = await fetch('/api/llm/build', { method: 'POST' }).then(r => r.json().then(j => ({ status: r.status, body: j })));
  } catch (e) {
    if (log) log.textContent = 'Error: ' + e + '\n';
    if (stat) { stat.textContent = 'failed'; stat.style.color = 'var(--crit)'; }
    if (btn) btn.disabled = false;
    return;
  }
  if (!r.body || !r.body.ok) {
    const err = (r.body && (r.body.error || r.body.detail)) || ('HTTP ' + r.status);
    if (log) log.textContent = 'Error: ' + err + '\n';
    if (stat) { stat.textContent = 'failed'; stat.style.color = 'var(--crit)'; }
    if (btn) btn.disabled = false;
    if (r.status === 409) alert('A build is already running.');
    return;
  }

  if (_llamaBuildEventSrc) { try { _llamaBuildEventSrc.close(); } catch(_){} _llamaBuildEventSrc = null; }
  const src = await openAgentSse('/api/llm/build/stream-info', '/api/llm/build/stream');
  _llamaBuildEventSrc = src;
  if (stat) stat.textContent = 'running…';

  src.onmessage = e => {
    let msg;
    try { msg = JSON.parse(e.data); } catch (_) { return; }
    if (msg.type === 'keepalive') return;
    if (msg.type === 'start') {
      const _m = msg.method ? `[method: ${msg.method}]\n` : '';
      if (log) log.textContent = _m + '$ ' + (msg.cmd || 'build') + '\n\n';
    } else if (msg.type === 'line') {
      if (log) {
        log.textContent += (msg.data || msg.text || '') + '\n';
        log.scrollTop = log.scrollHeight;
      }
    } else if (msg.type === 'done') {
      const rc = (typeof msg.rc !== 'undefined') ? msg.rc : (msg.ok ? 0 : 1);
      const ok = msg.ok === true || rc === 0;
      if (log) log.textContent += ok ? `\n✓ Build complete (exit ${rc}).\n` : `\n✗ Build failed (exit ${rc}).\n`;
      if (stat) {
        stat.textContent = ok ? `done (exit ${rc})` : `failed (exit ${rc})`;
        stat.style.color = ok ? 'var(--ok)' : 'var(--crit)';
      }
      try { src.close(); } catch(_){}
      if (_llamaBuildEventSrc === src) _llamaBuildEventSrc = null;
      if (btn) btn.disabled = false;
      if (!ok) { try { alert('llama.cpp build failed (exit ' + rc + '). See output panel.'); } catch(_){} }
    }
  };
  src.onerror = () => {
    if (_llamaBuildEventSrc !== src) { try { src.close(); } catch(_){} return; }
    if (log) log.textContent += '\n[stream disconnected]\n';
    if (stat) { stat.textContent = 'disconnected'; stat.style.color = 'var(--crit)'; }
    try { src.close(); } catch(_){}
    _llamaBuildEventSrc = null;
    if (btn) btn.disabled = false;
  };
}

function extractQuantId(raw) {
  if (!raw) return '';
  // Strip .gguf extension if the user pasted a full filename
  const s = raw.replace(/\.gguf$/i, '');
  // Extract standard GGUF quant identifier from the end of the string
  // Handles: IQ4_XS, Q4_K_M, Q8_0, BF16, F16, F32, etc.
  const m = s.match(/(IQ\d+(?:_[A-Z0-9]+)*|Q\d+(?:_[A-Z0-9]+)*|BF\d+|F\d+)$/i);
  return m ? m[1].toUpperCase() : s;
}

function _quantsFromFilter(raw) {
  // Split the comma-separated filter input into a deduped, ordered list of
  // quant tokens (normalized via extractQuantId). Empty input → [''] so
  // the no-quant single-file case still renders one "Add downloaded model"
  // button addressed to the bare repo.
  if (!raw || !raw.trim()) return [''];
  const seen = new Set();
  const out = [];
  raw.split(',').map(q => q.trim()).filter(q => q.length).forEach(q => {
    const norm = extractQuantId(q);
    if (norm && !seen.has(norm)) { seen.add(norm); out.push(norm); }
  });
  return out.length ? out : [''];
}

function _renderAddDownloadedButtons() {
  const box = document.getElementById('dlAddBtn');
  if (!box || !_dlLastRepo) return;
  const quants = _quantsFromFilter(_dlLastQuant);
  box.innerHTML = quants.map(q => {
    const label = q ? `+ Add ${q} to config` : '+ Add downloaded model to config';
    return `<button class="btn btn-slate-muted-gradient" data-act="add-downloaded" data-quant="${_esc(q)}">${_esc(label)}</button>`;
  }).join('');
  if (!box._addWired) {
    box.addEventListener('click', ev => {
      const b = ev.target.closest('button[data-act="add-downloaded"]');
      if (b) addDownloadedModel(b.dataset.quant);
    });
    box._addWired = true;
  }
  box.style.display = 'flex';
}

function addDownloadedModel(explicitQuant) {
  if (!_dlLastRepo) return;
  const quant   = (explicitQuant !== undefined)
    ? (explicitQuant || '')
    : extractQuantId(_dlLastQuant);
  const modelId = quant ? `${_dlLastRepo}:${quant}` : _dlLastRepo;
  openAddModel();   // resets _editorIsDownload = false; we override below
  _editorIsDownload = true;
  document.getElementById('ef-id').value = modelId;
  document.getElementById('llmEditorTitle').textContent = 'Add — ' + shortName(modelId);
  const btn = document.getElementById('saveAndLoadBtn');
  if (btn) btn.textContent = 'Save & Restart llama.cpp';
  document.getElementById('llmEditor').scrollIntoView({behavior:'smooth'});
}

// ----- Cache Management -----

async function loadCacheList() {
  const log = document.getElementById('cacheLog');
  log.textContent = 'Loading...';
  try {
    const r = await fetch('/api/llm/cache').then(r => r.json());
    if (!r.ok) { log.textContent = 'Error: ' + r.error; return; }
    const entries = r.data || [];
    if (!entries.length) {
      log.textContent = r.raw || '(cache is empty)';
      return;
    }
    let out = '';
    entries.forEach(e => {
      out += `${e.repo_id}\n`;
      out += `  type:          ${e.repo_type || '?'}\n`;
      out += `  size:          ${e.size || '?'}\n`;
      out += `  last accessed: ${e.last_accessed || '?'}\n`;
      out += `  last modified: ${e.last_modified || '?'}\n`;
      out += `  refs:          ${(e.refs || []).join(', ') || 'none'}\n`;
      out += '\n';
    });
    log.textContent = out;
  } catch(e) {
    log.textContent = 'Error: ' + e;
  }
}

function _streamCacheOp(url, body) {
  const log = document.getElementById('cacheLog');
  log.textContent = '';
  if (_dlEventSrc) { try { _dlEventSrc.close(); } catch(_){} _dlEventSrc = null; }

  fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})
  }).then(r => r.json()).then(async r => {
    if (!r.ok) { log.textContent = 'Error: ' + (r.error || 'unknown'); return; }
    const src = await openAgentSse(
      '/api/llm/download/stream-info',
      '/api/llm/download/stream',
    );
    _dlEventSrc = src;
    src.onmessage = e => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'keepalive') return;
      if (msg.type === 'start') {
        log.textContent += `$ ${msg.cmd}\n`;
      } else if (msg.type === 'line') {
        log.textContent += msg.text + '\n';
        log.scrollTop = log.scrollHeight;
      } else if (msg.type === 'done') {
        log.textContent += msg.ok ? '\n✓ Done.\n' : `\n✗ Failed (exit ${msg.rc ?? msg.error}).\n`;
        try { src.close(); } catch(_){}
        if (_dlEventSrc === src) _dlEventSrc = null;
        // Delay before refreshing so user can read the output
        log.textContent += '\nRefreshing cache list in 5 seconds...\n';
        setTimeout(loadCacheList, 5000);
      }
    };
    src.onerror = () => {
      if (_dlEventSrc !== src) { try { src.close(); } catch(_){} return; }
      log.textContent += '\n[stream disconnected]\n';
      try { src.close(); } catch(_){}
      _dlEventSrc = null;
    };
  }).catch(e => { log.textContent = 'Error: ' + e; });
}

async function confirmPrune() {
  const ok = await _themedConfirm({
    title:        'Prune all detached revisions from the HF cache?',
    bodyHtml:     'This will free disk space used by old/unused model versions. Cannot be undone.',
    confirmLabel: 'Prune',
    cancelLabel:  'Cancel',
    danger:       true,
  });
  if (!ok) return;
  _streamCacheOp('/api/llm/cache/prune');
}

async function confirmCacheRm() {
  const repo = document.getElementById('cacheRmRepo').value.trim();
  if (!repo) { alert('Enter a repo ID to remove.'); return; }
  const ok = await _themedConfirm({
    title:        `Remove "${adminEsc(repo)}" from the HF cache?`,
    bodyHtml:     'This deletes the cached model files. Cannot be undone.',
    confirmLabel: 'Remove',
    cancelLabel:  'Cancel',
    danger:       true,
  });
  if (!ok) return;
  _streamCacheOp('/api/llm/cache/rm', {repo});
}

// ----- HF Trending Models -----

const _escHtml = _esc;   // legacy alias — see shared helpers block at top

function _showTrendingError(msg) {
  const container = document.getElementById('hfTrendingTable');
  const div = document.createElement('div');
  div.style.cssText = 'color:var(--crit);font-size:0.85em;padding:8px;';
  div.textContent = 'Error: ' + msg;
  container.replaceChildren(div);
}

async function loadHFTrending() {
  const container = document.getElementById('hfTrendingTable');
  container.innerHTML = '<div style="color:var(--fg-dim);font-size:0.85em;padding:8px;">Fetching from HuggingFace...</div>';
  try {
    const r = await fetch('/api/llm/hf-trending').then(r => r.json());
    if (!r.ok) {
      _showTrendingError(r.error);
      return;
    }
    const models = r.data || [];
    if (!models.length) {
      container.innerHTML = '<div style="color:var(--fg-dim);font-size:0.85em;padding:8px;">No results.</div>';
      return;
    }

    const rows = models.map(m => {
      const repo      = m.id || '?';
      const author    = m.author || '?';
      const created   = m.created_at   ? m.created_at.split('T')[0]   : '?';
      const downloads = m.downloads_all_time != null ? Number(m.downloads_all_time).toLocaleString() : '?';
      const modified  = m.last_modified ? m.last_modified.split('T')[0] : '?';
      const score     = m.trending_score != null ? Number(m.trending_score).toLocaleString() : '?';
      const repoUrl   = repo.split('/').map(encodeURIComponent).join('/');
      const repoJs    = JSON.stringify(repo);
      return `<tr>
        <td><a href="https://huggingface.co/${repoUrl}" target="_blank" style="color:var(--accent);text-decoration:none;">${_escHtml(repo)}</a></td>
        <td style="color:var(--fg);">${_escHtml(author)}</td>
        <td style="color:var(--fg-dim);">${_escHtml(created)}</td>
        <td class="trending-dl">${_escHtml(downloads)}</td>
        <td style="color:var(--fg-dim);">${_escHtml(modified)}</td>
        <td class="trending-dl">${_escHtml(score)}</td>
        <td><button class="btn btn-slate-muted-gradient" style="padding:3px 8px;font-size:0.75em;" onclick="prefillDownload(${_escHtml(repoJs)})">↓ Download</button></td>
      </tr>`;
    }).join('');

    container.innerHTML = `
      <table class="trending-table">
        <thead><tr>
          <th>Model</th><th>Author</th><th>Created</th><th>Downloads</th><th>Modified</th><th>Trending Score</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch(e) {
    _showTrendingError(e);
  }
}

function prefillDownload(repo) {
  switchTab('llm');
  switchSubTab('llm', 'llamacpp');
  // Ensure the Download section is expanded
  const sec = document.getElementById('secDownload');
  if (sec && sec.classList.contains('collapsed')) sec.classList.remove('collapsed');
  document.getElementById('dlRepo').value = repo;
  document.getElementById('dlInclude').value = '';
  setTimeout(() => {
    document.getElementById('dlRepo').scrollIntoView({behavior: 'smooth', block: 'center'});
    document.getElementById('dlRepo').focus();
  }, 80);
}
