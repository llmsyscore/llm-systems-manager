// ── Multi-agent picker state (PR3) ──────────────────────────────────
// _selectedAgents[provider] = agent_id of the dashboard picker's selection,
// or null. _withAgentParam() appends ?agent=<id> to same-origin provider API
// URLs so the manager dispatcher routes to the picked host. No-op when no
// selection exists (single-agent installs) → byte-identical behavior.
window._agentsByProvider = window._agentsByProvider || { llama: [], lms: [], vllm: [] };
window._selectedAgents   = window._selectedAgents   || { llama: null, lms: null, vllm: null };
// Last selection each provider's grid was rendered for, so the 30s picker poll
// only re-applies per-agent layout when the selection actually changed.
let _appliedAgentSel = { llama: undefined, lms: undefined, vllm: undefined };

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
  [/^\/api\/vllm\//,     'vllm'],
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
    // Explicit ?provider= (provider-aware routes like /api/benchmark/*)
    // overrides the path map's provider.
    const provMatch = /[?&]provider=([a-z0-9_-]+)/.exec(url);
    const provider = provMatch ? provMatch[1] : _providerForApiPath(path);
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

// True when a header state pill is hidden (provider has no agent).
function _pillHidden(id) {
  const el = document.getElementById(id);
  return !el || el.style.display === 'none';
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

const CARD_LABELS_LMS = {
  'lms-models':  'LM Studio Models',
  'lms-active':  'LM Studio Server',
  'lms-cpu':     'LM Studio CPU',
  'lms-ram':     'LM Studio RAM',
  'lms-network': 'LM Studio Network',
  'lms-disk':    'LM Studio Disk',
  'lms-io':      'LM Studio Disk IO',
  'lms-power':   'LM Studio powermetrics',
};
const CARD_LABELS_VLLM = {
  'vllm-server':     'vLLM Server',
  'vllm-requests':   'vLLM Requests',
  'vllm-kv':         'vLLM KV Cache',
  'vllm-throughput': 'vLLM Throughput',
  'vllm-cpu':        'vLLM CPU',
  'vllm-ram':        'vLLM RAM',
  'vllm-network':    'vLLM Network',
  'vllm-disk':       'vLLM Disk',
  'vllm-io':         'vLLM Disk IO',
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
    if (!layout.vllmHidden)      layout.vllmHidden      = [];
    if (!layout.vllmOrder)       layout.vllmOrder       = [];
    if (!layout.managerHidden)   layout.managerHidden   = [];
    if (!layout.hiddenByAgent || typeof layout.hiddenByAgent !== 'object') layout.hiddenByAgent = {};
    if (!layout.orderByAgent || typeof layout.orderByAgent !== 'object') layout.orderByAgent = {};
    if (!layout.sizesByAgent || typeof layout.sizesByAgent !== 'object') layout.sizesByAgent = {};
    if (!layout.managerOrder)    layout.managerOrder    = [];
    if (!layout.overallBorrowed) layout.overallBorrowed = [];
    if (!layout.overallOrder)    layout.overallOrder    = [];
    if (!layout.cardSizes || typeof layout.cardSizes !== 'object') layout.cardSizes = {};
    if (!layout.rolePreset || typeof layout.rolePreset !== 'object') layout.rolePreset = {};
    _migrateLegacyCardIds(layout);
    _applyOrderForGrid('lmsCardGrid', 'lmsOrder');
    _applyOrderForGrid('vllmCardGrid', 'vllmOrder');
    if (layout.managerOrder) applyManagerLayout(layout.managerOrder);
  } catch(e) {}
  if (layout && layout.theme) layout.theme = SettingsLib.normalizeTheme(layout.theme);
  applyTheme(layout && layout.theme, false);
  applyLayout();
  applyAllGridCols();
  if (typeof applyDensity === 'function') applyDensity(layout && layout.density, false);
  if (typeof applyLayoutEngine === 'function') applyLayoutEngine(layout && layout.layoutEngine, false);
}

// kraken → aio: existing saved layouts written before the rename still carry
// the old card id. Rewrite in place so applyLayout() finds the card and the
// next saveLayout() writes the new id.
const _LEGACY_CARD_RENAMES = {
  'kraken': 'aio',
  // #565 retired the native ov-* aggregate cards (fleet band replaces them);
  // stale saved ids simply match no card and are pruned on the next save.
};
function _migrateLegacyCardIds(lay) {
  const swap = arr => Array.isArray(arr) && arr.forEach((id, i) => {
    if (_LEGACY_CARD_RENAMES[id]) arr[i] = _LEGACY_CARD_RENAMES[id];
  });
  const swapKeys = obj => {
    if (!obj || typeof obj !== 'object') return;
    for (const oldId in _LEGACY_CARD_RENAMES) {
      if (oldId in obj) {
        const newId = _LEGACY_CARD_RENAMES[oldId];
        if (!(newId in obj)) obj[newId] = obj[oldId];
        delete obj[oldId];
      }
    }
  };
  // Per-agent buckets: byAgent[provider][agentId] = [cardId...] or {cardId:size}.
  const eachAgent = (bucket, fn) => {
    if (!bucket || typeof bucket !== 'object') return;
    for (const prov in bucket) {
      const byAgent = bucket[prov];
      if (byAgent && typeof byAgent === 'object') for (const aid in byAgent) fn(byAgent[aid]);
    }
  };
  swap(lay.order); swap(lay.hidden);
  swap(lay.lmsOrder); swap(lay.lmsHidden);
  swap(lay.managerOrder); swap(lay.managerHidden);
  swap(lay.overallOrder); swap(lay.overallBorrowed); swap(lay.hiddenOverall);
  eachAgent(lay.hiddenByAgent, swap);
  eachAgent(lay.orderByAgent, swap);
  eachAgent(lay.sizesByAgent, swapKeys);
  swapKeys(lay.cardSizes);
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
  vllm:  ['agentPickerDashVllm', 'agentPickerCtrlVllm'],
};

// Primary (is_default) agent first; stable backend order for the rest.
function _defaultFirst(list) {
  return (list || []).slice().sort((a, b) => (b.is_default ? 1 : 0) - (a.is_default ? 1 : 0));
}

async function _loadAgentsByProvider() {
  try {
    const data = await fetch('/api/agents/list-by-provider').then(r => r.json());
    window._agentsByProvider = {
      llama: _defaultFirst(data.llama), lms: _defaultFirst(data.lms), vllm: _defaultFirst(data.vllm),
    };
    // Restore persisted selection; else fall back to the provider default.
    ['llama', 'lms', 'vllm'].forEach(prov => {
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
    // Re-apply a provider's per-agent order/visibility/sizes only when its
    // selection changed (first resolve or a deleted-agent fallback).
    if (typeof _applyAgentGrid === 'function') {
      ['llama', 'lms', 'vllm'].forEach(prov => {
        const sel = _selectedAgent(prov);
        if (sel !== _appliedAgentSel[prov]) { _appliedAgentSel[prov] = sel; _applyAgentGrid(prov); }
      });
    }
  } catch (_) {}
}

function _renderAgentPickers() {
  ['llama', 'lms', 'vllm'].forEach(prov => {
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
  // Apply the new selection's per-agent order/visibility/sizes BEFORE saving, so
  // saveLayout() captures the new agent's card order, not the old grid's.
  _applyAgentGrid(provider);
  _appliedAgentSel[provider] = agentId;
  // Persist into layout — saveLayout() self-coalesces concurrent calls, so a
  // rapid chip-switch makes one POST, not one per click.
  if (typeof layout === 'object' && layout) {
    layout._selectedAgents = { ...(layout._selectedAgents || {}), [provider]: agentId };
    try { saveLayout(); } catch (_) {}
  }
  _renderAgentPickers();
  if (typeof renderSettingsPanel === 'function'
      && document.getElementById('settingsOverlay')?.classList.contains('open')) renderSettingsPanel();
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
    // Re-resolve host-scoped alarm-rule threshold lines for the new agent.
    if (typeof _applyThresholds === 'function')      _applyThresholds();
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
  } else if (provider === 'vllm') {
    _clearBars('vllmDiskList');
    // Backfill the new agent's history, then resume the live poll (#358) —
    // same shape as the LMS branch above.
    if (typeof loadVllmHistory === 'function') {
      loadVllmHistory().finally(() => { if (typeof fetchVllmMetrics === 'function') fetchVllmMetrics(); });
    } else {
      if (typeof _resetVllmCharts === 'function') _resetVllmCharts();
      if (typeof fetchVllmMetrics === 'function') fetchVllmMetrics();
    }
    if (typeof loadVllmBenchData === 'function') loadVllmBenchData();
    if (typeof _vllmLogOpen !== 'undefined' && _vllmLogOpen
        && typeof startVllmLogRefresh === 'function') startVllmLogRefresh();
    if (typeof closeVllmTerminal === 'function') {
      const _vp = document.getElementById('vllmTerminalPanel');
      if ((typeof _vllmTermSid !== 'undefined' && _vllmTermSid)
          || (_vp && _vp.style.display !== 'none')) closeVllmTerminal();
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

function _osPrefersLight() {
  try { return !!window.matchMedia('(prefers-color-scheme: light)').matches; } catch (_) { return false; }
}
// Saved theme name is normalized (legacy names map forward); the rendered
// theme may differ while "follow system" is on.
function applyTheme(name, save) {
  const saved = SettingsLib.normalizeTheme(name);
  const follow = !!(layout && layout.themeFollowSystem);
  const eff = SettingsLib.effectiveTheme(saved, follow, _osPrefersLight());
  document.documentElement.setAttribute('data-theme', eff);
  _retintCharts();
  _propagateThemeToAlarmEngine(eff);
  if (save) {
    if (typeof layout !== 'object' || !layout) layout = {};
    layout.theme = saved;
    saveLayout();
  }
  if (typeof _sdRenderAppearance === 'function') _sdRenderAppearance();
}
function setThemeFollowSystem(on) {
  if (typeof layout !== 'object' || !layout) layout = {};
  layout.themeFollowSystem = !!on;
  saveLayout();
  applyTheme(layout.theme, false);
}
try {
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (layout && layout.themeFollowSystem) applyTheme(layout.theme, false);
  });
} catch (_) {}

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

// Effective hidden-card list for a surface — per-agent for the llama.cpp/LMS
// dashboards, global elsewhere. Falls back to the global list if the lib is absent.
function _hiddenList(hiddenKey) {
  const lib = window.LMLayout;
  if (!lib) {
    if (!Array.isArray(layout[hiddenKey])) layout[hiddenKey] = [];
    return layout[hiddenKey];
  }
  const prov = lib.PER_AGENT_HIDDEN[hiddenKey] || null;
  const agentId = prov ? _selectedAgent(prov) : null;
  return lib.resolveHiddenList(layout, hiddenKey, agentId);
}

// Apply a grid's card visibility from its (possibly per-agent) hidden list.
function _applyHiddenForGrid(gridId, hiddenKey) {
  const grid = document.getElementById(gridId);
  if (!grid) return;
  const hidden = _hiddenList(hiddenKey);
  grid.querySelectorAll('.card').forEach(c => {
    c.style.display = hidden.includes(c.dataset.card) ? 'none' : '';
  });
}

// Effective card-order list for a surface — per-agent for llama.cpp/LMS, global
// elsewhere. Falls back to the global list if the lib is absent.
function _orderList(orderKey) {
  const lib = window.LMLayout;
  if (!lib) {
    if (!Array.isArray(layout[orderKey])) layout[orderKey] = [];
    return layout[orderKey];
  }
  const prov = lib.PER_AGENT_ORDER[orderKey] || null;
  const agentId = prov ? _selectedAgent(prov) : null;
  return lib.resolveOrderList(layout, orderKey, agentId);
}

// Reorder a grid's cards by its (possibly per-agent) order list.
function _applyOrderForGrid(gridId, orderKey) {
  const grid = document.getElementById(gridId);
  if (!grid) return;
  const order = _orderList(orderKey);
  if (!order || !order.length) return;
  const cards = [...grid.querySelectorAll('.card')];
  const ordered = [];
  order.forEach(id => { const c = cards.find(c => c.dataset.card === id); if (c) ordered.push(c); });
  cards.forEach(c => { if (!ordered.includes(c)) ordered.push(c); });
  ordered.forEach(c => grid.appendChild(c));
}

// Provider owning a per-agent surface card id, else null (global surfaces).
function _perAgentProviderForCard(cardId) {
  if (CARD_LABELS[cardId]) return 'llama';
  if (CARD_LABELS_LMS[cardId]) return 'lms';
  if (CARD_LABELS_VLLM[cardId]) return 'vllm';
  return null;
}

// cardId->size map holding this card's size — per-agent for llama.cpp/LMS cards,
// global cardSizes otherwise (or when the lib/agent is absent).
function _sizeMapFor(cardId) {
  const lib = window.LMLayout;
  const prov = _perAgentProviderForCard(cardId);
  const agentId = prov ? _selectedAgent(prov) : null;
  if (lib && prov && agentId) {
    const seedIds = prov === 'llama' ? Object.keys(CARD_LABELS)
                  : prov === 'vllm' ? Object.keys(CARD_LABELS_VLLM)
                  : Object.keys(CARD_LABELS_LMS);
    return lib.resolveSizeMap(layout, prov, agentId, seedIds);
  }
  if (!layout.cardSizes || typeof layout.cardSizes !== 'object') layout.cardSizes = {};
  return layout.cardSizes;
}

// Re-apply order, visibility, and sizes for a provider's per-agent grid.
function _applyAgentGrid(provider) {
  if (provider === 'llama') {
    _applyOrderForGrid('cardGrid', 'order');
    _applyHiddenForGrid('cardGrid', 'hidden');
  } else if (provider === 'lms') {
    _applyOrderForGrid('lmsCardGrid', 'lmsOrder');
    _applyHiddenForGrid('lmsCardGrid', 'lmsHidden');
  } else if (provider === 'vllm') {
    _applyOrderForGrid('vllmCardGrid', 'vllmOrder');
    _applyHiddenForGrid('vllmCardGrid', 'vllmHidden');
  }
  if (typeof initCardResize === 'function') initCardResize();
}

function applyLayout() {
  // Apply order — Dashboard/llama.cpp cards (per selected agent)
  _applyOrderForGrid('cardGrid', 'order');

  // Apply visibility — Dashboard/llama.cpp cards (per selected agent)
  _applyHiddenForGrid('cardGrid', 'hidden');

  // Apply visibility — LLM Overall cards
  const hiddenOv = layout.hiddenOverall || [];
  document.querySelectorAll('#overallGrid .card').forEach(c => {
    c.style.display = hiddenOv.includes(c.dataset.card) ? 'none' : '';
  });

  // Apply visibility — LMS dashboard cards (per selected agent)
  _applyHiddenForGrid('lmsCardGrid', 'lmsHidden');

  // Apply visibility — vLLM dashboard cards (per selected agent)
  _applyHiddenForGrid('vllmCardGrid', 'vllmHidden');

  // Apply visibility — Manager dashboard cards
  const hiddenMgr = layout.managerHidden || [];
  document.querySelectorAll('#managerCardGrid .card').forEach(c => {
    c.style.display = hiddenMgr.includes(c.dataset.card) ? 'none' : '';
  });

  // Fleet-band strip order (#565)
  _applyBandOrder();

  // Recreate pinned-card shells in overallGrid
  const overallGrid = document.getElementById('overallGrid');
  if (overallGrid) {
    (layout.overallBorrowed || []).forEach(cardId => {
      if (document.querySelector(`#overallGrid [data-card="ov-borrow-${cardId}"]`)) return;
      const shell = document.createElement('div');
      shell.className = 'card ov-shell';
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
    // Adopt into the fresh shells when the Overall tab is showing
    if (_activeTab === 'overall' && typeof adoptPinnedCards === 'function') adoptPinnedCards();
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
        const ids = [...grid.querySelectorAll('.card')].map(c => c.dataset.card);
        const ol = _orderList('order');
        ol.splice(0, ol.length, ...ids);
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
// Pinned cards — DOM adoption (#565). While Overall is active each pinned
// card's real node lives inside its ov-borrow shell in #overallGrid; a
// comment marker holds its home slot for the return trip. Only one tab is
// visible at a time, so the card is never needed in two places at once.
// ---------------------------------------------------------------------------
const _ovHomeMarks = {};
window._ovAdopted = new Set();

function _ovPinned(id) {
  const lay = (typeof layout !== 'undefined' && layout) || window.layout;
  return !!(lay && (lay.overallBorrowed || []).includes(id));
}

function _homeCardEl(id) {
  return document.querySelector(
    `#cardGrid [data-card="${id}"], #lmsCardGrid [data-card="${id}"], ` +
    `#vllmCardGrid [data-card="${id}"], #managerCardGrid [data-card="${id}"]`);
}

function adoptPinnedCards() {
  const lay = (typeof layout !== 'undefined' && layout) || window.layout;
  ((lay && lay.overallBorrowed) || []).forEach(id => {
    const shell = document.querySelector(`#overallGrid [data-card="ov-borrow-${id}"]`);
    if (!shell || _ovAdopted.has(id)) return;
    const home = _homeCardEl(id);
    shell.textContent = '';
    if (!home) {
      shell.innerHTML = '<div class="ov-missing">card unavailable</div>';
      return;
    }
    const mark = document.createComment('ov-home:' + id);
    home.parentNode.insertBefore(mark, home);
    _ovHomeMarks[id] = mark;
    shell.appendChild(home);
    // A pinned card is always visible on Overall — clear any hidden-at-home
    // inline display; the home state is re-applied on return.
    home.style.display = '';
    _ensureSizeBtn(shell);
    _ovAdopted.add(id);
  });
  if (typeof _resizeChartsIn === 'function') _resizeChartsIn(document.getElementById('overallGrid'));
}

function _returnOneAdopted(id) {
  const shell = document.querySelector(`#overallGrid [data-card="ov-borrow-${id}"]`);
  const card = shell && shell.querySelector(`:scope > [data-card="${id}"]`);
  const mark = _ovHomeMarks[id];
  if (card && mark && mark.parentNode) {
    mark.parentNode.replaceChild(card, mark);
    // Re-apply the home grid's hidden state (adoption cleared it).
    const keys = { cardGrid: 'hidden', lmsCardGrid: 'lmsHidden',
                   vllmCardGrid: 'vllmHidden', managerCardGrid: 'managerHidden' };
    const grid = card.closest('.grid');
    if (grid && keys[grid.id] && typeof _applyHiddenForGrid === 'function') {
      _applyHiddenForGrid(grid.id, keys[grid.id]);
    }
    if (typeof _applyCardSize === 'function') _applyCardSize(card, _sizeMapFor(id)[id] || _defaultCardSize(id));
  } else if (mark && mark.parentNode) mark.remove();
  delete _ovHomeMarks[id];
  _ovAdopted.delete(id);
}

function returnPinnedCards() {
  [..._ovAdopted].forEach(_returnOneAdopted);
}

// Reorder the fleet-band strips per layout.overallBandOrder (#565). Unknown
// ids are skipped; strips missing from the saved order append in DOM order.
function _applyBandOrder() {
  const lay = (typeof layout !== 'undefined' && layout) || window.layout;
  const band = document.querySelector('.ov-band');
  const saved = (lay && lay.overallBandOrder) || [];
  if (!band || !saved.length) return;
  const all = [...band.children].filter(s => s.dataset && s.dataset.strip);
  const ordered = [];
  saved.forEach(id => {
    const s = all.find(x => x.dataset.strip === id);
    if (s) ordered.push(s);
  });
  all.forEach(s => { if (!ordered.includes(s)) ordered.push(s); });
  ordered.forEach(s => band.appendChild(s));
}

// Backfill home-provider chart history + kick the manager one-shots for
// pinned cards, so adopted cards are live even if their home dashboard was
// never visited this session (#565, #506 pattern).
function _ovBackfillPinnedProviders() {
  const lay = (typeof layout !== 'undefined' && layout) || window.layout;
  const b = (lay && lay.overallBorrowed) || [];
  if (!b.length) return;
  const run = fn => { if (typeof fn === 'function') Promise.resolve(fn()).catch(() => {}); };
  const hasIn = map => b.some(id => map && map[id] !== undefined);
  if (hasIn(CARD_LABELS))         run(window.loadHistory);
  if (hasIn(CARD_LABELS_LMS))     run(window.loadLmsHistory);
  if (hasIn(CARD_LABELS_VLLM))    run(window.loadVllmHistory);
  if (hasIn(CARD_LABELS_MANAGER)) {
    run(window.loadManagerPerfHistory);
    run(window.fetchServicesAndInflux);
    run(window.fetchManagerAgentsCard);
    run(window.fetchManagerStreamsCard);
  }
}

function addBorrowedCard(cardId) {
  if (!layout.overallBorrowed) layout.overallBorrowed = [];
  if (layout.overallBorrowed.includes(cardId)) return;
  layout.overallBorrowed.push(cardId);
  const grid = document.getElementById('overallGrid');
  if (!grid) return;
  const shell = document.createElement('div');
  shell.className = 'card ov-shell';
  shell.dataset.card = 'ov-borrow-' + cardId;
  shell.style.minHeight = '120px';
  grid.appendChild(shell);
  _ensureSizeBtn(shell);
  const saved = (layout.cardSizes || {})['ov-borrow-' + cardId];
  _applyCardSize(shell, saved || _defaultCardSize(cardId));
  if (_activeTab === 'overall') adoptPinnedCards();
  saveLayout();
}

function removeBorrowedCard(cardId) {
  // Return the live card to its home grid before dropping the shell.
  if (window._ovAdopted && _ovAdopted.has(cardId)) _returnOneAdopted(cardId);
  layout.overallBorrowed = (layout.overallBorrowed || []).filter(id => id !== cardId);
  const shell = document.querySelector(`#overallGrid [data-card="ov-borrow-${cardId}"]`);
  if (shell) { if (typeof _flowUnobserve === 'function') _flowUnobserve(shell); shell.remove(); }
  // Also prune from saved order
  layout.overallOrder = (layout.overallOrder || []).filter(id => id !== 'ov-borrow-' + cardId);
  saveLayout();
}

// Coalescing per-grid card-order saver factory: one POST in flight, a
// trailing re-run when calls land mid-flight.
function _makeLayoutSaver(gridId, orderKey) {
  let inFlight = null;
  let pending  = false;
  return async function () {
    if (inFlight) { pending = true; return inFlight; }
    inFlight = (async () => {
      try {
        do {
          pending = false;
          const grid = document.getElementById(gridId);
          if (!grid) return;
          const ids = [...grid.querySelectorAll('.card')].map(c => c.dataset.card);
          // Per-agent keys (LMLayout.PER_AGENT_ORDER) write through
          // _orderList() and persist layout.orderByAgent too.
          const perAgent = !!(window.LMLayout && window.LMLayout.PER_AGENT_ORDER[orderKey]);
          if (perAgent) {
            const ol = _orderList(orderKey);
            ol.splice(0, ol.length, ...ids);
          }
          try {
            const current = await fetch('/api/layout').then(r => r.json());
            current[orderKey] = perAgent ? layout[orderKey] : ids;
            if (perAgent) current.orderByAgent = layout.orderByAgent;
            await fetch('/api/layout', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(current),
            });
          } catch(_) {}
        } while (pending);
      } finally {
        inFlight = null;
      }
    })();
    return inFlight;
  };
}

// LMS / vLLM / Manager dashboard card-order persistence.
const saveLmsLayout     = _makeLayoutSaver('lmsCardGrid', 'lmsOrder');
const saveVllmLayout    = _makeLayoutSaver('vllmCardGrid', 'vllmOrder');
const saveManagerLayout = _makeLayoutSaver('managerCardGrid', 'managerOrder');

function applyManagerLayout(savedOrder) {
  const grid = document.getElementById('managerCardGrid');
  if (!grid || !savedOrder || !savedOrder.length) return;
  const cards = [...grid.querySelectorAll('.card')];
  const ordered = [];
  savedOrder.forEach(id => { const c = cards.find(c => c.dataset.card === id); if (c) ordered.push(c); });
  cards.forEach(c => { if (!ordered.includes(c)) ordered.push(c); });
  ordered.forEach(c => grid.appendChild(c));
}

// ---------------------------------------------------------------------------
// Settings panel — compact chips + grid layout selector
// ---------------------------------------------------------------------------

function toggleCard(cardId, visible) {
  let hiddenKey = 'hidden';
  if (CARD_LABELS_LMS[cardId]) hiddenKey = 'lmsHidden';
  else if (CARD_LABELS_VLLM[cardId]) hiddenKey = 'vllmHidden';
  else if (CARD_LABELS_MANAGER[cardId]) hiddenKey = 'managerHidden';
  const list = _hiddenList(hiddenKey);
  const idx = list.indexOf(cardId);
  if (visible) { if (idx !== -1) list.splice(idx, 1); }
  else if (idx === -1) { list.push(cardId); }
  const card = document.querySelector(`[data-card="${cardId}"]`);
  if (card) card.style.display = visible ? '' : 'none';
  saveLayout();
}

// ── Settings drawer ─────────────────────────────────────────────────
function _getDashSubTab() {
  // Returns the active Dashboard sub-tab id. Defaults to llamacpp.
  if (document.getElementById('dash-lmstudio')?.classList.contains('active')) return 'lmstudio';
  if (document.getElementById('dash-vllm')?.classList.contains('active'))     return 'vllm';
  if (document.getElementById('dash-manager')?.classList.contains('active'))  return 'manager';
  if (document.getElementById('dash-energy')?.classList.contains('active'))   return 'energy';
  if (document.getElementById('dash-openclaw')?.classList.contains('active')) return 'openclaw';
  return 'llamacpp';
}

function _sdScope() {
  return SettingsLib.settingsScope(_activeTab, _activeTab === 'dashboard' ? _getDashSubTab() : null);
}

function _getGridColsKey() { return _sdScope().cols; }
function _getGridEl() { return document.getElementById(_sdScope().grid); }

function applyGridCols(n, save) {
  const cols = SettingsLib.normalizeCols(n);
  const el = _getGridEl();
  if (el) {
    el.style.gridTemplateColumns = SettingsLib.gridTemplate(cols);
    _relayoutGridCards(el);
    _resizeChartsIn(el);
  }
  if (save) {
    layout[_getGridColsKey()] = cols;
    saveLayout();
    _sdRenderLayout();
    _sdRenderHeader();
  }
}

// Direct children only: adopted cards inside Overall shells carry data-card too.
function _sdVisibleCards(scope) {
  const grid = _sdGridEl(scope);
  if (!grid) return [];
  return [...grid.querySelectorAll(':scope > [data-card]')].filter(c => c.style.display !== 'none');
}
function _sdCurrentPreset(scope, cols) {
  const cards = _sdVisibleCards(scope);
  if (_sdIsFlow()) {
    const custom = cards.some(c => c.dataset.size && c.dataset.size !== 'auto');
    return custom ? null : _rolePresetFor(_sdGridEl(scope));
  }
  const sizes = {};
  cards.forEach((c, i) => { sizes[i] = c.dataset.size || 'auto'; });
  return SettingsLib.matchPreset(cols, sizes);
}
function _sdIsFlow() { return SettingsLib.normalizeEngine(layout && layout.layoutEngine) === 'flow'; }
function _sdGridEl(scope) { return scope.grid instanceof Element ? scope.grid : document.getElementById(scope.grid); }
function _sdPresetLabel(cols, id) {
  if (!id) return null;
  if (_sdIsFlow()) { const p = SettingsLib.ROLE_PRESETS[id]; return p ? p.label : null; }
  return SettingsLib.presetLabel(cols, id);
}
// Role-preset tile: the page's visible cards placed at their preset widths.
function _sdRoleTileSvg(cols, presetId, scope) {
  const n = cols === 'auto' ? 3 : Number(cols);
  const grid = _sdGridEl(scope);
  const hero = grid && _sdIsFlow() ? _flowHeroId(grid) : null;
  const sizes = {};
  _sdVisibleCards(scope).slice(0, 8).forEach((c, i) => {
    const id = c.dataset.card;
    sizes[i] = `${SettingsLib.roleWidth(presetId, SettingsLib.roleOf(id), id === hero, n)}x1`;
  });
  return _sdTileSvg(cols, sizes);
}

// Row-major first-fit placement of a preset's first cards, for the tile glyph.
function _sdPlace(cols, sizes, n = 8, rows = 3) {
  const occ = Array.from({ length: rows }, () => Array(cols).fill(false));
  const out = [];
  for (let i = 0; i < n; i++) {
    const [w, h] = String(sizes[i] || '1x1').split('x').map(Number);
    let done = false;
    for (let r = 0; r < rows && !done; r++) {
      for (let c = 0; c <= cols - w && !done; c++) {
        if (r + h > rows) continue;
        let ok = true;
        for (let dr = 0; dr < h; dr++) for (let dc = 0; dc < w; dc++) if (occ[r + dr][c + dc]) ok = false;
        if (!ok) continue;
        for (let dr = 0; dr < h; dr++) for (let dc = 0; dc < w; dc++) occ[r + dr][c + dc] = true;
        out.push([c, r, w, h]); done = true;
      }
    }
  }
  return out;
}
function _sdTileSvg(cols, sizes) {
  const n = cols === 'auto' ? 3 : Number(cols);
  const W = 72, H = 42, g = 3, cw = (W - g * (n - 1)) / n, rh = (H - g * 2) / 3;
  const rects = _sdPlace(n, sizes).map(([c, r, w, h]) =>
    `<rect x="${(c * (cw + g)).toFixed(1)}" y="${(r * (rh + g)).toFixed(1)}" width="${(w * cw + (w - 1) * g).toFixed(1)}" height="${(h * rh + (h - 1) * g).toFixed(1)}" rx="1.5"/>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" aria-hidden="true">${rects}</svg>`;
}

function _sdRenderHeader(scope) {
  scope = scope || _sdScope();
  const sum = document.getElementById('settingsSum');
  if (!sum) return;
  if (scope.kind !== 'cards') { sum.innerHTML = `<b>${_esc(scope.label)}</b> · no cards on this page`; return; }
  const cols = SettingsLib.normalizeCols(layout[scope.cols] || 3);
  const preset = _sdCurrentPreset(scope, cols);
  const pname = _sdPresetLabel(cols, preset);
  const map = _sdCardMap(scope);
  const count = _activeTab === 'overall'
    ? `${(layout.overallBorrowed || []).length} pinned`
    : `${Object.keys(map).filter(id => !_hiddenList(scope.hidden).includes(id)).length} of ${Object.keys(map).length} cards`;
  const eng = _sdIsFlow() ? ' · flow' : '';
  sum.innerHTML = `<b>${_esc(scope.label)}</b> · ${count} · ${cols === 'auto' ? 'auto columns' : cols + ' columns'}${eng} · ${pname ? _esc(pname.toLowerCase()) : 'custom'}`;
}

function _sdCardMap(scope) {
  return { 'dashboard/llamacpp': CARD_LABELS, 'dashboard/lmstudio': CARD_LABELS_LMS,
           'dashboard/vllm': CARD_LABELS_VLLM, 'dashboard/manager': CARD_LABELS_MANAGER }[scope.key] || {};
}

function _sdChip(id, label, on, kind) {
  return `<button type="button" class="sd-chip${on ? ' on' : ''}" data-sd="${kind}" data-id="${_esc(id)}" aria-pressed="${on}">${_esc(label)}</button>`;
}

function _sdRenderCards(scope) {
  scope = scope || _sdScope();
  const el = document.getElementById('sdCards');
  if (!el || scope.kind !== 'cards') return;
  if (_activeTab === 'overall') {
    const borrowed = layout.overallBorrowed || [];
    const groups = [
      ['llama.cpp', CARD_LABELS], ['LM Studio', CARD_LABELS_LMS], ['vLLM', CARD_LABELS_VLLM], ['Manager', CARD_LABELS_MANAGER],
    ];
    const inner = groups.map(([g, map]) => {
      const ids = Object.keys(map);
      const on = ids.filter(id => borrowed.includes(id)).length;
      return `<div class="microlbl">${_esc(g)} <span class="cnt">${on}/${ids.length}</span></div>
        <div class="sd-chips">${ids.map(id => _sdChip(id, _cardLabel(id, map), borrowed.includes(id), 'pin')).join('')}</div>`;
    }).join('');
    el.innerHTML = `
      <div class="sd-sh"><h3>Pinned from other pages</h3><span class="meta">${borrowed.length} pinned</span></div>
      <div class="help" style="margin-top:0;">Pin any Dashboard card here. Pinned cards leave their home page while Overall is open.</div>
      ${inner}
      <div class="help">Drag a card to reorder it. The ⤢ button on a card cycles its size.</div>`;
    return;
  }
  const map = _sdCardMap(scope);
  const hidden = _hiddenList(scope.hidden);
  const ids = Object.keys(map);
  const shown = ids.filter(id => !hidden.includes(id)).length;
  el.innerHTML = `
    <div class="sd-sh"><h3>Cards on this page</h3><span class="meta">${shown} of ${ids.length} shown</span>
      <span class="act"><button type="button" class="lnk" data-sd="show-all">Show all</button><button type="button" class="lnk" data-sd="hide-all">Hide all</button></span></div>
    <div class="sd-chips">${ids.map(id => _sdChip(id, _cardLabel(id, map), !hidden.includes(id), 'card')).join('')}</div>
    <div class="help">Drag a card to reorder it. The ⤢ button on a card cycles its size.</div>`;
}

function _sdRenderLayout(scope) {
  scope = scope || _sdScope();
  const el = document.getElementById('sdLayout');
  if (!el || scope.kind !== 'cards') return;
  const cols = SettingsLib.normalizeCols(layout[scope.cols] || 3);
  const current = _sdCurrentPreset(scope, cols);
  const seg = SettingsLib.COLUMN_OPTIONS.map(c => c === 'auto'
    ? `<span class="sep"></span><button type="button" data-sd="cols" data-v="auto" class="${cols === 'auto' ? 'on' : ''}" title="Fit as many ${SettingsLib.AUTO_MIN_COL_PX}px columns as the window allows">Auto</button>`
    : `<button type="button" data-sd="cols" data-v="${c}" class="${cols === c ? 'on' : ''}">${c}</button>`).join('');
  const flow = _sdIsFlow();
  const customTile = `<div class="sd-tile custom${current ? '' : ' on'}" title="Sizes set per card"><svg viewBox="0 0 72 42" aria-hidden="true"><rect x="0" y="0" width="34" height="42" rx="1.5"/><rect x="37" y="0" width="35" height="19" rx="1.5"/><rect x="37" y="23" width="16" height="19" rx="1.5"/><rect x="56" y="23" width="16" height="19" rx="1.5"/></svg><span class="tn">Custom</span></div>`;
  const tiles = flow
    ? Object.entries(SettingsLib.ROLE_PRESETS).map(([id, p]) =>
        `<button type="button" class="sd-tile${id === current ? ' on' : ''}" data-sd="rpreset" data-id="${id}" title="${_esc(p.label)}">${_sdRoleTileSvg(cols, id, scope)}<span class="tn">${_esc(p.label)}</span></button>`).join('') + customTile
    : SettingsLib.presetsFor(cols).map(([id, p]) =>
        `<button type="button" class="sd-tile${id === current ? ' on' : ''}" data-sd="preset" data-id="${id}" title="${_esc(p.label)}">${_sdTileSvg(cols, p.sizes)}<span class="tn">${_esc(p.label)}</span></button>`).join('') + customTile;
  const pname = _sdPresetLabel(cols, current) || 'custom';
  const engSeg = `<div class="mc-seg" role="group" aria-label="Layout engine">
      <button type="button" data-sd="engine" data-v="grid" class="${flow ? '' : 'on'}" title="Cards stretch to their row">Grid</button>
      <button type="button" data-sd="engine" data-v="flow" class="${flow ? 'on' : ''}" title="Cards are as tall as their content">Flow</button></div>`;
  const presetHelp = flow
    ? 'Presets size cards by what they hold — charts, stat tiles, tables — across the whole page. The ⤢ button on a card still sets its own width.'
    : 'Presets for the selected column count. They size the first cards in your current order; the rest stay 1×1.';
  el.innerHTML = `
    <div class="sd-sh"><h3>Layout</h3><span class="meta">${cols === 'auto' ? 'auto columns' : cols + ' columns'}${flow ? ' · flow' : ''} · ${_esc(String(pname).toLowerCase())}</span>
      <span class="act"><button type="button" class="sd-btn warn" data-sd="reset" title="Restore this page's default cards, order, columns and sizes">⟲ Reset this page</button></span></div>
    <div class="sd-row top"><span class="k">Engine</span><div class="grow">${engSeg}
      <div class="help">${flow ? 'Flow sizes every card to its content and packs cards to close gaps.' : 'Grid stretches each row to its tallest card.'} Applies to every card page.</div></div></div>
    <div class="sd-row"><span class="k">Columns</span><div class="mc-seg" role="group" aria-label="Columns">${seg}</div></div>
    <div class="sd-row top"><span class="k">Preset</span><div class="grow"><div class="sd-tiles">${tiles}</div>
      <div class="help">${presetHelp}</div></div></div>`;
}

// Swatch colours read from the theme's own :root[data-theme] rule so the
// picker can't drift from base.css; the lib's hex list is the fallback.
function _sdSwatchFor(theme) {
  const want = ['--bg', '--bg-card', '--accent', '--accent-2', '--border'];
  try {
    for (const sheet of document.styleSheets) {
      let rules; try { rules = sheet.cssRules; } catch (_) { continue; }
      for (const r of rules) {
        if (!r.selectorText || !r.selectorText.includes(`[data-theme="${theme.id}"]`)) continue;
        const vals = want.map(v => r.style.getPropertyValue(v).trim());
        if (vals.every(Boolean)) return vals;
      }
    }
  } catch (_) {}
  return theme.swatch;
}

function _sdRenderAppearance() {
  const el = document.getElementById('sdAppearance');
  if (!el) return;
  const saved = SettingsLib.normalizeTheme(layout && layout.theme);
  const follow = !!(layout && layout.themeFollowSystem);
  const eff = document.documentElement.getAttribute('data-theme') || saved;
  const sw = SettingsLib.THEMES.map(t => {
    const [p0, p1, p2, p3, pb] = _sdSwatchFor(t);
    return `<button type="button" class="sd-swt${t.id === saved ? ' on' : ''}" data-sd="theme" data-id="${t.id}" style="--p0:${p0};--p1:${p1};--p2:${p2};--p3:${p3};--pb:${pb}" aria-pressed="${t.id === saved}"><span class="pv"></span><span class="tn">${_esc(t.label)}</span></button>`;
  }).join('');
  const light = SettingsLib.THEMES.find(t => t.id === SettingsLib.SYSTEM_LIGHT_THEME);
  const dens = SettingsLib.normalizeDensity(layout && layout.density);
  el.innerHTML = `
    <div class="sd-sh"><h3>Appearance</h3><span class="meta">${_esc(eff)}${follow && eff !== saved ? ' · following system' : ''}</span></div>
    <div class="sd-sw">${sw}</div>
    <div class="sd-row" style="margin-top:12px;"><button type="button" class="mc-toggle${follow ? ' on' : ''}" data-sd="follow-system" role="switch" aria-checked="${follow}"><span class="track"></span><span class="tlbl">Follow the system light/dark setting</span></button></div>
    <div class="help">Uses <b>${_esc(light ? light.label : 'Frost')}</b> while your OS is in light mode and your chosen dark theme otherwise.</div>
    <div class="sd-row" style="margin-top:12px;"><span class="k">Density</span><div class="mc-seg" role="group" aria-label="Card density">
      <button type="button" data-sd="density" data-v="comfortable" class="${dens === 'comfortable' ? 'on' : ''}">Comfortable</button>
      <button type="button" data-sd="density" data-v="compact" class="${dens === 'compact' ? 'on' : ''}">Compact</button></div></div>
    <div class="help">Compact tightens card padding, stat type and chart height on every card page.</div>`;
}

let _intervalManual = null;

function _sdRenderRefresh() {
  const el = document.getElementById('sdRefresh');
  if (!el) return;
  const cfg = window._pollCfg || {};
  const mode = cfg.interval_mode || 'auto';
  if (mode === 'manual' && cfg.interval_override) _intervalManual = cfg.interval_override;
  const manualVal = SettingsLib.clampInterval(_intervalManual || cfg.poll_interval || SettingsLib.INTERVAL_CHIPS[0]);
  const idle = cfg.poll_interval_idle, active = cfg.poll_interval_active;
  const cur = cfg.poll_interval ? `${cfg.poll_interval}s` : '—';
  const reason = cfg.interval_reason && cfg.interval_reason !== 'idle' ? `active · ${cfg.interval_reason}` : 'idle';
  const seg = `<div class="mc-seg" role="group" aria-label="Refresh mode">
      <button type="button" data-sd="mode" data-v="auto" class="${mode === 'auto' ? 'on' : ''}">Auto</button>
      <button type="button" data-sd="mode" data-v="manual" class="${mode === 'manual' ? 'on' : ''}">Manual</button></div>`;
  const autoBox = `
    <div class="sd-live auto"><span class="dot"></span>every <b>${_esc(cur)}</b><span class="why">${_esc(reason)}</span></div>
    <div class="help">Switches between your configured cadences: <b>${active != null ? active + 's' : '—'}</b> while a provider is active, <b>${idle != null ? idle + 's' : '—'}</b> while llama sleeps and LM Studio is idle. Change them under Admin → Settings → Polling. Agents sample every 5s, so that is the fastest useful cadence.</div>`;
  const chips = SettingsLib.INTERVAL_CHIPS.map(v =>
    `<button type="button" class="sd-chip${v === manualVal ? ' on' : ''}" data-sd="ival" data-v="${v}">${v}s</button>`).join('');
  const manBox = `
    <div class="sd-row"><span class="k">Every</span><div class="sd-chips">${chips}</div></div>
    <div class="sd-row"><span class="k">Custom</span>
      <div class="sd-stp"><button type="button" data-sd="ival-step" data-v="-5" aria-label="Slower">−</button><input type="number" id="sdIvalInput" min="${SettingsLib.INTERVAL_MIN}" max="${SettingsLib.INTERVAL_MAX}" step="5" value="${manualVal}" aria-label="Seconds"><button type="button" data-sd="ival-step" data-v="5" aria-label="Faster">+</button></div>
      <span class="unit">seconds · ${SettingsLib.INTERVAL_MIN}–${SettingsLib.INTERVAL_MAX}</span></div>
    <div class="sd-live manual" style="margin-top:8px;"><span class="dot"></span>every <b>${_esc(cur)}</b><span class="why">manual · all viewers</span></div>`;
  el.innerHTML = `
    <div class="sd-sh"><h3>Refresh</h3><span class="meta">poll interval</span></div>
    <div class="sd-row"><span class="k">Mode</span>${seg}</div>
    <div ${mode === 'auto' ? '' : 'hidden'}>${autoBox}</div>
    <div ${mode === 'manual' ? '' : 'hidden'}>${manBox}</div>`;
}

function _renderIntervalBadge() {
  const b = document.getElementById('intervalBadge');
  if (!b) return;
  const cfg = window._pollCfg || {};
  const s = cfg.poll_interval ? `${cfg.poll_interval}s` : '—';
  const mode = cfg.interval_mode || 'auto';
  b.className = 'hdr-badge ' + mode;
  b.innerHTML = `<span class="dot"></span><b>${_esc(s)}</b> ${mode}`;
  b.title = `Refresh every ${s} (${mode}) · open settings`;
}

function renderSettingsPanel() {
  const scope = _sdScope();
  const has = scope.kind === 'cards';
  const none = document.getElementById('sdNoCards');
  const cards = document.getElementById('sdCards');
  const lay = document.getElementById('sdLayout');
  if (none) none.hidden = has;
  if (cards) cards.hidden = !has;
  if (lay) lay.hidden = !has;
  _sdRenderHeader(scope);
  if (has) { _sdRenderCards(scope); _sdRenderLayout(scope); }
  _sdRenderAppearance();
  _sdRenderRefresh();
}

function _sdSetAllCards(visible) {
  const scope = _sdScope();
  if (scope.kind !== 'cards' || _activeTab === 'overall') return;
  Object.keys(_sdCardMap(scope)).forEach(id => toggleCard(id, visible));
}

function _sdRerenderCards() { _sdRenderCards(); _sdRenderHeader(); _sdRenderLayout(); }

let _sdBound = false;
function _sdBind() {
  if (_sdBound) return;
  _sdBound = true;
  const root = document.getElementById('settingsOverlay');
  if (!root) return;
  root.addEventListener('click', (ev) => {
    const t = ev.target.closest('[data-sd]');
    if (!t) return;
    const kind = t.dataset.sd;
    if (kind === 'card') {
      const on = t.getAttribute('aria-pressed') !== 'true';
      toggleCard(t.dataset.id, on); _sdRerenderCards();
    } else if (kind === 'pin') {
      const on = t.getAttribute('aria-pressed') !== 'true';
      if (on) addBorrowedCard(t.dataset.id); else removeBorrowedCard(t.dataset.id);
      _sdRerenderCards();
    } else if (kind === 'show-all' || kind === 'hide-all') {
      _sdSetAllCards(kind === 'show-all'); _sdRerenderCards();
    } else if (kind === 'cols') {
      applyGridCols(t.dataset.v === 'auto' ? 'auto' : Number(t.dataset.v), true);
    } else if (kind === 'preset') {
      applyLayoutPreset(t.dataset.id);
    } else if (kind === 'rpreset') {
      applyRolePreset(t.dataset.id);
    } else if (kind === 'engine') {
      applyLayoutEngine(t.dataset.v, true);
    } else if (kind === 'density') {
      applyDensity(t.dataset.v, true);
    } else if (kind === 'reset') {
      resetCurrentTabLayout();
    } else if (kind === 'theme') {
      applyTheme(t.dataset.id, true);
    } else if (kind === 'follow-system') {
      setThemeFollowSystem(t.getAttribute('aria-checked') !== 'true');
    } else if (kind === 'mode') {
      applyIntervalMode(t.dataset.v, _intervalManual);
    } else if (kind === 'ival') {
      applyIntervalMode('manual', Number(t.dataset.v));
    } else if (kind === 'ival-step') {
      const inp = document.getElementById('sdIvalInput');
      const v = SettingsLib.clampInterval((inp ? Number(inp.value) : SettingsLib.INTERVAL_CHIPS[0]) + Number(t.dataset.v));
      applyIntervalMode('manual', v);
    }
  });
  root.addEventListener('change', (ev) => {
    if (ev.target && ev.target.id === 'sdIvalInput') applyIntervalMode('manual', Number(ev.target.value));
  });
  root.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && ev.target && ev.target.id === 'sdIvalInput') { ev.preventDefault(); ev.target.blur(); }
  });
}

function _sdKey(ev) {
  if (ev.key === 'Escape') closeSettings();
}

function openSettings(section) {
  _sdBind();
  renderSettingsPanel();
  const ov = document.getElementById('settingsOverlay');
  if (!ov) return;
  ov.classList.add('open');
  document.getElementById('settingsCog')?.classList.add('open');
  document.addEventListener('keydown', _sdKey);
  const target = section === 'refresh' ? document.getElementById('sdRefresh') : null;
  if (target) target.scrollIntoView({ block: 'start' });
  else { const body = ov.querySelector('.sd-b'); if (body) body.scrollTop = 0; }
  setTimeout(() => ov.querySelector('.sd')?.focus({ preventScroll: true }), 0);
}
function closeSettings() {
  document.getElementById('settingsOverlay')?.classList.remove('open');
  document.getElementById('settingsCog')?.classList.remove('open');
  document.removeEventListener('keydown', _sdKey);
}

async function applyIntervalMode(mode, value) {
  mode = mode === 'manual' ? 'manual' : 'auto';
  if (mode === 'manual') _intervalManual = SettingsLib.clampInterval(value || _intervalManual || SettingsLib.INTERVAL_CHIPS[0]);
  try {
    await fetch('/api/config/interval', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(mode === 'manual' ? {mode: 'manual', value: _intervalManual} : {mode: 'auto'}),
    });
    await checkConfig();
  } catch(e) {}
}

// checkConfig publishes each /api/config read; the badge always follows it,
// the open drawer's Refresh section only while its interval input is idle.
document.addEventListener('lsm:pollcfg', () => {
  _renderIntervalBadge();
  const ov = document.getElementById('settingsOverlay');
  if (!ov || !ov.classList.contains('open')) return;
  if (document.activeElement && document.activeElement.id === 'sdIvalInput') return;
  _sdRenderRefresh();
});

// Apply saved column counts from layout on load
function applyAllGridCols() {
  Object.values(SettingsLib.CARD_PAGES).forEach(p => {
    const el = document.getElementById(p.grid);
    const n = layout[p.cols];
    if (el && n) el.style.gridTemplateColumns = SettingsLib.gridTemplate(SettingsLib.normalizeCols(n));
  });
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
    map = {};
  } else if (_activeTab === 'dashboard') {
    const sub = _getDashSubTab();
    if (sub === 'lmstudio') {
      scope = { hidden: 'lmsHidden', order: 'lmsOrder', cols: 'lmsCols' };
      map = CARD_LABELS_LMS;
    } else if (sub === 'vllm') {
      scope = { hidden: 'vllmHidden', order: 'vllmOrder', cols: 'vllmCols' };
      map = CARD_LABELS_VLLM;
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
  if (layout.rolePreset) delete layout.rolePreset[_sdScope().key];
  if (scope.borrowed) layout[scope.borrowed] = [];
  // Drop cardSizes entries for ids in this tab's label map; the Overall
  // tab's sizes live under ov-borrow-* shell keys instead.
  if (layout.cardSizes) {
    for (const id of Object.keys(map)) delete layout.cardSizes[id];
    if (_activeTab === 'overall') {
      for (const id of Object.keys(layout.cardSizes)) {
        if (id.startsWith('ov-')) delete layout.cardSizes[id];
      }
    }
  }
  // Per-agent surfaces: also clear the selected agent's own hidden/order/size sets.
  const _resetProv = (window.LMLayout && LMLayout.PER_AGENT_HIDDEN[scope.hidden]) || null;
  const _aid = _resetProv ? _selectedAgent(_resetProv) : null;
  if (_aid) {
    if (layout.hiddenByAgent && layout.hiddenByAgent[_resetProv]) delete layout.hiddenByAgent[_resetProv][_aid];
    if (layout.orderByAgent && layout.orderByAgent[_resetProv]) delete layout.orderByAgent[_resetProv][_aid];
    if (layout.sizesByAgent && layout.sizesByAgent[_resetProv]) delete layout.sizesByAgent[_resetProv][_aid];
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

// switchTab — tab dispatcher (moved here so tab batches can rely on it)
function switchTab(tab) {
  if (tab === 'admin' && window._me && window._me.admin_access === false) { tab = 'overall'; }
  // Leaving Overall: pinned cards go back to their home grids first (#565).
  if (_activeTab === 'overall' && tab !== 'overall'
      && typeof returnPinnedCards === 'function') returnPinnedCards();
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

  // Overall entry re-backfills the fleet TPS chart before the live fetch — it
  // takes no live points while another tab is showing (#506).
  if (tab === 'overall')    {
    document.getElementById('overallTab').style.display = '';
    if (typeof adoptPinnedCards === 'function') adoptPinnedCards();
    _ovBackfillPinnedProviders();
    if (typeof loadOverallHistory === 'function') {
      loadOverallHistory().finally(() => fetchOverallMetrics()).catch(() => {});
    } else {
      fetchOverallMetrics();
    }
  }
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
  else {
    adminStopAutoRefresh();
    // Close the body-level admin overlays: the log panel (with its
    // EventSource) and the self-update panel.
    if (typeof _adminLogsClose === 'function') _adminLogsClose();
    if (typeof _adminUpdateClose === 'function') _adminUpdateClose();
  }
  if (tab !== 'llm')        {
    stopLogStream(); stopPerfRefresh(); stopLmsLogRefresh();
    if (typeof stopVllmLogRefresh === 'function') stopVllmLogRefresh();
    if (typeof rcStopStream === 'function') rcStopStream();
  }
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
