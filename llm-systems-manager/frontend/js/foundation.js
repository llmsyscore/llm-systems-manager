// ── Multi-agent picker state (PR3) ──────────────────────────────────
// _selectedAgents[provider] = agent_id of the dashboard picker's selection,
// or null. _withAgentParam() appends ?agent=<id> to same-origin provider API
// URLs so the manager dispatcher routes to the picked host. No-op when no
// selection exists (single-agent installs) → byte-identical behavior.
window._agentsByProvider = window._agentsByProvider || { llama: [], lms: [] };
window._selectedAgents   = window._selectedAgents   || { llama: null, lms: null };

function _selectedAgent(provider) {
  return (window._selectedAgents && window._selectedAgents[provider]) || null;
}

// In-flight guard key scoped to the selected agent. A picker switch must not
// be swallowed by an in-flight poll for the PREVIOUS agent (different key →
// the new fetch proceeds immediately instead of waiting a full interval).
function _agentClaimKey(base, provider) {
  return base + ':' + (_selectedAgent(provider) || '');
}

const _AGENT_PATH_PROVIDER = [
  [/^\/api\/lmstudio\//, 'lms'],
  [/^\/api\/lms\//,      'lms'],     // incl. /api/lms/terminal/create — must precede /api/terminal/
  [/^\/api\/llm\//,      'llama'],
  [/^\/api\/llama/,      'llama'],
  [/^\/api\/benchmark\//,'llama'],   // bench run/stream/cancel/perf live on the llama host
  [/^\/api\/terminal\//, 'llama'],   // llama PTY create; sid-routed IO calls ignore ?agent= server-side
  [/^\/api\/metrics$/,   'llama'],   // Dashboard llama host+throughput sample
  [/^\/api\/alert$/,     'llama'],   // llama host alert-state booleans
];
function _providerForApiPath(path) {
  for (const [re, prov] of _AGENT_PATH_PROVIDER) if (re.test(path)) return prov;
  return null;
}
window._withAgentParam = function (url) {
  try {
    if (typeof url !== 'string') return url;
    const path = url.split('?')[0];
    if (!path.startsWith('/api/')) return url;       // leave absolute agent URLs alone
    const provider = _providerForApiPath(path);
    if (!provider) return url;
    if (/[?&]agent=/.test(url)) return url;           // caller already pinned an agent
    const aid = _selectedAgent(provider);
    if (!aid) return url;
    return url + (url.includes('?') ? '&' : '?') + 'agent=' + encodeURIComponent(aid);
  } catch (_) { return url; }
};

// When the dashboard login session expires (or is required and absent), the
// manager answers API/proxy calls with 401 {auth_required:true}. Bounce the
// browser to the login page so the operator can re-authenticate.
// Also injects the picker's ?agent= selection into provider API calls — one
// choke point covers fetch + _fetchT (which delegates here).
(function () {
  const _origFetch = window.fetch;
  window.fetch = function (input, ...rest) {
    try {
      if (typeof input === 'string' && window._withAgentParam) {
        input = window._withAgentParam(input);
      }
    } catch (_) {}
    return _origFetch.call(this, input, ...rest).then(resp => {
      if (resp.status === 401) {
        resp.clone().json().then(j => {
          if (j && j.auth_required && !location.pathname.startsWith('/login')) {
            location.href = '/login';
          }
        }).catch(() => {});
      }
      return resp;
    });
  };
  // EventSource carries no custom headers, so the ?agent= param is the only
  // way to route an SSE stream to the picked agent. Same transform.
  const _OrigES = window.EventSource;
  if (_OrigES) {
    window.EventSource = function (url, cfg) {
      try {
        if (typeof url === 'string' && window._withAgentParam) {
          url = window._withAgentParam(url);
        }
      } catch (_) {}
      return cfg === undefined ? new _OrigES(url) : new _OrigES(url, cfg);
    };
    window.EventSource.prototype = _OrigES.prototype;
    window.EventSource.CONNECTING = _OrigES.CONNECTING;
    window.EventSource.OPEN = _OrigES.OPEN;
    window.EventSource.CLOSED = _OrigES.CLOSED;
  }
})();

const MAX_POINTS = 3600;

// ---------------------------------------------------------------------------
// Shared helpers: HTML escaping, fetch-with-timeout, in-flight guards
// ---------------------------------------------------------------------------
function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]
  ));
}

// Abort-controller fetch so hung backends don't leave promises pending forever.
// Usage: await _fetchT('/api/foo', {method:'POST', body:...}, 15000)
function _fetchT(url, opts = {}, timeoutMs = 15000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  return fetch(url, { ...opts, signal: ctrl.signal }).finally(() => clearTimeout(t));
}

// Simple once-at-a-time guard for polling functions.
// Usage: if (!_claim('metrics')) return; try {...} finally { _release('metrics'); }
const _inflight = new Set();
function _claim(key)   { if (_inflight.has(key)) return false; _inflight.add(key); return true; }
function _release(key) { _inflight.delete(key); }

// Per-action debounce for user-initiated ops (load/unload/reload).
const _actionInflight = new Set();
function _actionClaim(key)   { if (_actionInflight.has(key)) return false; _actionInflight.add(key); return true; }
function _actionRelease(key) { _actionInflight.delete(key); }

let _activeTab = 'overall';   // tracks which top-level tab is visible

const CARD_LABELS_OVERALL = {
  'ov-llama-fleet':  'llama.cpp Fleet',
  'ov-llama-gpu':    'llama.cpp Fleet — GPU',
  'ov-llama-active': 'llama.cpp Fleet — Active Models',
  'ov-llama-chart':  'llama.cpp Fleet — Throughput',
  'ov-lms-fleet':    'LM Studio Fleet',
  'ov-fleet':        'Fleet Overview',
};
const CARD_LABELS_LMS = {
  'lms-models':  'LM Studio Models',
  'lms-active':  'Active Model',
  'lms-cpu':     'LM Studio CPU',
  'lms-ram':     'LM Studio RAM',
  'lms-network': 'LM Studio Network',
  'lms-disk':    'LM Studio Disk',
  'lms-power':   'LM Studio powermetrics',
};
const CARD_LABELS_MANAGER = {
  'services':         'Services',
  'influxdb':         'InfluxDB',
  'mgr-agents':       'Agents',
  'mgr-ram':          'CPU, RAM & Swap',
  'mgr-disk':         'Disk Usage & IO',
  'mgr-network':      'Network',
  'mgr-processes':    'Processes',
  'mgr-perf-summary': 'Self-Monitor Summary',
  'mgr-perf':         'Manager Perf',
  'ae-perf':          'Alarm Engine + Influx Perf',
};
const CARD_LABELS = {
  'llama-server':    'Llama server',
  'llama-throughput':'Llama throughput',
  'gpu':             'GPU',
  'cpu-overall':     'CPU',
  'ram':             'RAM',
  'network':         'Network',
  'disk-usage':      'Disk usage',
  'disk-io':         'Disk IO',
  'ups':             'UPS',
  'aio':             'AIO',
  'psu':             'Corsair PSU',
  'smart-device':    'NZXT Smart Device',
};

// ---------------------------------------------------------------------------
// Layout persistence
// ---------------------------------------------------------------------------
let layout = { order: [], hidden: [] };

async function loadLayout() {
  try {
    layout = await fetch('/api/layout').then(r => r.json());
    if (!layout.order)           layout.order           = [];
    if (!layout.hidden)          layout.hidden          = [];
    if (!layout.hiddenOverall)   layout.hiddenOverall   = [];
    if (!layout.lmsHidden)       layout.lmsHidden       = [];
    if (!layout.managerHidden)   layout.managerHidden   = [];
    if (!layout.managerOrder)    layout.managerOrder    = [];
    if (!layout.overallBorrowed) layout.overallBorrowed = [];
    if (!layout.overallOrder)    layout.overallOrder    = [];
    if (!layout.cardSizes || typeof layout.cardSizes !== 'object') layout.cardSizes = {};
    _migrateLegacyCardIds(layout);
    if (layout.lmsOrder)     applyLmsLayout(layout.lmsOrder);
    if (layout.managerOrder) applyManagerLayout(layout.managerOrder);
  } catch(e) {}
  applyTheme(layout && layout.theme, false);
  applyLayout();
  applyAllGridCols();
}

// kraken → aio: existing saved layouts written before the rename still carry
// the old card id. Rewrite in place so applyLayout() finds the card and the
// next saveLayout() writes the new id.
const _LEGACY_CARD_RENAMES = {
  'kraken': 'aio',
  // PR4: Overall-tab single-host cards → fleet-aggregate cards. ov-llama-chart
  // keeps its id (relabeled only). Saved overallOrder/hiddenOverall/
  // overallBorrowed/cardSizes entries rewrite in place so layouts survive.
  'ov-llama':     'ov-llama-fleet',
  'ov-gpu':       'ov-llama-gpu',
  'ov-llcpp-sys': 'ov-llama-active',
  'ov-lms':       'ov-lms-fleet',
  'ov-lms-sys':   'ov-fleet',
};
function _migrateLegacyCardIds(lay) {
  const swap = arr => Array.isArray(arr) && arr.forEach((id, i) => {
    if (_LEGACY_CARD_RENAMES[id]) arr[i] = _LEGACY_CARD_RENAMES[id];
  });
  swap(lay.order); swap(lay.hidden);
  swap(lay.lmsOrder); swap(lay.lmsHidden);
  swap(lay.managerOrder); swap(lay.managerHidden);
  swap(lay.overallOrder); swap(lay.overallBorrowed); swap(lay.hiddenOverall);
  if (lay.cardSizes && typeof lay.cardSizes === 'object') {
    for (const oldId in _LEGACY_CARD_RENAMES) {
      if (oldId in lay.cardSizes) {
        const newId = _LEGACY_CARD_RENAMES[oldId];
        if (!(newId in lay.cardSizes)) lay.cardSizes[newId] = lay.cardSizes[oldId];
        delete lay.cardSizes[oldId];
      }
    }
  }
}

// Live hardware-name registry. _setCardTitle() in charts.js updates this on
// every fetchMetrics() tick so renderSettingsPanel() can show the same
// agent-reported name the card title shows. Keyed by data-card id.
window._hardwareNames = window._hardwareNames || {};
function _cardLabel(id, map) {
  return (window._hardwareNames && window._hardwareNames[id]) || (map && map[id]) || id;
}

// ── Multi-agent picker (PR3) ────────────────────────────────────────
// Picker chip-rows live in 4 sub-panels (dash-llamacpp/dash-lmstudio/
// llm-llamacpp/llm-lmstudio). They auto-hide when a provider has ≤1 agent so
// single-host installs see no UI change. Selection persists in layout and is
// restored on load. Maps provider → container element id.
const _AGENT_PICKER_CONTAINERS = {
  llama: ['agentPickerDashLlama', 'agentPickerCtrlLlama'],
  lms:   ['agentPickerDashLms', 'agentPickerCtrlLms'],
};

async function _loadAgentsByProvider() {
  try {
    const data = await fetch('/api/agents/list-by-provider').then(r => r.json());
    window._agentsByProvider = { llama: data.llama || [], lms: data.lms || [] };
    // Restore persisted selection; else fall back to the provider default.
    ['llama', 'lms'].forEach(prov => {
      const list = window._agentsByProvider[prov] || [];
      const saved = (layout && layout._selectedAgents && layout._selectedAgents[prov]) || null;
      const savedValid = saved && list.some(a => a.agent_id === saved);
      if (savedValid) {
        window._selectedAgents[prov] = saved;
      } else if (list.length > 1) {
        // Only pin a selection when there's an actual choice. A single-agent
        // provider keeps selection null so _withAgentParam stays a no-op →
        // byte-identical to pre-PR3 (no ?agent= appended anywhere).
        const def = list.find(a => a.is_default) || list[0];
        window._selectedAgents[prov] = def ? def.agent_id : null;
      } else {
        window._selectedAgents[prov] = null;
      }
      // Correct a stale/deleted persisted id in-memory so it doesn't linger
      // in layout.json (re-persisted on the next real save).
      if (!savedValid && layout && layout._selectedAgents) {
        layout._selectedAgents[prov] = window._selectedAgents[prov];
      }
    });
    _renderAgentPickers();
  } catch (_) {}
}

function _renderAgentPickers() {
  ['llama', 'lms'].forEach(prov => {
    const list = window._agentsByProvider[prov] || [];
    const sel = _selectedAgent(prov);
    (_AGENT_PICKER_CONTAINERS[prov] || []).forEach(cid => {
      const el = document.getElementById(cid);
      if (!el) return;
      // Auto-hide when only one agent of this type exists.
      if (list.length <= 1) { el.style.display = 'none'; el.innerHTML = ''; return; }
      el.style.display = '';
      el.innerHTML = list.map(a => {
        const active = a.agent_id === sel ? ' active' : '';
        const off = a.online ? '' : ' offline';
        const name = _esc(a.hostname || a.agent_id.slice(0, 8));
        const dot = a.is_default ? ' ★' : '';
        return `<button type="button" class="agent-chip${active}${off}" `
             + `data-provider="${prov}" data-agent="${_esc(a.agent_id)}" `
             + `title="${_esc(a.agent_id)}${a.online ? '' : ' (offline)'}">`
             + `${name}${dot}</button>`;
      }).join('');
    });
  });
}

function _selectAgent(provider, agentId) {
  if (!provider) return;
  if (_selectedAgent(provider) === agentId) return;
  window._selectedAgents[provider] = agentId;
  // Persist into layout — saveLayout() self-coalesces concurrent calls, so a
  // rapid chip-switch makes one POST, not one per click.
  if (typeof layout === 'object' && layout) {
    layout._selectedAgents = { ...(layout._selectedAgents || {}), [provider]: agentId };
    try { saveLayout(); } catch (_) {}
  }
  _renderAgentPickers();
  // Reset the editor/download/cache/build panels before loading the new agent.
  if (typeof resetLLMControlPanels === 'function') resetLLMControlPanels();
  // Clear the disk-usage bar list (guarded render keeps its last value when a
  // sample lacks disk, so it'd otherwise show the previous agent's mounts) (#121).
  const _clearBars = (id) => { const el = document.getElementById(id); if (el) el.innerHTML = ''; };
  // Re-pull everything for the newly-selected agent.
  if (provider === 'llama') {
    _clearBars('diskList');
    // Charts are per-agent: loadHistory() clears + backfills this agent's
    // history (runs synchronously up to its fetch, so the old agent's lines
    // clear at once); resume live points only after the backfill so they
    // don't interleave out of order (#121).
    if (typeof loadHistory === 'function') {
      loadHistory().finally(() => { if (typeof fetchMetrics === 'function') fetchMetrics(); });
    } else if (typeof fetchMetrics === 'function') {
      fetchMetrics();
    }
    if (typeof pollServerState === 'function')       pollServerState();
    if (typeof _startLlamaStateStream === 'function') _startLlamaStateStream();
    if (typeof refreshLLMTab === 'function')         refreshLLMTab();
    // Re-query HF trending against the newly-selected agent (runs the hf CLI
    // on that agent's host) only on the LLM Control tab — otherwise an
    // agent-switch on the llama dashboard runs the remote CLI needlessly.
    if (typeof loadHFTrending === 'function'
        && typeof _activeTab !== 'undefined' && _activeTab === 'llm') loadHFTrending();
    // Reopen the log stream against the new agent (the old one is pinned to
    // the previous host). Only when the panel is open AND the user is actually
    // on the LLM Control tab — otherwise an agent-switch on the llama dashboard
    // would open /llama/log/stream needlessly (the panel state persists across
    // tabs), piling up proxied streams.
    if (typeof _logPanelOpen !== 'undefined' && _logPanelOpen
        && typeof _activeTab !== 'undefined' && _activeTab === 'llm'
        && typeof _subTabState !== 'undefined' && _subTabState.llm === 'llamacpp'
        && typeof restartLogStream === 'function') restartLogStream();
    // Close the terminal on switch — never auto-open it on the new agent.
    // It only (re)opens on the selected agent when the user clicks the
    // terminal button.
    if (typeof closeTerminal === 'function'
        && ((typeof _termSid !== 'undefined' && _termSid)
            || (typeof _termOpen !== 'undefined' && _termOpen))) closeTerminal();
  } else if (provider === 'lms') {
    _clearBars('lmsDiskList');
    // loadLmsHistory reads the picker selection (_selectedAgent('lms')) and
    // backfills that agent's host server-side via /api/history?agent= — no
    // hostname needed here (#140). Resume live only after, like llama (#121).
    if (typeof loadLmsHistory === 'function') {
      loadLmsHistory().finally(() => { if (typeof fetchLMStudioMetrics === 'function') fetchLMStudioMetrics(); });
    } else if (typeof fetchLMStudioMetrics === 'function') {
      fetchLMStudioMetrics();
    }
    if (typeof _lmsLogOpen !== 'undefined' && _lmsLogOpen
        && typeof startLmsLogRefresh === 'function') startLmsLogRefresh();
    if (typeof closeLmsTerminal === 'function') {
      const _lp = document.getElementById('lmsTerminalPanel');
      if ((typeof _lmsTermSid !== 'undefined' && _lmsTermSid)
          || (_lp && _lp.style.display !== 'none')) closeLmsTerminal();
    }
  }
}

// Delegated chip-click handler — chips are (re)rendered dynamically.
document.addEventListener('click', (e) => {
  const chip = e.target.closest && e.target.closest('.agent-chip');
  if (!chip) return;
  _selectAgent(chip.dataset.provider, chip.dataset.agent);
});

// Lightweight self-contained toast for picker/routing notices (e.g. a model
// pin overriding the picker selection). Independent of the alarm-engine toast
// IIFE in events-toasts.js. Auto-dismisses after 6s.
function _pickerToast(message) {
  try {
    let host = document.getElementById('_pickerToastHost');
    if (!host) {
      host = document.createElement('div');
      host.id = '_pickerToastHost';
      host.style.cssText = 'position:fixed;bottom:18px;right:18px;z-index:9999;'
        + 'display:flex;flex-direction:column;gap:8px;max-width:360px;';
      document.body.appendChild(host);
    }
    const t = document.createElement('div');
    t.className = 'picker-toast';
    t.textContent = message;
    host.appendChild(t);
    const kill = () => { try { t.remove(); } catch (_) {} };
    t.addEventListener('click', kill);
    setTimeout(kill, 6000);
  } catch (_) {}
}

// Read a proxied response's X-Routing-Override header; toast when a model pin
// overrode the picker selection so the operator isn't surprised the action
// landed on a different host than the chip they had selected.
function _notePinOverride(resp, modelId) {
  try {
    if (resp && resp.headers && resp.headers.get('X-Routing-Override') === 'pin') {
      const host = resp.headers.get('X-Proxied-To') || 'its pinned host';
      const name = (typeof shortName === 'function') ? shortName(modelId) : modelId;
      _pickerToast(`"${name}" is pinned — routed to ${host} instead of the selected agent.`);
    }
  } catch (_) {}
}

const VALID_THEMES = ['dark', 'medium', 'light', 'modern', 'classic', 'slate', 'enterprise'];
function applyTheme(name, save) {
  if (!VALID_THEMES.includes(name)) name = 'dark';
  document.documentElement.setAttribute('data-theme', name);
  const sel = document.getElementById('themeSelect');
  if (sel && sel.value !== name) sel.value = name;
  _retintCharts();
  _propagateThemeToAlarmEngine(name);
  if (save) {
    if (typeof layout !== 'object' || !layout) layout = {};
    layout.theme = name;
    saveLayout();
  }
}

// Lazy-load the alarm engine iframe on first Events-tab visit. The iframe
// HTML carries data-src="/alarm/" with NO src — until this runs, the AE
// dashboard doesn't boot and contributes zero traffic. Subsequent calls
// are no-ops so tab toggling doesn't reload the SPA.
function _ensureAlarmIframeLoaded() {
  const iframe = document.getElementById('alarmEngineIframe');
  if (!iframe) return;
  const have = iframe.getAttribute('src');
  if (have) return;
  const target = iframe.getAttribute('data-src') || '/alarm/';
  try {
    const u = new URL(target, window.location.origin);
    const theme = (document.documentElement.dataset.theme || '').trim();
    if (theme && !u.searchParams.get('theme')) u.searchParams.set('theme', theme);
    iframe.setAttribute('src', u.pathname + (u.search ? u.search : ''));
  } catch (_) {
    iframe.setAttribute('src', target);
  }
}

// Sync the embedded alarm engine SPA with the parent's theme.
//   • On first apply (iframe still has bare src="/alarm/") rewrite the src
//     to include ?theme=<name>. The SPA reads the query param at load.
//   • On subsequent changes (iframe already loaded) postMessage so the
//     SPA can update without a full reload.
//   • If the iframe hasn't been loaded yet (no src — Events tab never
//     opened), don't force a load. _ensureAlarmIframeLoaded() will pick
//     up the current theme when the user first visits.
function _propagateThemeToAlarmEngine(name) {
  const iframe = document.getElementById('alarmEngineIframe');
  if (!iframe) return;
  const have = iframe.getAttribute('src');
  if (!have) return;  // lazy-loaded; theme applied on first visit
  try {
    const u = new URL(have, window.location.origin);
    const had = u.searchParams.get('theme');
    if (had !== name && !had) {
      u.searchParams.set('theme', name);
      iframe.setAttribute('src', u.pathname + (u.search ? u.search : ''));
    }
  } catch (_) { /* ignore — falls back to postMessage */ }
  try {
    iframe.contentWindow?.postMessage({ type: 'theme', name }, window.location.origin);
  } catch (_) {}
}

// Resolve a CSS theme token (e.g. '--accent') to its computed string — for
// canvas contexts (Chart.js) that can't consume var() directly.
function cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback || '';
  } catch (_) { return fallback || ''; }
}
function _themeChartDefaults() {
  if (!window.Chart) return;
  const muted = cssVar('--fg-muted'), grid = cssVar('--border-soft');
  if (muted) Chart.defaults.color = muted;
  if (grid)  Chart.defaults.borderColor = grid;
  const plugins = Chart.defaults.plugins = Chart.defaults.plugins || {};
  const tt = plugins.tooltip = plugins.tooltip || {};
  tt.backgroundColor = cssVar('--bg-card');
  tt.titleColor = cssVar('--fg');
  tt.bodyColor = cssVar('--fg-muted');
  tt.borderColor = cssVar('--border');
  tt.borderWidth = 1;
}
// Re-tint every live chart's structural chrome (ticks/grid/tooltip) from the
// active theme without destroying it — preserves buffered history points.
// Data-series colors are an intentional categorical palette, left as-is.
function _retintCharts() {
  _themeChartDefaults();
  if (!(window.Chart && Chart.instances)) return;
  const tick = cssVar('--fg-muted'), grid = cssVar('--border-soft');
  const ttBg = cssVar('--bg-card'), ttTitle = cssVar('--fg'), ttBody = cssVar('--fg-muted'), ttBorder = cssVar('--border');
  Object.values(Chart.instances).forEach(c => {
    try {
      const sc = c.options && c.options.scales;
      if (sc) Object.keys(sc).forEach(k => {
        const ax = sc[k]; if (!ax) return;
        if (ax.ticks) ax.ticks.color = tick;
        if (ax.grid)  ax.grid.color  = grid;
      });
      const tt = c.options && c.options.plugins && c.options.plugins.tooltip;
      if (tt) { tt.backgroundColor = ttBg; tt.titleColor = ttTitle; tt.bodyColor = ttBody; tt.borderColor = ttBorder; tt.borderWidth = 1; }
      c.update('none');
    } catch (_) {}
  });
}
// Apply chart defaults once at load before any Chart() is instantiated below.
_themeChartDefaults();

