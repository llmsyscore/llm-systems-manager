// Fleet Autopilot admin panel (#472) — entry editor, proposals queue, wiring.
// IIFE-scoped; exposes window.AP only. Pure testables + thin DOM glue.
(function (root) {

const PROVIDERS = [
  { v: 'llama', label: 'llama.cpp' },
  { v: 'vllm', label: 'vLLM' },
  { v: 'lms', label: 'LM Studio' },
];
const FAILOVER = [
  { v: 'semi', label: 'semi (propose)' },
  { v: 'auto', label: 'auto (execute)' },
];
const NUMERIC_FIELDS = ['priority', 'min_replicas', 'max_replicas'];

// createElement + textContent only — never innerHTML with server data.
function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

function _labeled(label, control) {
  const wrap = document.createElement('label');
  wrap.className = 'ap-field-wrap';
  wrap.appendChild(el('span', 'ap-lbl', label));
  wrap.appendChild(control);
  return wrap;
}

function _select(field, options, value, className) {
  const s = document.createElement('select');
  s.className = className || 'ap-select';
  s.dataset.field = field;
  options.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.v;
    opt.textContent = o.label;
    s.appendChild(opt);
  });
  s.value = value;
  return s;
}

function _text(field, value, className, placeholder) {
  const i = document.createElement('input');
  i.type = 'text';
  i.className = className || 'ap-input';
  i.dataset.field = field;
  i.value = value == null ? '' : value;
  if (placeholder) i.placeholder = placeholder;
  return i;
}

function _num(field, value, min) {
  const i = document.createElement('input');
  i.type = 'number';
  i.min = String(min);
  i.className = 'ap-input ap-num';
  i.dataset.field = field;
  i.value = String(value);
  return i;
}

// Injectable model/placement datalist source (#472) — wiring populates this
// from admin.js's provider-models endpoint + agent cache; tests inject
// fixtures directly (AP.setCatalog) so entryRow needs no network.
let _catalog = { models: {}, agents: [] };
function setCatalog(catalog) {
  _catalog = { models: (catalog && catalog.models) || {},
               agents: (catalog && catalog.agents) || [] };
}

let _rowSeq = 0;

// Model id -> hosts serving it; same option shape as admin.js's pin-editor
// datalist (value = id, label = "on: host1, host2").
function _fillModelOptions(dl, provider) {
  dl.replaceChildren();
  (_catalog.models[provider] || []).forEach(m => {
    const opt = document.createElement('option');
    opt.value = m.id;
    if (m.agents && m.agents.length) opt.textContent = 'on: ' + m.agents.join(', ');
    dl.appendChild(opt);
  });
}

// "auto" plus every approved agent advertising provider's capability;
// value = agent id (what readEntries needs back), label = hostname.
function _fillPlacementOptions(dl, provider) {
  dl.replaceChildren();
  const auto = document.createElement('option');
  auto.value = 'auto';
  auto.textContent = 'auto (pool logic)';
  dl.appendChild(auto);
  _catalog.agents
    .filter(a => a && a.status === 'approved' && (a.capabilities || {})[provider])
    .forEach(a => {
      const opt = document.createElement('option');
      opt.value = a.agent_id;
      opt.textContent = a.hostname || (a.agent_id || '').slice(0, 8);
      dl.appendChild(opt);
    });
}

