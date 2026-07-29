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

  row.appendChild(_labeled('model', _text('model', entry.model, 'ap-input ap-model', 'model id')));

  const provider = _select('provider', PROVIDERS, entry.provider || 'llama');
  row.appendChild(_labeled('provider', provider));

  row.appendChild(_labeled('placement', _text('placement', entry.placement || 'auto',
    'ap-input ap-placement', 'auto or agent id')));

  row.appendChild(_labeled('failover', _select('failover', FAILOVER, entry.failover || 'semi')));

  row.appendChild(_labeled('priority', _num('priority', entry.priority ?? 100, 0)));
  row.appendChild(_labeled('min replicas', _num('min_replicas', entry.min_replicas ?? 1, 1)));
  row.appendChild(_labeled('max replicas', _num('max_replicas', entry.max_replicas ?? 1, 1)));

  const badge = el('span', 'status status--warn ap-vllm-badge', 'manual-apply only');
  badge.title = 'vLLM entries can never auto-execute — proposals always need a manual Apply.';
  badge.style.display = (entry.provider === 'vllm') ? '' : 'none';
  provider.addEventListener('change', () => {
    badge.style.display = (provider.value === 'vllm') ? '' : 'none';
  });
  row.appendChild(badge);

  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'adm-btn-icon ap-remove-btn';
  removeBtn.dataset.act = 'remove';
  removeBtn.title = 'Remove entry';
  removeBtn.textContent = '✕';
  removeBtn.addEventListener('click', () => row.remove());
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

  // Live proposals nest reason under action (server Action dataclass);
  // top-level p.reason wins when callers pass it directly (e.g. tests).
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

// Small status badge for an entry: counts proposals whose action.entry_key
// matches model/provider — "N pending" (warn) or "stable" (muted).
function statusChip(entry, placements) {
  const key = `${entry.model}/${entry.provider}`;
  const list = Array.isArray(placements) ? placements : [];
  const pending = list.filter(p => {
    const ek = p && (p.entry_key || (p.action && p.action.entry_key));
    return ek === key;
  }).length;
  if (pending > 0) return el('span', 'status status--warn', `${pending} pending`);
  return el('span', 'status status--muted', 'stable');
}

// ── DOM wiring (untested glue) ─────────────────────────────────────────

let _lastState = null;
let _lastProposals = [];
let _wired = false;

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
    wrap.appendChild(statusChip(entry, _lastProposals));
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
  const toggle = document.getElementById('apEnabledToggle');
  if (toggle && _lastState) toggle.checked = !!_lastState.enabled;
  _renderEntries();
  _renderProposals();
}

async function fetchState() {
  try {
    const r = await fetch('/api/autopilot');
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    _lastState = d.state || { enabled: false, entries: [], hosts: {} };
    _lastProposals = d.proposals || [];
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

async function planNow() {
  try {
    await fetch('/api/autopilot/tick', { method: 'POST' });
  } catch (_) { /* surfaced by the next fetchState() render */ }
  fetchState();
}

function addEntry() {
  const body = document.getElementById('apEntriesBody');
  if (!body) return;
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
}

// Called on sub-tab entry (mirrors adminAuditLoad/initReportCard).
function init() {
  _wire();
  fetchState();
}

// Called by the boot-time 10s setInterval; only fetches while the
// admin/autopilot sub-tab is actually visible.
function poll() {
  if (!_visible()) return;
  fetchState();
}

const AP = { entryRow, readEntries, proposalRow, statusChip, init, poll, save, addEntry,
             applyProposal, dismissProposal, planNow, fetchState };

if (typeof root !== 'undefined') root.AP = AP;
if (typeof module !== 'undefined' && module.exports) module.exports.AP = AP;

})(typeof window !== 'undefined' ? window : globalThis);