function applyLayout() {
  const grid = document.getElementById('cardGrid');
  const cards = [...grid.querySelectorAll('.card')];

  // Apply order
  if (layout.order && layout.order.length) {
    const ordered = [];
    layout.order.forEach(id => {
      const c = cards.find(c => c.dataset.card === id);
      if (c) ordered.push(c);
    });
    cards.forEach(c => { if (!ordered.includes(c)) ordered.push(c); });
    ordered.forEach(c => grid.appendChild(c));
  }

  // Apply visibility — Dashboard/llama.cpp cards
  cards.forEach(c => {
    c.style.display = layout.hidden.includes(c.dataset.card) ? 'none' : '';
  });

  // Apply visibility — LLM Overall cards
  const hiddenOv = layout.hiddenOverall || [];
  document.querySelectorAll('#overallGrid .card').forEach(c => {
    c.style.display = hiddenOv.includes(c.dataset.card) ? 'none' : '';
  });

  // Apply visibility — LMS dashboard cards
  const hiddenLms = layout.lmsHidden || [];
  document.querySelectorAll('#lmsCardGrid .card').forEach(c => {
    c.style.display = hiddenLms.includes(c.dataset.card) ? 'none' : '';
  });

  // Apply visibility — Manager dashboard cards
  const hiddenMgr = layout.managerHidden || [];
  document.querySelectorAll('#managerCardGrid .card').forEach(c => {
    c.style.display = hiddenMgr.includes(c.dataset.card) ? 'none' : '';
  });

  // Recreate borrowed-card mirror shells in overallGrid
  const overallGrid = document.getElementById('overallGrid');
  if (overallGrid) {
    (layout.overallBorrowed || []).forEach(cardId => {
      if (document.querySelector(`#overallGrid [data-card="ov-borrow-${cardId}"]`)) return;
      const shell = document.createElement('div');
      shell.className = 'card';
      shell.dataset.card = 'ov-borrow-' + cardId;
      shell.style.minHeight = '120px';
      overallGrid.appendChild(shell);
    });
    // Apply saved overallGrid order if present
    if (layout.overallOrder && layout.overallOrder.length) {
      const all = [...overallGrid.querySelectorAll('.card')];
      const ordered = [];
      layout.overallOrder.forEach(id => {
        const c = all.find(c => c.dataset.card === id);
        if (c) ordered.push(c);
      });
      all.forEach(c => { if (!ordered.includes(c)) ordered.push(c); });
      ordered.forEach(c => overallGrid.appendChild(c));
    }
    // Sync content now that shells exist
    if (typeof syncBorrowedCards === 'function') syncBorrowedCards();
  }
}