// Builds one entry's editor row: inputs/selects tagged data-field, plus the
// vLLM "manual-apply only" badge (kept live via the provider select's change).
function entryRow(entry) {
  const row = document.createElement('div');
  row.className = 'ap-entry-row';
  // Preserves autoscale (not editable here) so an unrelated Save doesn't
  // silently reset a customized target_saturation/up_window_s/down_window_s.
  if (entry && entry.autoscale) {
    try { row.dataset.autoscale = JSON.stringify(entry.autoscale); } catch (_) { /* ignore */ }
  }

  const seq = ++_rowSeq;
  const modelsDl = document.createElement('datalist');
  modelsDl.id = `apModelsDl${seq}`;
  const placementDl = document.createElement('datalist');
  placementDl.id = `apPlacementDl${seq}`;

  const modelInput = _text('model', entry.model, 'ap-input ap-model', 'model id');
  modelInput.setAttribute('list', modelsDl.id);
  row.appendChild(_labeled('model', modelInput));
  row.appendChild(modelsDl);

  const provider = _select('provider', PROVIDERS, entry.provider || 'llama');
  row.appendChild(_labeled('provider', provider));

  const placementInput = _text('placement', entry.placement || 'auto',
    'ap-input ap-placement', 'auto or agent id');
  placementInput.setAttribute('list', placementDl.id);
  row.appendChild(_labeled('placement', placementInput));
  row.appendChild(placementDl);

  row.appendChild(_labeled('failover', _select('failover', FAILOVER, entry.failover || 'semi')));

  row.appendChild(_labeled('priority', _num('priority', entry.priority ?? 100, 0)));
  row.appendChild(_labeled('min replicas', _num('min_replicas', entry.min_replicas ?? 1, 1)));
  row.appendChild(_labeled('max replicas', _num('max_replicas', entry.max_replicas ?? 1, 1)));

  _fillModelOptions(modelsDl, provider.value);
  _fillPlacementOptions(placementDl, provider.value);

  const badge = el('span', 'status status--warn ap-vllm-badge', 'manual-apply only');
  badge.title = 'vLLM entries can never auto-execute — proposals always need a manual Apply.';
  badge.style.display = (entry.provider === 'vllm') ? '' : 'none';
  provider.addEventListener('change', () => {
    badge.style.display = (provider.value === 'vllm') ? '' : 'none';
    _fillModelOptions(modelsDl, provider.value);
    _fillPlacementOptions(placementDl, provider.value);
  });
  row.appendChild(badge);

  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'adm-btn-icon ap-remove-btn';
  removeBtn.dataset.act = 'remove';
  removeBtn.title = 'Remove entry';
  removeBtn.textContent = '✕';
  // _renderEntries() wraps each row in .ap-entry-wrap with its statusChip;
  // remove the wrap when present so the chip doesn't orphan, else the row.
  removeBtn.addEventListener('click', () => {
    _dirty = true;
    (row.closest('.ap-entry-wrap') || row).remove();
  });
  row.appendChild(removeBtn);

  return row;
}

// Reconstructs typed entries from every .ap-entry-row under container;
// numeric fields are parseInt'd, blank-model rows are dropped.
function readEntries(container) {
  const rows = container.querySelectorAll('.ap-entry-row');
  const out = [];
  rows.forEach(row => {
    const entry = {};
    row.querySelectorAll('[data-field]').forEach(input => {
      const f = input.dataset.field;
      entry[f] = NUMERIC_FIELDS.includes(f) ? parseInt(input.value, 10) : input.value;
    });
    if (row.dataset.autoscale) {
      try { entry.autoscale = JSON.parse(row.dataset.autoscale); } catch (_) { /* ignore */ }
    }
    if ((entry.model || '').trim() !== '') out.push(entry);
  });
  return out;
}

// One pending-proposal row: reason + kind + truncated agent id, with
// data-act=apply|dismiss buttons wired to the given callbacks.
function proposalRow(p, callbacks) {
  const cb = callbacks || {};
  const action = p.action || {};
  const row = document.createElement('div');
  row.className = 'ap-proposal-row';

  // Server always sets top-level p.reason; action.reason is a defensive
  // fallback only, for callers that don't (e.g. hand-built fixtures).
  row.appendChild(el('div', 'ap-proposal-reason', p.reason || action.reason || ''));

  const meta = document.createElement('div');
  meta.className = 'ap-proposal-meta';
  meta.appendChild(el('span', 'status status--info', action.kind || '—'));
  if (action.provider) meta.appendChild(el('span', 'ap-proposal-model', action.provider));
  if (action.model) meta.appendChild(el('span', 'ap-proposal-model', action.model));
  if (action.agent_id) meta.appendChild(el('span', 'ap-proposal-agent', String(action.agent_id).slice(0, 8)));
  row.appendChild(meta);

  const actions = document.createElement('div');
  actions.className = 'ap-proposal-actions';

  const applyBtn = document.createElement('button');
  applyBtn.type = 'button';
  applyBtn.className = 'adm-btn primary';
  applyBtn.dataset.act = 'apply';
  applyBtn.textContent = 'Apply';
  applyBtn.addEventListener('click', () => { if (typeof cb.onApply === 'function') cb.onApply(p.id); });
  actions.appendChild(applyBtn);

  const dismissBtn = document.createElement('button');
  dismissBtn.type = 'button';
  dismissBtn.className = 'adm-btn';
  dismissBtn.dataset.act = 'dismiss';
  dismissBtn.textContent = 'Dismiss';
  dismissBtn.addEventListener('click', () => { if (typeof cb.onDismiss === 'function') cb.onDismiss(p.id); });
  actions.appendChild(dismissBtn);

  row.appendChild(actions);
  return row;
}