// Serialize layout POSTs so rapid drag-drops can't race. Multiple calls while
// a POST is in flight collapse into a single trailing save that captures the
// final DOM order.
let _layoutInFlight = null;
let _layoutPending  = false;
async function saveLayout() {
  if (_layoutInFlight) { _layoutPending = true; return _layoutInFlight; }
  _layoutInFlight = (async () => {
    try {
      do {
        _layoutPending = false;
        const grid = document.getElementById('cardGrid');
        layout.order = [...grid.querySelectorAll('.card')].map(c => c.dataset.card);
        try {
          await fetch('/api/layout', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(layout),
          });
        } catch(_) {}
      } while (_layoutPending);
    } finally {
      _layoutInFlight = null;
    }
  })();
  return _layoutInFlight;
}

// ---------------------------------------------------------------------------
// Borrowed cards — mirror any Dashboard card into the LLM Overall tab.
// Home card in #cardGrid or #lmsCardGrid keeps getting updated by the existing
// fetchMetrics / fetchLMStudioMetrics functions. After each update cycle,
// syncBorrowedCards() copies innerHTML from each home card to its mirror shell
// in #overallGrid, then uses canvas.drawImage to mirror any Chart.js canvases.
// ---------------------------------------------------------------------------
function syncBorrowedCards() {
  const borrowed = (layout && layout.overallBorrowed) || [];
  if (!borrowed.length) return;
  const pairs = [];
  borrowed.forEach(id => {
    const home   = document.querySelector(`#cardGrid [data-card="${id}"], #lmsCardGrid [data-card="${id}"], #managerCardGrid [data-card="${id}"]`);
    const mirror = document.querySelector(`#overallGrid [data-card="ov-borrow-${id}"]`);
    if (home && mirror) pairs.push([home, mirror]);
  });
  if (!pairs.length) return;

  // Copy rendered HTML (text, stats, bars). This also resets canvases, so we
  // re-paint them from the home canvases on the next animation frame.
  pairs.forEach(([home, mirror]) => {
    mirror.innerHTML = home.innerHTML;
    // Override any Chart.js inline pixel widths so canvases fill the mirror
    // card's container width rather than the home card's (different column count).
    mirror.querySelectorAll('canvas').forEach(c => {
      c.style.width = '100%';
      c.style.height = '';
      if (c.width && c.height) c.style.aspectRatio = c.width + ' / ' + c.height;
    });
    // innerHTML copied the home card's size button (an unwired clone) —
    // drop it and add a fresh one bound to this mirror so the user can
    // resize borrowed cards independently of their home.
    mirror.querySelectorAll(':scope > .card-size-btn').forEach(b => b.remove());
    _ensureSizeBtn(mirror);
  });

  requestAnimationFrame(() => {
    pairs.forEach(([home, mirror]) => {
      const homeCanvases   = home.querySelectorAll('canvas');
      const mirrorCanvases = mirror.querySelectorAll('canvas');
      homeCanvases.forEach((src, i) => {
        const dst = mirrorCanvases[i];
        if (!dst) return;
        if (src.width && src.height) {
          dst.width  = src.width;
          dst.height = src.height;
          try { dst.getContext('2d')?.drawImage(src, 0, 0); } catch(_) {}
          // Re-apply after pixel buffer assignment; setting .width clears inline style.
          dst.style.width = '100%';
          dst.style.height = '';
          dst.style.aspectRatio = src.width + ' / ' + src.height;
        }
        // Borrowed-card hover stopgap: the mirror is a bitmap copy with no
        // Chart.js instance, so native tooltips don't fire. Forward the
        // hover position to the home chart, read its data at the nearest
        // index, and render a small native tooltip on the mirror.
        _attachBorrowedHover(dst, src);
      });
    });
  });
}

let _borrowedTooltipEl = null;
function _borrowedTooltip() {
  if (_borrowedTooltipEl) return _borrowedTooltipEl;
  const el = document.createElement('div');
  el.id = 'borrowedHoverTooltip';
  el.style.cssText = 'position:fixed;pointer-events:none;z-index:9999;'
    + 'background:rgba(18,18,22,0.97);color:var(--fg);'
    + 'border:1px solid var(--border);border-radius:4px;'
    + 'padding:6px 8px;font-size:11px;line-height:1.45;'
    + 'font-family:system-ui,-apple-system,sans-serif;'
    + 'box-shadow:0 4px 12px rgba(0,0,0,0.5);display:none;'
    + 'white-space:nowrap;';
  document.body.appendChild(el);
  _borrowedTooltipEl = el;
  return el;
}

function _fmtBorrowed(v) {
  if (v == null) return '—';
  const a = Math.abs(v);
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2);
}

function _attachBorrowedHover(mirror, homeCanvas) {
  // Resolve the home chart once. If the home canvas isn't a Chart.js
  // canvas (some cards have static SVGs), skip — nothing to mirror.
  const chart = (typeof Chart !== 'undefined' && Chart.getChart) ? Chart.getChart(homeCanvas) : null;
  if (!chart) return;
  const tip = _borrowedTooltip();
  mirror.addEventListener('mousemove', (e) => {
    const labels = chart.data.labels;
    if (!labels || !labels.length) { tip.style.display = 'none'; return; }
    const rect = mirror.getBoundingClientRect();
    const xRatio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    // Snap to the nearest data index — proportional, since the mirror
    // can be a different pixel width than the home canvas (it picks up
    // 100% of the borrowed card's column width).
    const idx = Math.round(xRatio * (labels.length - 1));
    const ts = labels[idx];
    const tsStr = ts instanceof Date
      ? ts.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })
      : String(ts);
    let html = `<div style="color:var(--fg);font-weight:600;margin-bottom:3px;">${tsStr}</div>`;
    for (const ds of chart.data.datasets) {
      const v = ds.data[idx];
      const swatch = `<span style="display:inline-block;width:9px;height:9px;background:${ds.borderColor};border-radius:2px;margin-right:6px;vertical-align:middle;"></span>`;
      html += `<div>${swatch}${ds.label}: ${_fmtBorrowed(v)}</div>`;
    }
    tip.innerHTML = html;
    tip.style.display = 'block';
    // Offset 12px from cursor; flip to the left if we'd overflow the viewport.
    const tipW = tip.offsetWidth, tipH = tip.offsetHeight;
    const left = (e.clientX + 12 + tipW > window.innerWidth) ? e.clientX - tipW - 12 : e.clientX + 12;
    const top  = (e.clientY + 12 + tipH > window.innerHeight) ? e.clientY - tipH - 12 : e.clientY + 12;
    tip.style.left = left + 'px';
    tip.style.top  = top + 'px';
  });
  mirror.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
}