// Status badge: "N pending" wins over placed/want + blocked reason;
// "stable" only when no entry_status was passed (#472 back-compat).
function statusChip(entry, placements, status) {
  const key = `${entry.model}/${entry.provider}`;
  const list = Array.isArray(placements) ? placements : [];
  const pending = list.filter(p => {
    const ek = p && (p.entry_key || (p.action && p.action.entry_key));
    return ek === key;
  }).length;
  if (pending > 0) return el('span', 'status status--warn', `${pending} pending`);
  if (status) {
    const placed = status.placed || 0;
    const want = status.want || 0;
    if (status.blocked) {
      return el('span', 'status status--warn', `${placed}/${want} — ${status.blocked}`);
    }
    const cls = placed >= want ? 'status status--ok' : 'status status--muted';
    return el('span', cls, `${placed}/${want} placed`);
  }
  return el('span', 'status status--muted', 'stable');
}

// ── DOM wiring (untested glue) ─────────────────────────────────────────

let _lastState = null;
let _lastProposals = [];
let _lastEntryStatus = {};
let _wired = false;
// True once the editor has unsaved user edits; blocks the 10s poll from
// clobbering them (#472). Cleared on init() and on a successful save().
let _dirty = false;

function _markDirty() { _dirty = true; }

function _visible() {
  return typeof _activeTab !== 'undefined' && _activeTab === 'admin' &&
    typeof _subTabState !== 'undefined' && _subTabState.admin === 'autopilot';
}

function _setStatus(msg, isErr) {
  const s = document.getElementById('apSaveStatus');
  if (!s) return;
  s.style.color = isErr ? 'var(--crit)' : 'var(--fg-muted)';
  s.textContent = msg;
}

function _renderEntries() {
  const body = document.getElementById('apEntriesBody');
  if (!body || !_lastState) return;
  body.replaceChildren();
  (_lastState.entries || []).forEach(entry => {
    const wrap = document.createElement('div');
    wrap.className = 'ap-entry-wrap';
    wrap.appendChild(entryRow(entry));
    const status = _lastEntryStatus[`${entry.model}/${entry.provider}`];
    wrap.appendChild(statusChip(entry, _lastProposals, status));
    body.appendChild(wrap);
  });
}

function _renderProposals() {
  const body = document.getElementById('apProposalsBody');
  if (!body) return;
  body.replaceChildren();
  if (!_lastProposals.length) {
    body.appendChild(el('div', 'adm-muted', 'No pending proposals.'));
    return;
  }
  _lastProposals.forEach(p => {
    body.appendChild(proposalRow(p, { onApply: applyProposal, onDismiss: dismissProposal }));
  });
}

function _render() {
  // Proposals are read-only, so they always reflect the latest poll.
  // The editor (toggle + entries) only takes server state while clean —
  // a dirty editor holds unsaved user edits until save() or a fresh init().
  _renderProposals();
  if (_dirty) return;
  const toggle = document.getElementById('apEnabledToggle');
  if (toggle && _lastState) toggle.checked = !!_lastState.enabled;
  _renderEntries();
}

// Providers with a /api/admin/<name>-models endpoint (same one admin.js's
// pin editor uses — pool_provider_names() server-side; 'lms' has none).
const _MODEL_LIST_PROVIDERS = ['llama', 'vllm'];

// Guards against worker-thread pileup on a slow/flapping fleet (#472):
// the backend handler is a sequential per-agent fan-out with 5s timeouts,
// so a 10s poll cadence can otherwise stack overlapping refreshes.
let _catalogInFlight = false;
let _catalogLastRefresh = 0;
const _CATALOG_MIN_INTERVAL_MS = 30000;

// Refreshes the model/placement datalist source. Skips if a refresh is
// already running, and skips a non-forced call inside the 30s floor —
// init() (tab entry) passes force=true so re-entry always gets fresh lists.
async function _refreshCatalog(force) {
  if (_catalogInFlight) return;
  if (!force && Date.now() - _catalogLastRefresh < _CATALOG_MIN_INTERVAL_MS) return;
  _catalogInFlight = true;
  try {
    const models = { ..._catalog.models };
    await Promise.all(_MODEL_LIST_PROVIDERS.map(async prov => {
      try {
        const r = await fetch(`/api/admin/${prov}-models`);
        if (!r.ok) return;
        const d = await r.json();
        models[prov] = d.models || [];
      } catch (_) { /* keep the previous list for this provider */ }
    }));
    // _adminAgentsCache is admin.js's agent cache (same script scope, kept
    // fresh by its own 20s refresh) — reused rather than a parallel fetch.
    const agents = typeof _adminAgentsCache !== 'undefined' ? _adminAgentsCache : _catalog.agents;
    setCatalog({ models, agents });
    _catalogLastRefresh = Date.now();
  } finally {
    _catalogInFlight = false;
  }
}

async function fetchState(forceCatalog) {
  // Runs alongside the state fetch (not serially); awaited below so a
  // refresh that actually ran finishes before entries render. A skipped
  // refresh resolves immediately, so this costs nothing when guarded off.
  const catalogP = _refreshCatalog(forceCatalog);
  try {
    const r = await fetch('/api/autopilot');
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    _lastState = d.state || { enabled: false, entries: [], hosts: {} };
    _lastProposals = d.proposals || [];
    _lastEntryStatus = d.entry_status || {};
    await catalogP;
    _render();
  } catch (e) {
    _setStatus('load failed: ' + e.message, true);
  }
}