function addBorrowedCard(cardId) {
  if (!layout.overallBorrowed) layout.overallBorrowed = [];
  if (layout.overallBorrowed.includes(cardId)) return;
  layout.overallBorrowed.push(cardId);
  const grid = document.getElementById('overallGrid');
  if (!grid) return;
  const shell = document.createElement('div');
  shell.className = 'card';
  shell.dataset.card = 'ov-borrow-' + cardId;
  shell.style.minHeight = '120px';
  grid.appendChild(shell);
  // Give the new shell its resize button and apply any previously-saved
  // size (e.g. user added → resized → reloaded). syncBorrowedCards()
  // below will re-add the button after innerHTML copy, but doing it now
  // means it's available for the brief window before the first sync.
  _ensureSizeBtn(shell);
  const saved = (layout.cardSizes || {})['ov-borrow-' + cardId];
  if (saved) _applyCardSize(shell, saved);
  syncBorrowedCards();
  saveLayout();
}

function removeBorrowedCard(cardId) {
  layout.overallBorrowed = (layout.overallBorrowed || []).filter(id => id !== cardId);
  const mirror = document.querySelector(`#overallGrid [data-card="ov-borrow-${cardId}"]`);
  if (mirror) mirror.remove();
  // Also prune from saved order
  layout.overallOrder = (layout.overallOrder || []).filter(id => id !== 'ov-borrow-' + cardId);
  saveLayout();
}

// LMS dashboard card order persistence (stored alongside main layout)
let _lmsLayoutInFlight = null;
let _lmsLayoutPending  = false;
async function saveLmsLayout() {
  if (_lmsLayoutInFlight) { _lmsLayoutPending = true; return _lmsLayoutInFlight; }
  _lmsLayoutInFlight = (async () => {
    try {
      do {
        _lmsLayoutPending = false;
        const grid = document.getElementById('lmsCardGrid');
        if (!grid) return;
        const lmsOrder = [...grid.querySelectorAll('.card')].map(c => c.dataset.card);
        try {
          const current = await fetch('/api/layout').then(r => r.json());
          current.lmsOrder = lmsOrder;
          await fetch('/api/layout', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(current),
          });
        } catch(_) {}
      } while (_lmsLayoutPending);
    } finally {
      _lmsLayoutInFlight = null;
    }
  })();
  return _lmsLayoutInFlight;
}

function applyLmsLayout(savedOrder) {
  const grid = document.getElementById('lmsCardGrid');
  if (!grid || !savedOrder || !savedOrder.length) return;
  const cards = [...grid.querySelectorAll('.card')];
  const ordered = [];
  savedOrder.forEach(id => { const c = cards.find(c => c.dataset.card === id); if (c) ordered.push(c); });
  cards.forEach(c => { if (!ordered.includes(c)) ordered.push(c); });
  ordered.forEach(c => grid.appendChild(c));
}

// Manager dashboard card order persistence — same shape as LMS, separate
// layout key so reordering one grid never clobbers the other.
let _managerLayoutInFlight = null;
let _managerLayoutPending  = false;
async function saveManagerLayout() {
  if (_managerLayoutInFlight) { _managerLayoutPending = true; return _managerLayoutInFlight; }
  _managerLayoutInFlight = (async () => {
    try {
      do {
        _managerLayoutPending = false;
        const grid = document.getElementById('managerCardGrid');
        if (!grid) return;
        const order = [...grid.querySelectorAll('.card')].map(c => c.dataset.card);
        try {
          const current = await fetch('/api/layout').then(r => r.json());
          current.managerOrder = order;
          await fetch('/api/layout', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(current),
          });
        } catch(_) {}
      } while (_managerLayoutPending);
    } finally {
      _managerLayoutInFlight = null;
    }
  })();
  return _managerLayoutInFlight;
}

function applyManagerLayout(savedOrder) {
  const grid = document.getElementById('managerCardGrid');
  if (!grid || !savedOrder || !savedOrder.length) return;
  const cards = [...grid.querySelectorAll('.card')];
  const ordered = [];
  savedOrder.forEach(id => { const c = cards.find(c => c.dataset.card === id); if (c) ordered.push(c); });
  cards.forEach(c => { if (!ordered.includes(c)) ordered.push(c); });
  ordered.forEach(c => grid.appendChild(c));
}

function toggleCard(cardId, visible) {
  if (visible) {
    layout.hidden = layout.hidden.filter(id => id !== cardId);
  } else {
    if (!layout.hidden.includes(cardId)) layout.hidden.push(cardId);
  }
  const card = document.querySelector(`[data-card="${cardId}"]`);
  if (card) card.style.display = visible ? '' : 'none';
  saveLayout();
}

// ---------------------------------------------------------------------------
// Settings panel — compact chips + grid layout selector
// ---------------------------------------------------------------------------

function toggleCard(cardId, visible) {
  let hiddenKey = 'hidden';
  if (CARD_LABELS_OVERALL[cardId]) hiddenKey = 'hiddenOverall';
  else if (CARD_LABELS_LMS[cardId]) hiddenKey = 'lmsHidden';
  else if (CARD_LABELS_MANAGER[cardId]) hiddenKey = 'managerHidden';
  if (!layout[hiddenKey]) layout[hiddenKey] = [];
  if (visible) {
    layout[hiddenKey] = layout[hiddenKey].filter(id => id !== cardId);
  } else {
    if (!layout[hiddenKey].includes(cardId)) layout[hiddenKey].push(cardId);
  }
  const card = document.querySelector(`[data-card="${cardId}"]`);
  if (card) card.style.display = visible ? '' : 'none';
  saveLayout();
}

// Mini SVG icon for N-column grid
function _gridIcon(n) {
  const W = 36, H = 26, gap = 2, pad = 2;
  const colW = (W - pad*2 - gap*(n-1)) / n;
  let rects = '';
  for (let i = 0; i < n; i++) {
    const x = pad + i*(colW + gap);
    rects += `<rect x="${x.toFixed(1)}" y="${pad}" width="${colW.toFixed(1)}" height="${H-pad*2}" rx="1"/>`;
  }
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" fill="none" xmlns="http://www.w3.org/2000/svg">${rects}</svg>`;
}

function _getDashSubTab() {
  // Returns 'lmstudio' | 'manager' | 'llamacpp'. Defaults to llamacpp so the
  // settings panel falls back to llama.cpp when nothing else is active.
  if (document.getElementById('dash-lmstudio')?.classList.contains('active')) return 'lmstudio';
  if (document.getElementById('dash-manager')?.classList.contains('active'))  return 'manager';
  return 'llamacpp';
}

function _getGridColsKey() {
  if (_activeTab === 'overall') return 'overallCols';
  if (_activeTab === 'dashboard') {
    const sub = _getDashSubTab();
    if (sub === 'lmstudio') return 'lmsCols';
    if (sub === 'manager')  return 'managerCols';
    return 'cols';
  }
  return 'cols';
}

function _getGridEl() {
  if (_activeTab === 'overall') return document.getElementById('overallGrid');
  if (_activeTab === 'dashboard') {
    const sub = _getDashSubTab();
    if (sub === 'lmstudio') return document.getElementById('lmsCardGrid');
    if (sub === 'manager')  return document.getElementById('managerCardGrid');
    return document.getElementById('cardGrid');
  }
  return null;
}

function applyGridCols(n, save) {
  const el = _getGridEl();
  if (el) el.style.gridTemplateColumns = `repeat(${n}, 1fr)`;
  if (save) {
    const key = _getGridColsKey();
    layout[key] = n;
    saveLayout();
    // re-render selector to update active state
    const sel = document.getElementById('settingsGridSel');
    if (sel) _renderGridSelector(sel, n);
  }
}

function _renderGridSelector(container, current) {
  container.innerHTML = [2,3,4,5].map(n =>
    `<div class="grid-opt ${n === current ? 'grid-opt-active' : ''}" onclick="applyGridCols(${n}, true)" title="${n} columns">
      ${_gridIcon(n)}
      <span>${n} cols</span>
    </div>`
  ).join('');
}

function _settingsCollapseToggle(hdr, body) {
  const open = hdr.classList.toggle('open');
  body.style.maxHeight = open ? body.scrollHeight + 'px' : '0';
}