async function save() {
  const body = document.getElementById('apEntriesBody');
  const toggle = document.getElementById('apEnabledToggle');
  if (!body) return;
  const state = {
    enabled: toggle ? toggle.checked : false,
    entries: readEntries(body),
    hosts: (_lastState && _lastState.hosts) || {},
  };
  _setStatus('saving…');
  try {
    const r = await fetch('/api/autopilot', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(state),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { _setStatus('✗ ' + (d.error || ('HTTP ' + r.status)), true); return; }
    _lastState = d.state || state;
    _dirty = false;
    _setStatus('✓ saved');
    _render();
  } catch (e) {
    _setStatus('✗ ' + e.message, true);
  }
}

async function applyProposal(id) {
  try {
    await fetch(`/api/autopilot/proposals/${encodeURIComponent(id)}/apply`, { method: 'POST' });
  } catch (_) { /* surfaced by the next fetchState() render */ }
  fetchState();
}

async function dismissProposal(id) {
  try {
    await fetch(`/api/autopilot/proposals/${encodeURIComponent(id)}/dismiss`, { method: 'POST' });
  } catch (_) { /* surfaced by the next fetchState() render */ }
  fetchState();
}

// Surfaces the tick result on apSaveStatus; zero actions only reads as
// "satisfied" when nothing is blocked, else reports the blocked count (#472).
async function planNow() {
  try {
    const r = await fetch('/api/autopilot/tick', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      _setStatus('✗ plan failed: ' + (d.error || ('HTTP ' + r.status)), true);
    } else {
      const actions = d.actions || [];
      const proposals = d.proposals || [];
      if (actions.length) {
        _setStatus(`plan: ${actions.length} action(s), ${proposals.length} proposal(s) pending`);
      } else {
        const blocked = Object.values(d.entry_status || {}).filter(s => s && s.blocked).length;
        _setStatus(blocked
          ? `plan: no plannable actions — ${blocked} entr${blocked === 1 ? 'y' : 'ies'} blocked (see status chips)`
          : 'plan: no actions needed — desired state satisfied');
      }
    }
  } catch (e) {
    _setStatus('✗ plan failed: ' + e.message, true);
  }
  fetchState();
}

function addEntry() {
  const body = document.getElementById('apEntriesBody');
  if (!body) return;
  _dirty = true;
  const wrap = document.createElement('div');
  wrap.className = 'ap-entry-wrap';
  wrap.appendChild(entryRow({ model: '', provider: 'llama', placement: 'auto',
    failover: 'semi', priority: 100, min_replicas: 1, max_replicas: 1 }));
  body.appendChild(wrap);
}

function _wire() {
  if (_wired) return;
  _wired = true;
  const save_ = document.getElementById('apSaveBtn');
  if (save_) save_.addEventListener('click', save);
  const add_ = document.getElementById('apAddEntryBtn');
  if (add_) add_.addEventListener('click', addEntry);
  const plan_ = document.getElementById('apPlanNowBtn');
  if (plan_) plan_.addEventListener('click', planNow);
  const refresh_ = document.getElementById('apProposalsRefreshBtn');
  if (refresh_) refresh_.addEventListener('click', fetchState);
  // Delegated so it covers rows added/removed after wiring — typing
  // (input) and select/checkbox changes (change) both mark dirty.
  const entries_ = document.getElementById('apEntriesBody');
  if (entries_) {
    entries_.addEventListener('input', _markDirty);
    entries_.addEventListener('change', _markDirty);
  }
  const toggle_ = document.getElementById('apEnabledToggle');
  if (toggle_) toggle_.addEventListener('change', _markDirty);
}

// Called on sub-tab entry (mirrors adminAuditLoad/initReportCard). Forces
// the catalog refresh past the 30s floor so re-entry always sees fresh
// model/agent lists; returns the promise so callers/tests can await it.
function init() {
  _wire();
  _dirty = false;
  return fetchState(true);
}

// Called by the boot-time 10s setInterval; only fetches while the
// admin/autopilot sub-tab is actually visible.
function poll() {
  if (!_visible()) return;
  fetchState();
}

const AP = { entryRow, readEntries, proposalRow, statusChip, setCatalog, init, poll, save,
             addEntry, applyProposal, dismissProposal, planNow, fetchState };

if (typeof root !== 'undefined') root.AP = AP;
if (typeof module !== 'undefined' && module.exports) module.exports.AP = AP;

})(typeof window !== 'undefined' ? window : globalThis);