function renderSettingsPanel() {
  const list  = document.getElementById('settingsList');
  const title = document.getElementById('settingsTitle');

  let label, map, hiddenKey, colsKey;
  if (_activeTab === 'overall') {
    label    = 'LLM Overall'; map = CARD_LABELS_OVERALL;
    hiddenKey = 'hiddenOverall'; colsKey = 'overallCols';
  } else if (_activeTab === 'dashboard') {
    const sub = _getDashSubTab();
    if (sub === 'lmstudio') {
      label = 'Dashboard · LM Studio'; map = CARD_LABELS_LMS;
      hiddenKey = 'lmsHidden'; colsKey = 'lmsCols';
    } else if (sub === 'manager') {
      label = 'Dashboard · Manager'; map = CARD_LABELS_MANAGER;
      hiddenKey = 'managerHidden'; colsKey = 'managerCols';
    } else {
      label = 'Dashboard · llama.cpp'; map = CARD_LABELS;
      hiddenKey = 'hidden'; colsKey = 'cols';
    }
  } else {
    label = 'Dashboard · llama.cpp'; map = CARD_LABELS;
    hiddenKey = 'hidden'; colsKey = 'cols';
  }
  if (title) title.textContent = 'Settings — ' + label;

  const hidden  = layout[hiddenKey] || [];
  const curCols = layout[colsKey]   || 3;

  // Build chips HTML — labels prefer live hardware names over the static map
  // so the picker shows e.g. "Radeon RX 7900 XTX" instead of "GPU".
  const chips = Object.entries(map).map(([id, _lbl]) => {
    const on = !hidden.includes(id);
    const lbl = _cardLabel(id, map);
    return `<span class="card-chip ${on ? 'chip-on' : 'chip-off'}" onclick="toggleCard('${id}', ${!on}); renderSettingsPanel();" title="${on ? 'Click to hide' : 'Click to show'}">
      <span class="chip-dot"></span>${_esc(lbl)}
    </span>`;
  }).join('');

  // When on Overall tab: build chips for borrowable cards from other dashboards
  let borrowSection = '';
  if (_activeTab === 'overall') {
    const borrowed = layout.overallBorrowed || [];
    const groups = [
      { label: 'llama.cpp cards', map: CARD_LABELS },
      { label: 'LM Studio cards', map: CARD_LABELS_LMS },
      { label: 'Manager cards',   map: CARD_LABELS_MANAGER },
    ];
    let inner = '';
    groups.forEach(g => {
      const groupChips = Object.entries(g.map).map(([id, _lbl]) => {
        const on = borrowed.includes(id);
        const action = on ? `removeBorrowedCard('${id}')` : `addBorrowedCard('${id}')`;
        const lbl = _cardLabel(id, g.map);
        return `<span class="card-chip ${on ? 'chip-on' : 'chip-off'}" onclick="${action}; renderSettingsPanel();" title="${on ? 'Click to remove from Overall' : 'Click to add to Overall'}">
          <span class="chip-dot"></span>${_esc(lbl)}
        </span>`;
      }).join('');
      inner += `<div style="font-size:0.74em;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.05em;margin:8px 0 4px;">${g.label}</div>
        <div class="card-chips">${groupChips}</div>`;
    });
    borrowSection = `
      <div class="settings-section-title">Add cards from other pages
        <span style="color:var(--fg-dim);font-size:0.8em;font-weight:400;margin-left:6px;">(${borrowed.length} pinned)</span>
      </div>
      <div class="settings-collapse-hdr${borrowed.length ? ' open' : ''}" id="sBorHdr"
           onclick="_settingsCollapseToggle(this, document.getElementById('sBorBody'))">
        <span style="font-size:0.78em;color:inherit;">Pin any Dashboard card to the Overall page</span>
        <span class="settings-collapse-arrow">▾</span>
      </div>
      <div class="settings-collapse-body" id="sBorBody" style="max-height:${borrowed.length ? '500px' : '0'};">
        ${inner}
      </div>
    `;
  }

  // Build preset dropdown options.
  const presetOpts = Object.entries(LAYOUT_PRESETS).map(([id, p]) =>
    `<option value="${id}">${_esc(p.label)}</option>`).join('');

  list.innerHTML = `
    <div class="settings-section-title" style="border-top:none;padding-top:0;margin-top:0;">Visible cards</div>
    <div class="settings-collapse-hdr open" id="sColHdr" onclick="_settingsCollapseToggle(this, document.getElementById('sColBody'))">
      <span style="font-size:0.78em;color:inherit;">${Object.keys(map).length} cards — click to collapse</span>
      <span class="settings-collapse-arrow">▾</span>
    </div>
    <div class="settings-collapse-body" id="sColBody" style="max-height:400px;">
      <div class="card-chips">${chips}</div>
    </div>
    ${borrowSection}
    <div class="settings-section-title">Layout preset — ${label}</div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;">
      <select id="settingsLayoutPreset" style="flex:1;padding:6px 8px;background:var(--bg-elev);color:var(--fg);border:1px solid var(--border);border-radius:4px;">
        <option value="">— choose a preset —</option>
        ${presetOpts}
      </select>
      <button class="btn btn-gray-muted-gradient"
              onclick="(function(){var v=document.getElementById('settingsLayoutPreset').value; if(v) applyLayoutPreset(v);})()"
              title="Apply the selected preset to this tab">Apply</button>
    </div>
    <div style="font-size:0.74em;color:var(--fg-dim);margin-bottom:14px;">
      Each card also has a ⤢ button in its top-right corner to cycle its size individually (1×1 → 2×1 → 2×2 → 1×2).
    </div>
    <div class="settings-section-title">Columns — ${label}</div>
    <div class="grid-selector" id="settingsGridSel"></div>
    <div style="margin-top:14px;display:flex;justify-content:flex-end;">
      <button class="btn btn-gray-muted-gradient"
              onclick="resetCurrentTabLayout()"
              title="Restore default card order, visibility, columns, and size for this tab only">
        ⟲ Reset layout for this tab
      </button>
    </div>
  `;

  _renderGridSelector(document.getElementById('settingsGridSel'), curCols);
}

// Reset only the active tab's layout keys back to defaults. Other tabs
// keep their order/visibility/size/columns. Card sizes are cleared just
// for the IDs that belong to the active tab (looked up via the right
// CARD_LABELS_* map).
async function resetCurrentTabLayout() {
  // Figure out which tab is active and which keys to clear.
  let scope, map;
  if (_activeTab === 'overall') {
    scope = { hidden: 'hiddenOverall', order: 'overallOrder', cols: 'overallCols', borrowed: 'overallBorrowed' };
    map = CARD_LABELS_OVERALL;
  } else if (_activeTab === 'dashboard') {
    const sub = _getDashSubTab();
    if (sub === 'lmstudio') {
      scope = { hidden: 'lmsHidden', order: 'lmsOrder', cols: 'lmsCols' };
      map = CARD_LABELS_LMS;
    } else if (sub === 'manager') {
      scope = { hidden: 'managerHidden', order: 'managerOrder', cols: 'managerCols' };
      map = CARD_LABELS_MANAGER;
    } else {
      scope = { hidden: 'hidden', order: 'order', cols: 'cols' };
      map = CARD_LABELS;
    }
  } else {
    return;
  }
  const ok = await _themedConfirm({
    title:        'Reset the card layout for this tab back to default?',
    bodyHtml:     'This clears card order, visibility, column count, and resized sizes for this tab only. Other tabs are unaffected.',
    confirmLabel: 'Reset',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  // Mutate the in-memory layout, then POST the whole thing back.
  layout[scope.hidden] = [];
  layout[scope.order]  = [];
  delete layout[scope.cols];
  if (scope.borrowed) layout[scope.borrowed] = [];
  // Drop cardSizes entries for ids in this tab's label map.
  if (layout.cardSizes) {
    for (const id of Object.keys(map)) delete layout.cardSizes[id];
  }
  try {
    await fetch('/api/layout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(layout),
    });
  } catch (e) { /* best-effort */ }
  // Simplest correct refresh: reload the page so every grid, chart,
  // sortable, and resize-binding re-initializes against the fresh
  // layout. Avoids drift between in-memory caches and the new state.
  window.location.reload();
}

function openSettings() {
  renderSettingsPanel();
  _syncIntervalUI();
  const sel = document.getElementById('themeSelect');
  if (sel) sel.value = (layout && layout.theme) || 'dark';
  document.getElementById('settingsOverlay').classList.add('open');
}
function closeSettings() {
  document.getElementById('settingsOverlay').classList.remove('open');
}

let _intervalMode = 'auto';
function _syncIntervalUI() {
  const radios = document.querySelectorAll('input[name=intervalMode]');
  radios.forEach(r => { r.checked = (r.value === _intervalMode); });
  const inp  = document.getElementById('intervalManualVal');
  const unit = document.getElementById('intervalManualUnit');
  const isManual = _intervalMode === 'manual';
  if (inp)  inp.style.display  = isManual ? '' : 'none';
  if (unit) unit.style.display = isManual ? '' : 'none';
}

async function applyIntervalMode() {
  const selected = document.querySelector('input[name=intervalMode]:checked')?.value || 'auto';
  _intervalMode = selected;
  _syncIntervalUI();
  const val = parseInt(document.getElementById('intervalManualVal')?.value || '5', 10);
  try {
    await fetch('/api/config/interval', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(selected === 'manual' ? {mode: 'manual', value: val} : {mode: 'auto'}),
    });
    checkConfig();
  } catch(e) {}
}

// Apply saved column counts from layout on load
function applyAllGridCols() {
  const pairs = [
    [document.getElementById('overallGrid'),  layout.overallCols],
    [document.getElementById('cardGrid'),     layout.cols],
    [document.getElementById('lmsCardGrid'),  layout.lmsCols],
    [document.getElementById('managerCardGrid'), layout.managerCols],
  ];
  pairs.forEach(([el, n]) => {
    if (el && n) el.style.gridTemplateColumns = `repeat(${n}, 1fr)`;
  });
}


// switchTab — tab dispatcher (moved here so tab batches can rely on it)
function switchTab(tab) {
  if (tab === 'admin' && window._me && window._me.admin_access === false) { tab = 'overall'; }
  _activeTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelector(`.tab-btn[onclick="switchTab('${tab}')"]`).classList.add('active');

  const tabs = ['overallTab','dashboardTab','llmTab','eventsTab','openclawTab','llmchatTab','imggenTab','adminTab'];
  tabs.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  // Also hide legacy cardGrid if still top-level
  const cg = document.getElementById('cardGrid');
  if (cg && !cg.closest('#dashboardTab')) cg.style.display = 'none';

  if (tab === 'overall')    { document.getElementById('overallTab').style.display    = '';   fetchOverallMetrics(); }
  if (tab === 'dashboard')  { document.getElementById('dashboardTab').style.display  = '';   }
  if (tab === 'events')     {
    document.getElementById('eventsTab').style.display = '';
    _ensureAlarmIframeLoaded();
  }
  // Hide main-dashboard toast container when on Events tab — the alarm engine
  // iframe shows its own toasts there. Prevents stacking duplicate toasts.
  {
    const _atc = document.getElementById('alarmToastContainer');
    if (_atc) _atc.style.visibility = (tab === 'events') ? 'hidden' : '';
  }
  if (tab === 'llm')        {
    document.getElementById('llmTab').style.display = '';
    refreshLLMTab();
    loadHFTrending();
    startPerfRefresh();
    _initLLMSections();
    // Always restart the log stream on every visit (it's stopped on leave)
    if (_logPanelOpen) startLogStream();
  }
  if (tab === 'openclaw')   { document.getElementById('openclawTab').style.display   = ''; }
  if (tab === 'llmchat')    { document.getElementById('llmchatTab').style.display    = ''; }
  if (tab === 'imggen')     { document.getElementById('imggenTab').style.display     = ''; }
  if (tab === 'admin')      { document.getElementById('adminTab').style.display      = ''; adminLoadAgents(); adminLoadHealth(); adminAuthLoad(); adminStartAutoRefresh(); }
  else                       { adminStopAutoRefresh(); }
  if (tab !== 'llm')        { stopLogStream(); stopPerfRefresh(); stopLmsLogRefresh(); }
}

// ── Role-aware UI (multi-user, #125) ────────────────────────────────────────
window._me = window._me || { role: 'admin', is_admin: true, admin_access: true, username: null, authenticated: false };

async function loadMe() {
  try {
    const r = await fetch('/api/me');
    if (!r.ok) return;
    window._me = await r.json();
  } catch (_) {}
  applyRoleGating();
}

function applyRoleGating() {
  const isAdmin = !!(window._me && window._me.admin_access);
  const adminBtn = document.getElementById('tabBtnAdmin');
  if (adminBtn) adminBtn.style.display = isAdmin ? '' : 'none';
  // Account (change-my-password) shows only for a real logged-in (non-bypass) session.
  const acct = document.getElementById('tabBtnAccount');
  if (acct) acct.style.display = (window._me && window._me.authenticated) ? '' : 'none';
  if (!isAdmin && _activeTab === 'admin') switchTab('overall');
}

async function accountMenu() {
  const me = window._me || {};
  const action = await _accountActionMenu(me);
  if (action === 'logout') { window.location.href = '/logout'; return; }
  if (action === 'password') await _accountChangePassword();
}

// Themed account menu → 'password' | 'logout' | null. Built inline because the
// shared dialog helpers don't offer a 3-way choice; styling mirrors _themedConfirm.
function _accountActionMenu(me) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;'
      + 'display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px);';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
      + 'padding:20px 22px;min-width:340px;max-width:480px;color:var(--fg);'
      + 'font-family:system-ui,-apple-system,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    box.innerHTML = `
      <div style="font-size:1.05em;font-weight:600;margin-bottom:6px;">Account</div>
      <div style="font-size:0.85em;color:var(--fg-muted,#9aa);margin-bottom:16px;">Signed in as <b>${_esc(me.username || '')}</b> (${_esc(me.role || '')})</div>
      <div style="display:flex;flex-direction:column;gap:8px;">
        <button id="amPw" style="background:var(--bg-card-alt);color:var(--fg);border:1px solid var(--border);border-radius:5px;padding:9px 14px;cursor:pointer;font-size:0.9em;text-align:left;">Change my password</button>
        <button id="amOut" style="background:#a33;color:#fff;border:1px solid var(--border);border-radius:5px;padding:9px 14px;cursor:pointer;font-size:0.9em;text-align:left;font-weight:500;">Log out</button>
        <button id="amCancel" style="background:transparent;color:var(--fg-muted,#9aa);border:1px solid var(--border);border-radius:5px;padding:7px 14px;cursor:pointer;font-size:0.85em;">Cancel</button>
      </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const cleanup = (v) => { document.removeEventListener('keydown', keyHandler); overlay.remove(); resolve(v); };
    const keyHandler = (e) => { if (e.key === 'Escape') cleanup(null); };
    document.addEventListener('keydown', keyHandler);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(null); });
    box.querySelector('#amPw').addEventListener('click', () => cleanup('password'));
    box.querySelector('#amOut').addEventListener('click', () => cleanup('logout'));
    box.querySelector('#amCancel').addEventListener('click', () => cleanup(null));
    setTimeout(() => box.querySelector('#amPw').focus(), 0);
  });
}

async function _accountChangePassword() {
  const cur = await _themedPrompt({ title: 'Change my password', bodyHtml: 'Current password:', placeholder: 'current password', inputType: 'password' });
  if (cur === null) return;
  const np = await _themedPrompt({ title: 'Change my password', bodyHtml: 'New password (min 8):', placeholder: 'new password', inputType: 'password' });
  if (np === null) return;
  if (np.length < 8) { _themedToast('password too short', { kind: 'warn' }); return; }
  try {
    const r = await fetch('/api/account/password', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: cur, new_password: np }) });
    const d = await r.json().catch(() => ({}));
    _themedToast((r.ok && d.ok) ? 'Password changed' : (d.error || 'failed'), { kind: (r.ok && d.ok) ? 'ok' : 'err' });
  } catch (_) { _themedToast('request failed', { kind: 'err' }); }
}
