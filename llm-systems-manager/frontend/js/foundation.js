// ── Multi-agent picker state (PR3) ──────────────────────────────────
// _selectedAgents[provider] = agent_id of the dashboard picker's selection,
// or null. _withAgentParam() appends ?agent=<id> to same-origin provider API
// URLs so the manager dispatcher routes to the picked host. No-op when no
// selection exists (single-agent installs) → byte-identical behavior.
window._agentsByProvider = window._agentsByProvider || { llama: [], lms: [], vllm: [] };
window._selectedAgents   = window._selectedAgents   || { llama: null, lms: null, vllm: null };

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

// Per-browser-tab pause for scheduled pollers and stream frames.
// Usage: LivePause.every(fn, ms) instead of setInterval; `if (LivePause.on) return;` in stream handlers.
const LivePause = (() => {
  const KEY = 'lsm.livePaused';
  let on = false;
  try { on = sessionStorage.getItem(KEY) === '1'; } catch (_) { /* storage blocked */ }
  function set(v) {
    v = !!v;
    if (v === on) return;
    on = v;
    try { if (v) sessionStorage.setItem(KEY, '1'); else sessionStorage.removeItem(KEY); } catch (_) { /* storage blocked */ }
    document.dispatchEvent(new CustomEvent('lsm:livepause', { detail: { paused: v } }));
  }
  function guard(fn) { return function () { if (on) return undefined; return fn.apply(this, arguments); }; }
  function every(fn, ms) { return setInterval(guard(fn), ms); }
  return { get on() { return on; }, set, toggle: () => set(!on), guard, every };
})();
window.LivePause = LivePause;

// Per-action debounce for user-initiated ops (load/unload/reload).
const _actionInflight = new Set();
function _actionClaim(key)   { if (_actionInflight.has(key)) return false; _actionInflight.add(key); return true; }
function _actionRelease(key) { _actionInflight.delete(key); }

let _activeTab = 'overall';   // tracks which top-level tab is visible

// Host page panel ids and titles of the llama.cpp, LM Studio, vLLM and Host pages, filled by
// _hostPanelLabels() from HP.panelsFor once js/lib/host-view.js has loaded.
const CARD_LABELS = {};
const CARD_LABELS_LMS = {};
const CARD_LABELS_VLLM = {};
const CARD_LABELS_HOST = {};
function _hostPanelLabels() {
  if (typeof HP === 'undefined' || Object.keys(CARD_LABELS).length) return;
  [[CARD_LABELS, 'llama'], [CARD_LABELS_LMS, 'lms'], [CARD_LABELS_VLLM, 'vllm'], [CARD_LABELS_HOST, 'host']].forEach(([map, page]) => {
    HP.panelsFor(page).forEach(p => { map[page + '-' + p.name] = p.title; });
  });
}
// Manager page panel ids and titles in page order: js/dashboard-manager.js's rows, then the System row of js/hostpage.js.
const CARD_LABELS_MANAGER = {
  'mgr-tiles':       'Snapshot',
  'mgr-perf':        'Manager latency',
  'mgr-ae-perf':     'Alarm engine latency',
  'mgr-influx-perf': 'InfluxDB latency',
  'mgr-services':    'Services',
  'mgr-db':          'Databases',
  'mgr-streams':     'Connection health',
  'mgr-hosts':       'Host streams',
  'mgr-procs':       'Processes',
  'mgr-tower':       'Tower',
  'mgr-cpu-mem':     'CPU and memory',
  'mgr-cpu-details': 'CPU details',
  'mgr-mem-details': 'Memory details',
  'mgr-net':         'Network',
  'mgr-io':          'Disk activity',
  'mgr-disk':        'Disk space',
};
// Marks a row's only panel whose title matches the row title (case-insensitive) with mc-solo, which shows that
// title to screen readers only (css/base.css); run after kitDress on every kit page.
function mcSoloTitles(root) {
  if (!root || !root.querySelectorAll) return;
  const norm = (t) => String(t || '').trim().toLowerCase();
  root.querySelectorAll('section.row[data-row]').forEach(row => {
    const hd = Array.from(row.children).find(c => c.classList.contains('row-hd'));
    const box = Array.from(row.children).find(c => c.classList.contains('panels'));
    if (!hd || !box) return;
    const name = norm(Array.from(hd.childNodes).filter(n => n.nodeType === 3).map(n => n.nodeValue).join(''));
    const list = Array.from(box.children).filter(p => p.classList.contains('panel'));
    list.forEach(p => {
      const t = p.querySelector('.p-hd > .p-title'), solo = list.length === 1 && !!name && norm(t ? t.textContent : p.getAttribute('data-title')) === name;
      if (p.classList.contains('mc-solo') !== solo) p.classList.toggle('mc-solo', solo);
    });
  });
}
// ---------------------------------------------------------------------------
// Layout persistence
// ---------------------------------------------------------------------------
let layout = {};

async function loadLayout() {
  let bandMapped = false, hostMapped = false, mgrMapped = false;
  _hostPanelLabels();
  try {
    layout = await fetch('/api/layout').then(r => r.json());
    SettingsLib.applyFreshDefaults(layout);
    _migrateLegacyCardIds(layout);
    // Old manager and provider card ids become panel ids once; the layout is saved below when either did.
    mgrMapped = typeof MV !== 'undefined' && MV.mapLegacyLayout(layout).changed;
    hostMapped = typeof HP !== 'undefined' && HP.mapLegacyLayout(layout, Object.keys(CARD_LABELS_MANAGER)).changed;
    if (!layout.hiddenOverall)   layout.hiddenOverall   = [];
    if (!layout.overallBorrowed) layout.overallBorrowed = [];
    if (!layout.overallOrder)    layout.overallOrder    = [];
    if (!layout.cardSizes || typeof layout.cardSizes !== 'object') layout.cardSizes = {};
    if (!layout.rolePreset || typeof layout.rolePreset !== 'object') layout.rolePreset = {};
    // Old Overall band keys become row ids once; the layout is saved below when they did.
    bandMapped = typeof OV !== 'undefined' && OV.mapLegacyBand(layout).changed;
  } catch(e) { console.warn('layout load failed', e); }
  const rawTheme = layout && layout.theme;
  if (rawTheme) layout.theme = SettingsLib.normalizeTheme(rawTheme);
  applyTheme(layout && layout.theme, false);
  applyLayout();
  applyAllGridCols();
  if (typeof applyDensity === 'function') applyDensity(layout && layout.density, false);
  if (typeof applyLayoutEngine === 'function') applyLayoutEngine(layout && layout.layoutEngine, false);
  // Saves once when the stored theme was a legacy or unknown one, or old band or card keys were mapped.
  if ((rawTheme && rawTheme !== layout.theme) || bandMapped || hostMapped || mgrMapped) saveLayout();
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
  // Per-host hidden lists: byAgent[provider][agentId] = [cardId...].
  const eachAgent = (bucket, fn) => {
    if (!bucket || typeof bucket !== 'object') return;
    for (const prov in bucket) {
      const byAgent = bucket[prov];
      if (byAgent && typeof byAgent === 'object') for (const aid in byAgent) fn(byAgent[aid]);
    }
  };
  swap(lay.hidden);
  swap(lay.overallOrder); swap(lay.overallBorrowed); swap(lay.hiddenOverall);
  eachAgent(lay.hiddenByAgent, swap);
  swapKeys(lay.cardSizes);
}

function _cardLabel(id, map) {
  return (map && map[id]) || id;
}

// ── Multi-agent picker (PR3) ────────────────────────────────────────
// Picker chip-rows live in the LLM Control sub-panels (llm-llamacpp/
// llm-lmstudio/llm-vllm). They auto-hide when a provider has ≤1 agent so
// single-host installs see no UI change. Selection persists in layout and is
// restored on load. Maps provider → container element id.
const _AGENT_PICKER_CONTAINERS = {
  llama: ['agentPickerCtrlLlama'],
  lms:   ['agentPickerCtrlLms'],
  vllm:  ['agentPickerCtrlVllm'],
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
  // Re-pull everything for the newly-selected agent.
  if (provider === 'llama') {
    // loadHistory() resets and reseeds this agent's rate peaks (the reset runs
    // before its fetch); live samples resume after it so they land in order (#121).
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
    if (typeof fetchLMStudioMetrics === 'function') fetchLMStudioMetrics();
    if (typeof _lmsLogOpen !== 'undefined' && _lmsLogOpen
        && typeof startLmsLogRefresh === 'function') startLmsLogRefresh();
    if (typeof closeLmsTerminal === 'function') {
      const _lp = document.getElementById('lmsTerminalPanel');
      if ((typeof _lmsTermSid !== 'undefined' && _lmsTermSid)
          || (_lp && _lp.style.display !== 'none')) closeLmsTerminal();
    }
  } else if (provider === 'vllm') {
    if (typeof fetchVllmMetrics === 'function') fetchVllmMetrics();
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
  if (typeof mcShellPaintTheme === 'function') mcShellPaintTheme();
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

// Opens the Events tab with one alert selected, scrolled into view and expanded (#962).
// A booted alarm console takes a postMessage; one that has not loaded yet gets ?alert= on its URL.
function focusAlarmAlert(id) {
  if (id == null || id === '') return;
  const iframe = document.getElementById('alarmEngineIframe');
  const wanted = String(id);
  const booted = !!(iframe && iframe.getAttribute('src'));
  if (iframe && !booted) {
    const theme = (document.documentElement.dataset.theme || '').trim();
    const q = new URLSearchParams();
    if (/^[a-z][a-z0-9-]{0,31}$/.test(theme)) q.set('theme', theme);
    q.set('alert', wanted);
    iframe.setAttribute('src', '/alarm/?' + q.toString());
  }
  if (typeof switchTab === 'function') switchTab('events');
  if (!iframe || !booted) return;
  const post = () => { try { iframe.contentWindow?.postMessage({ type: 'open_alert', id: wanted }, window.location.origin); } catch (_) {} };
  post();
  // A console still loading gets the message once it is up.
  iframe.addEventListener('load', post, { once: true });
  setTimeout(() => iframe.removeEventListener('load', post), 15000);
}

// Opens Events and metrics on Metrics with one host's metric picked; a console not loaded yet gets ?metric= on its URL.
function focusAlarmMetric(host, source, name) {
  if (!source || !name) return;
  const iframe = document.getElementById('alarmEngineIframe');
  const booted = !!(iframe && iframe.getAttribute('src'));
  if (iframe && !booted) {
    const theme = (document.documentElement.dataset.theme || '').trim();
    const q = new URLSearchParams();
    if (/^[a-z][a-z0-9-]{0,31}$/.test(theme)) q.set('theme', theme);
    q.set('metric', (host || '*') + '|' + source + '/' + name);
    iframe.setAttribute('src', '/alarm/?' + q.toString());
  }
  if (typeof switchTab === 'function') switchTab('events');
  if (!iframe || !booted) return;
  const post = () => { try { iframe.contentWindow?.postMessage({ type: 'open_metric', host: host || '', source, name }, window.location.origin); } catch (_) {} };
  post();
  iframe.addEventListener('load', post, { once: true });
  setTimeout(() => iframe.removeEventListener('load', post), 15000);
}

// Keeps the window on the same panel while content under root changes height or width: the first shown panel
// in view is noted on scroll and after each correction, and a size change of root scrolls back by its move.
function keepPlace(root, sel) {
  if (!root || root._keepPlace || typeof ResizeObserver !== 'function') return;
  let at = null, due = false;
  const note = () => {
    at = null;
    if (!(window.scrollY > 0) || !root.clientWidth) return;
    for (const p of root.querySelectorAll(sel)) {
      const r = p.getBoundingClientRect();
      if (!p.classList.contains('full') && r.height && r.bottom > 0) { at = { el: p, top: r.top, w: root.clientWidth }; return; }
    }
  };
  // A scroll while root's width differs from the noted one comes from a resize and keeps the old place.
  const onScroll = () => {
    if (due) return;
    due = true;
    requestAnimationFrame(() => { due = false; if (!(at && at.el.isConnected && at.w !== root.clientWidth)) note(); });
  };
  const ro = new ResizeObserver(() => {
    if (at && at.el.isConnected) {
      const r = at.el.getBoundingClientRect();
      if (r.height && Math.abs(r.top - at.top) >= 1) window.scrollBy(0, r.top - at.top);
    }
    note();
  });
  ro.observe(root);
  window.addEventListener('scroll', onScroll, { passive: true });
  root._keepPlace = { ro, onScroll, note };
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
  // Charts already carry these colours: nothing to redraw.
  const tint = [tick, grid, ttBg, ttTitle, ttBody, ttBorder].join('|');
  if (tint === _chartTint) return;
  _chartTint = tint;
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
      _chartUpdate(c);
    } catch (_) {}
  });
}

// Charts skipped while hidden, waiting for _flushDirtyCharts.
const _dirtyCharts = new Set();
let _chartShownMemo = null;
// Whether a canvas is on screen; each canvas is read once per task.
function _chartShown(cv) {
  if (!_chartShownMemo) { _chartShownMemo = new Map(); queueMicrotask(() => { _chartShownMemo = null; }); }
  let v = _chartShownMemo.get(cv);
  if (v === undefined) { v = cv.offsetParent !== null; _chartShownMemo.set(cv, v); }
  return v;
}
// Redraws a chart whose canvas is on screen; a hidden chart is marked for _flushDirtyCharts.
function _chartUpdate(ch) {
  if (!ch) return;
  if (ch.canvas && !_chartShown(ch.canvas)) { _dirtyCharts.add(ch); return; }
  _dirtyCharts.delete(ch);
  ch.update('none');
}
// Redraws the marked charts that are now on screen; drops charts whose canvas left the page.
function _flushDirtyCharts() {
  _chartShownMemo = null;
  _dirtyCharts.forEach(ch => {
    if (!ch.canvas || !ch.canvas.isConnected) { _dirtyCharts.delete(ch); return; }
    if (ch.canvas.offsetParent === null) return;
    _dirtyCharts.delete(ch);
    try { ch.update('none'); } catch (_) {}
  });
}
// Apply chart defaults once at load before any Chart() is instantiated below.
_themeChartDefaults();
// Chrome colours the charts are built with at load; _retintCharts compares against it.
let _chartTint = ['--fg-muted', '--border-soft', '--bg-card', '--fg', '--fg-muted', '--border'].map(v => cssVar(v)).join('|');

// The hidden-card list saved under a layout key, created empty when missing.
function _hiddenList(hiddenKey) {
  if (window.LMLayout) return window.LMLayout.resolveHiddenList(layout, hiddenKey);
  if (!Array.isArray(layout[hiddenKey])) layout[hiddenKey] = [];
  return layout[hiddenKey];
}

// cardId->size map holding every card's size (the global cardSizes).
function _sizeMapFor(_cardId) {
  if (!layout.cardSizes || typeof layout.cardSizes !== 'object') layout.cardSizes = {};
  return layout.cardSizes;
}

function applyLayout() {
  // Apply visibility — LLM Overall cards
  const hiddenOv = layout.hiddenOverall || [];
  document.querySelectorAll('#overallGrid .card').forEach(c => {
    c.style.display = hiddenOv.includes(c.dataset.card) ? 'none' : '';
  });

  _applyRowsHidden();

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
    if (typeof _ovPinNoneSync === 'function') _ovPinNoneSync();
  }
  if (typeof _flushDirtyCharts === 'function') _flushDirtyCharts();
}

// Serialize layout POSTs so rapid drag-drops can't race. Multiple calls while
// a POST is in flight collapse into a single trailing save of the layout as it is then.
let _layoutInFlight = null;
let _layoutPending  = false;
async function saveLayout() {
  if (_layoutInFlight) { _layoutPending = true; return _layoutInFlight; }
  _layoutInFlight = (async () => {
    try {
      do {
        _layoutPending = false;
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

// True when the card is pinned to Overall and the Pinned row is not hidden.
function _ovPinned(id) {
  const lay = (typeof layout !== 'undefined' && layout) || window.layout;
  return !!(lay && (lay.overallBorrowed || []).includes(id) && !(lay.overallRowsHidden || []).includes('pinned'));
}

function _homeCardEl(id) {
  return document.querySelector(`#mg-root [data-card="${id}"], .hp-root [data-card="${id}"]`);
}

function adoptPinnedCards() {
  const lay = (typeof layout !== 'undefined' && layout) || window.layout;
  // A hidden Pinned row adopts nothing: its cards stay on, or go back to, their home pages.
  if (((lay && lay.overallRowsHidden) || []).includes('pinned')) { returnPinnedCards(); return; }
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
    if (home.hidden) home.hidden = false;
    _ensureSizeBtn(shell);
    _ovAdopted.add(id);
  });
  if (typeof _flushDirtyCharts === 'function') _flushDirtyCharts();
  if (typeof _resizeChartsIn === 'function') _resizeChartsIn(document.getElementById('overallGrid'));
}

function _returnOneAdopted(id) {
  const shell = document.querySelector(`#overallGrid [data-card="ov-borrow-${id}"]`);
  const card = shell && shell.querySelector(`:scope > [data-card="${id}"]`);
  const mark = _ovHomeMarks[id];
  if (card && mark && mark.parentNode) {
    mark.parentNode.replaceChild(card, mark);
    // An old-style card gets its size back; host page and Manager page panels keep the kit's own layout.
    if (!card.closest('.hp-root, .mg-root') && typeof _applyCardSize === 'function') _applyCardSize(card, _sizeMapFor(id)[id] || _defaultCardSize(id));
  } else if (mark && mark.parentNode) mark.remove();
  delete _ovHomeMarks[id];
  _ovAdopted.delete(id);
}

function returnPinnedCards() {
  [..._ovAdopted].forEach(_returnOneAdopted);
}

// A kit page's rows as [id, label] in page order; the label is the row's fold button text without its chevron.
function _kitRows(rootId) {
  return [...document.querySelectorAll(`#${rootId} section.row[data-row]`)].map(r => {
    const b = r.querySelector('button[data-row-fold]');
    const label = b ? [...b.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim() : '';
    return [r.dataset.row, label || r.dataset.row];
  });
}

// Hides the Overall and All hosts rows listed in layout.overallRowsHidden and layout.allHostsRowsHidden
// and refreshes the rows' Move up / Move down state.
function _applyRowsHidden() {
  const lay = (typeof layout !== 'undefined' && layout) || window.layout;
  const hidden = (lay && lay.overallRowsHidden) || [];
  document.querySelectorAll('#overallTab section.row[data-row]').forEach(r => { r.hidden = hidden.includes(r.dataset.row); });
  const tab = document.getElementById('overallTab');
  if (tab && typeof kitRowMenus === 'function') kitRowMenus(tab);
  const ahHidden = (lay && lay.allHostsRowsHidden) || [];
  document.querySelectorAll('#dash-all section.row[data-row]').forEach(r => { r.hidden = ahHidden.includes(r.dataset.row); });
  const ah = document.getElementById('dash-all');
  if (ah && typeof kitRowMenus === 'function') kitRowMenus(ah);
}

// Shows the Pinned row's empty sentence while no card is pinned to Overall.
function _ovPinNoneSync() {
  const lay = (typeof layout !== 'undefined' && layout) || window.layout;
  const el = document.getElementById('ovPinNone');
  if (el) el.hidden = ((lay && lay.overallBorrowed) || []).length > 0;
}

// Shows or hides one Overall row, keeping only ids of rows on the page, and saves the layout.
// On Overall, hiding the Pinned row returns its cards home and showing it adopts them again.
function toggleOverallRow(id, on) {
  const hidden = new Set(layout.overallRowsHidden || []);
  if (on) hidden.delete(id); else hidden.add(id);
  const ids = _kitRows('overallTab').map(r => r[0]);
  layout.overallRowsHidden = [...hidden].filter(x => ids.includes(x));
  _applyRowsHidden();
  if (id === 'pinned' && _activeTab === 'overall' && typeof adoptPinnedCards === 'function') {
    adoptPinnedCards();
    if (on && typeof _relayoutGridCards === 'function') _relayoutGridCards(document.getElementById('overallGrid'));
    if (on && typeof _ovBackfillPinnedProviders === 'function') _ovBackfillPinnedProviders();
  }
  saveLayout();
}

// Shows or hides one All hosts row, keeping only ids of rows on the page, and saves the layout.
function toggleAllHostsRow(id, on) {
  const hidden = new Set(layout.allHostsRowsHidden || []);
  if (on) hidden.delete(id); else hidden.add(id);
  const ids = _kitRows('dash-all').map(r => r[0]);
  layout.allHostsRowsHidden = [...hidden].filter(x => ids.includes(x));
  _applyRowsHidden();
  saveLayout();
}

// Shows or hides one row of the shown host, Energy, Forecast or Manager page, keeping only ids of rows on the page, and saves the layout.
function toggleHostPageRow(id, on) {
  const scope = _sdScope();
  const root = scope.kind === 'rows' && scope.key !== 'dashboard/all' && document.getElementById(scope.root);
  if (!root) return;
  const hidden = new Set(layout[scope.rowsHidden] || []);
  if (on) hidden.delete(id); else hidden.add(id);
  const ids = _kitRows(scope.root).map(r => r[0]);
  layout[scope.rowsHidden] = [...hidden].filter(x => ids.includes(x));
  const row = root.querySelector(`section.row[data-row="${id}"]`);
  if (row) row.hidden = !on;
  if (typeof kitRowMenus === 'function') kitRowMenus(root);
  if (scope.key === 'dashboard/energy') { if (typeof enRefit === 'function') enRefit(document); }
  else if (scope.key === 'dashboard/forecast') { if (typeof fcRefit === 'function') fcRefit(document); }
  else {
    if (scope.key === 'dashboard/manager' && typeof mgRefit === 'function') mgRefit(document);
    if (typeof hpRender === 'function') hpRender();
  }
  saveLayout();
}

// Backfill home-provider chart history + kick the manager one-shots for
// pinned cards, so adopted cards are live even if their home dashboard was
// never visited this session (#565, #506 pattern).
function _ovBackfillPinnedProviders() {
  const lay = (typeof layout !== 'undefined' && layout) || window.layout;
  const b = (lay && lay.overallBorrowed) || [];
  if (!b.length || ((lay && lay.overallRowsHidden) || []).includes('pinned')) return;
  const run = fn => { if (typeof fn === 'function') Promise.resolve(fn()).catch(() => {}); };
  // Host page panels (the Manager page's System row too) are filled by their page renderer, which keeps polling
  // while Overall shows them; the other Manager page panels paint now and again as their fetches land.
  const hpPinned = typeof hpPagePanelIds === 'function' && b.some(id => {
    const m = /^(llama|lms|vllm|host|mgr)-/.exec(id);
    return !!m && hpPagePanelIds(m[1]).includes(id);
  });
  if (hpPinned) run(window.hpPaintPinned);
  if (typeof mgPagePanelIds === 'function' && b.some(id => mgPagePanelIds().includes(id))) {
    run(window.mgPaint);
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
  // A Manager page panel pinned while Overall shows is built, handed over and fetched for at once.
  if (_activeTab === 'overall' && typeof mgPagePanelIds === 'function' && mgPagePanelIds().includes(cardId)
      && typeof _ovBackfillPinnedProviders === 'function') _ovBackfillPinnedProviders();
  // A host page panel pinned while Overall shows starts its page's poll and paints at once.
  const hpPage = (/^(llama|lms|vllm|host|mgr)-/.exec(cardId) || [])[1];
  if (_activeTab === 'overall' && hpPage && typeof hpPagePanelIds === 'function' && hpPagePanelIds(hpPage).includes(cardId)
      && typeof hpPaintPinned === 'function') Promise.resolve(hpPaintPinned()).catch(() => {});
  if (typeof _ovPinNoneSync === 'function') _ovPinNoneSync();
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
  if (typeof _ovPinNoneSync === 'function') _ovPinNoneSync();
  saveLayout();
}

// ---------------------------------------------------------------------------
// Settings panel — compact chips + grid layout selector
// ---------------------------------------------------------------------------

// Host page panel ids of a page and their titles: {page, map}, or null for other ids.
function _hpPanelMap(id) {
  _hostPanelLabels();
  const page = (/^(llama|lms|vllm|host)-/.exec(String(id)) || [])[1];
  const map = { llama: CARD_LABELS, lms: CARD_LABELS_LMS, vllm: CARD_LABELS_VLLM, host: CARD_LABELS_HOST }[page];
  return map && map[id] !== undefined && typeof HP !== 'undefined' ? { page, map } : null;
}

function toggleCard(cardId, visible) {
  const hp = _hpPanelMap(cardId);
  if (hp) {
    const list = _hiddenList(HP.HIDDEN_KEYS[hp.page]), idx = list.indexOf(cardId);
    if (visible) { if (idx !== -1) list.splice(idx, 1); }
    else if (idx === -1) list.push(cardId);
    if (typeof hpRender === 'function') hpRender();
    saveLayout();
  }
}

// ── Settings drawer ─────────────────────────────────────────────────
function _getDashSubTab() {
  // Returns the active Dashboard sub-tab id. Defaults to all.
  if (document.getElementById('dash-all')?.classList.contains('active'))      return 'all';
  if (document.getElementById('dash-llamacpp')?.classList.contains('active')) return 'llamacpp';
  if (document.getElementById('dash-lmstudio')?.classList.contains('active')) return 'lmstudio';
  if (document.getElementById('dash-vllm')?.classList.contains('active'))     return 'vllm';
  if (document.getElementById('dash-host')?.classList.contains('active'))     return 'host';
  if (document.getElementById('dash-manager')?.classList.contains('active'))  return 'manager';
  if (document.getElementById('dash-energy')?.classList.contains('active'))   return 'energy';
  if (document.getElementById('dash-openclaw')?.classList.contains('active')) return 'openclaw';
  if (document.getElementById('dash-forecast')?.classList.contains('active')) return 'forecast';
  return 'all';
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
// Column span per visible card under a role preset (index → width), all cards.
function _sdRoleWidths(cols, presetId, scope, cards) {
  const n = cols === 'auto' ? 3 : Number(cols);
  const grid = _sdGridEl(scope);
  const hero = grid ? _flowHeroId(grid) : null;
  return cards.map(c => SettingsLib.roleWidth(presetId, SettingsLib.roleOf(c.dataset.card), c.dataset.card === hero, n));
}
// Role-preset tile: the page's cards in page order at their preset widths,
// with 3–5 rows so a widened last card still lands inside the glyph.
function _sdRoleTileSvg(cols, widths) {
  const n = cols === 'auto' ? 3 : Number(cols);
  const shown = widths.slice(0, 15);
  const units = shown.reduce((a, w) => a + w, 0);
  const rows = Math.max(3, Math.min(5, Math.ceil(units / n)));
  const sizes = {};
  shown.forEach((w, i) => { sizes[i] = `${w}x1`; });
  return _sdTileSvg(cols, sizes, shown.length, rows);
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
function _sdTileSvg(cols, sizes, count = 8, rows = 3) {
  const n = cols === 'auto' ? 3 : Number(cols);
  const W = 72, H = 42, g = 3, cw = (W - g * (n - 1)) / n, rh = (H - g * (rows - 1)) / rows;
  const rects = _sdPlace(n, sizes, count, rows).map(([c, r, w, h]) =>
    `<rect x="${(c * (cw + g)).toFixed(1)}" y="${(r * (rh + g)).toFixed(1)}" width="${(w * cw + (w - 1) * g).toFixed(1)}" height="${(h * rh + (h - 1) * g).toFixed(1)}" rx="1.5"/>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" aria-hidden="true">${rects}</svg>`;
}

function _sdRenderHeader(scope) {
  scope = scope || _sdScope();
  const sum = document.getElementById('settingsSum');
  if (!sum) return;
  if (scope.kind === 'rows') {
    const off = layout[scope.rowsHidden] || [], rows = _kitRows(scope.root);
    sum.innerHTML = `<b>${_esc(scope.label)}</b> · ${rows.filter(r => !off.includes(r[0])).length} of ${rows.length} sections shown`;
    return;
  }
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
  _hostPanelLabels();
  return { 'dashboard/llamacpp': CARD_LABELS, 'dashboard/lmstudio': CARD_LABELS_LMS,
           'dashboard/vllm': CARD_LABELS_VLLM, 'dashboard/host': CARD_LABELS_HOST }[scope.key] || {};
}

function _sdChip(id, label, on, kind) {
  return `<button type="button" class="sd-chip${on ? ' on' : ''}" data-sd="${kind}" data-id="${_esc(id)}" aria-pressed="${on}">${_esc(label)}</button>`;
}

function _sdRenderCards(scope) {
  scope = scope || _sdScope();
  const el = document.getElementById('sdCards');
  if (el && scope.kind === 'rows') {
    const off = layout[scope.rowsHidden] || [], rows = _kitRows(scope.root);
    el.innerHTML = `
      <div class="sd-sh"><h3>Sections on this page</h3><span class="meta">${rows.filter(r => !off.includes(r[0])).length} of ${rows.length} shown</span>
        <span class="act"><button type="button" class="sd-btn warn" data-sd="reset" title="Restore this page's sections, order and chart defaults">⟲ Reset this page</button></span></div>
      <div class="sd-chips">${rows.map(r => _sdChip(r[0], r[1], !off.includes(r[0]), scope.key === 'dashboard/all' ? 'ahrow' : 'hprow')).join('')}</div>${_sdHostPanels(scope)}`;
    return;
  }
  if (!el || scope.kind !== 'cards') return;
  if (_activeTab === 'overall') {
    _hostPanelLabels();
    const borrowed = layout.overallBorrowed || [];
    const groups = [
      ['llama.cpp', CARD_LABELS], ['LM Studio', CARD_LABELS_LMS], ['vLLM', CARD_LABELS_VLLM], ['Host', CARD_LABELS_HOST],
      ['Manager', CARD_LABELS_MANAGER],
    ];
    const inner = groups.map(([g, map]) => {
      const ids = Object.keys(map);
      const on = ids.filter(id => borrowed.includes(id)).length;
      return `<div class="microlbl">${_esc(g)} <span class="cnt">${on}/${ids.length}</span></div>
        <div class="sd-chips">${ids.map(id => _sdChip(id, _cardLabel(id, map), borrowed.includes(id), 'pin')).join('')}</div>`;
    }).join('');
    const off = layout.overallRowsHidden || [];
    const rows = _kitRows('overallTab');
    el.innerHTML = `
      <div class="sd-sh"><h3>Sections on this page</h3><span class="meta">${rows.filter(r => !off.includes(r[0])).length} of ${rows.length} shown</span></div>
      <div class="sd-chips">${rows.map(r => _sdChip(r[0], r[1], !off.includes(r[0]), 'row')).join('')}</div>
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

// A host page's panel chips: each panel shows or hides on this page; a panel pinned to Overall still shows there.
function _sdHostPanels(scope) {
  const map = scope.panelsHidden ? _sdCardMap(scope) : {}, ids = Object.keys(map);
  if (!ids.length) return '';
  const hidden = layout[scope.panelsHidden] || [], shown = ids.filter(id => !hidden.includes(id)).length;
  return `
      <div class="sd-sh"><h3>Panels on this page</h3><span class="meta">${shown} of ${ids.length} shown</span>
        <span class="act"><button type="button" class="lnk" data-sd="show-all">Show all</button><button type="button" class="lnk" data-sd="hide-all">Hide all</button></span></div>
      <div class="sd-chips">${ids.map(id => _sdChip(id, _cardLabel(id, map), !hidden.includes(id), 'card')).join('')}</div>
      <div class="help">A hidden panel still shows on Overall while it is pinned there.</div>`;
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
  const vis = flow ? _sdVisibleCards(scope) : [];
  const uniformW = flow ? _sdRoleWidths(cols, 'uniform', scope, vis).join(',') : '';
  const tiles = flow
    ? Object.entries(SettingsLib.ROLE_PRESETS).map(([id, p]) => {
        const widths = _sdRoleWidths(cols, id, scope, vis);
        const noop = id !== 'uniform' && widths.join(',') === uniformW;
        const why = noop ? (p.widths.chart ? 'no charts here' : 'no tables here') : '';
        return `<button type="button" class="sd-tile${id === current ? ' on' : ''}${noop ? ' noop' : ''}" data-sd="rpreset" data-id="${id}" title="${_esc(p.label)}"${noop ? ' disabled' : ''}>${_sdRoleTileSvg(cols, widths)}<span class="tn">${_esc(p.label)}</span>${noop ? `<span class="tw">${why}</span>` : ''}</button>`;
      }).join('') + customTile
    : SettingsLib.presetsFor(cols).map(([id, p]) =>
        `<button type="button" class="sd-tile${id === current ? ' on' : ''}" data-sd="preset" data-id="${id}" title="${_esc(p.label)}">${_sdTileSvg(cols, p.sizes)}<span class="tn">${_esc(p.label)}</span></button>`).join('') + customTile;
  const pname = _sdPresetLabel(cols, current) || 'custom';
  const engSeg = `<div class="mc-seg" role="group" aria-label="Layout engine">
      <button type="button" data-sd="engine" data-v="grid" class="${flow ? '' : 'on'}" title="Cards stretch to their row">Grid</button>
      <button type="button" data-sd="engine" data-v="flow" class="${flow ? 'on' : ''}" title="Cards are as tall as their content">Flow</button></div>`;
  const dens = SettingsLib.normalizeDensity(layout && layout.density);
  const densSeg = `<div class="mc-seg" role="group" aria-label="Card density">
      <button type="button" data-sd="density" data-v="comfortable" class="${dens === 'comfortable' ? 'on' : ''}">Comfortable</button>
      <button type="button" data-sd="density" data-v="compact" class="${dens === 'compact' ? 'on' : ''}">Compact</button></div>`;
  const presetHelp = flow
    ? 'A preset widens only cards of its kind — charts, tables and lists, or the page\'s hero card — so on a page without that kind it changes nothing and is greyed out. The ⤢ button on a card still sets its own width.'
    : 'Presets for the selected column count. They size the first cards in your current order; the rest stay 1×1.';
  el.innerHTML = `
    <div class="sd-sh"><h3>Layout</h3><span class="meta">${cols === 'auto' ? 'auto columns' : cols + ' columns'}${flow ? ' · flow' : ''} · ${_esc(String(pname).toLowerCase())}</span>
      <span class="act"><button type="button" class="sd-btn warn" data-sd="reset" title="Restore this page's default cards, order, columns and sizes">⟲ Reset this page</button></span></div>
    <div class="sd-row m-top"><span class="k">Engine</span><div class="grow">${engSeg}
      <div class="help">${flow ? 'Flow sizes every card to its content and packs cards to close gaps.' : 'Grid stretches each row to its tallest card.'} Applies to every card page.</div></div></div>
    <div class="sd-row m-top"><span class="k">Density</span><div class="grow">${densSeg}
      <div class="help">Compact tightens card padding, stat type and chart height. Applies to every card page.</div></div></div>
    <div class="sd-row"><span class="k">Columns</span><div class="mc-seg" role="group" aria-label="Columns">${seg}</div></div>
    <div class="sd-row m-top"><span class="k">Preset</span><div class="grow"><div class="sd-tiles">${tiles}</div>
      <div class="help">${presetHelp}</div></div></div>`;
}

// Swatch colours for a theme's picker tile: [bg, card, accent, accent-2, border]
// from the theme catalog in settingsdrawer.js.
function _sdSwatchFor(theme) {
  return theme.swatch;
}

// Account section (#847): first section in the settings drawer, hidden
// unless the session has a logged-in user.
function _sdRenderAccount() {
  const el = document.getElementById('sdAccount');
  if (!el) return;
  const me = window._me || {};
  el.hidden = !me.authenticated;
  if (el.hidden) { el.innerHTML = ''; return; }
  el.innerHTML = `
    <div class="sd-sh"><h3>Account</h3><span class="meta">${_esc(me.username || '')}${me.role ? ' · ' + _esc(me.role) : ''}</span></div>
    <div class="sd-row"><button type="button" class="sd-btn" data-sd="account-password">Change my password</button>
      <button type="button" class="sd-btn warn" data-sd="account-logout">Log out</button></div>`;
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
  el.innerHTML = `
    <div class="sd-sh"><h3>Appearance</h3><span class="meta">${_esc(eff)}${follow && eff !== saved ? ' · following system' : ''}</span></div>
    <div class="sd-sw">${sw}</div>
    <div class="sd-row" style="margin-top:12px;"><button type="button" class="mc-toggle${follow ? ' on' : ''}" data-sd="follow-system" role="switch" aria-checked="${follow}"><span class="track"></span><span class="tlbl">Follow the system light/dark setting</span></button></div>
    <div class="help">Uses <b>Light</b> while your OS is in light mode and <b>Dark</b> otherwise.</div>
`;
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
  const paused = LivePause.on;
  const seg = `<div class="mc-seg" role="group" aria-label="Refresh mode">
      <button type="button" data-sd="mode" data-v="auto" class="${mode === 'auto' ? 'on' : ''}">Auto</button>
      <button type="button" data-sd="mode" data-v="manual" class="${mode === 'manual' ? 'on' : ''}">Manual</button></div>`;
  const live = (cls, why) => paused
    ? `<div class="sd-live paused"><span class="dot"></span><b>paused</b><span class="why">this tab · every ${_esc(cur)} when resumed</span></div>`
    : `<div class="sd-live ${cls}"><span class="dot"></span>every <b>${_esc(cur)}</b><span class="why">${_esc(why)}</span></div>`;
  const pauseRow = `
    <div class="sd-row" style="margin-top:12px;"><button type="button" class="mc-toggle${paused ? ' on' : ''}" data-sd="pause" role="switch" aria-checked="${paused}"><span class="track"></span><span class="tlbl">Pause live updates in this tab</span></button></div>
    <div class="help">Holds scheduled refreshes and stream updates in this browser tab only; buttons and tab switches still load, and alarm notifications still arrive. Other tabs and viewers keep updating.</div>`;
  const autoBox = `
    ${live('auto', reason)}
    <div class="help">Switches between your configured cadences: <b>${active != null ? active + 's' : '—'}</b> while a provider is active, <b>${idle != null ? idle + 's' : '—'}</b> while llama sleeps and LM Studio is idle. Change them under Admin → Settings → Polling. Agents sample every 5s, so that is the fastest useful cadence.</div>`;
  const chips = SettingsLib.INTERVAL_CHIPS.map(v =>
    `<button type="button" class="sd-chip${v === manualVal ? ' on' : ''}" data-sd="ival" data-v="${v}">${v}s</button>`).join('');
  const manBox = `
    <div class="sd-row"><span class="k">Every</span><div class="sd-chips">${chips}</div></div>
    <div class="sd-row"><span class="k">Custom</span>
      <div class="sd-stp"><button type="button" data-sd="ival-step" data-v="-5" aria-label="Slower">−</button><input type="number" id="sdIvalInput" min="${SettingsLib.INTERVAL_MIN}" max="${SettingsLib.INTERVAL_MAX}" step="5" value="${manualVal}" aria-label="Seconds"><button type="button" data-sd="ival-step" data-v="5" aria-label="Faster">+</button></div>
      <span class="unit">seconds · ${SettingsLib.INTERVAL_MIN}–${SettingsLib.INTERVAL_MAX}</span></div>
    <div style="margin-top:8px;">${live('manual', 'manual · all viewers')}</div>`;
  el.innerHTML = `
    <div class="sd-sh"><h3>Refresh</h3><span class="meta">poll interval</span></div>
    <div class="sd-row"><span class="k">Mode</span>${seg}</div>
    <div ${mode === 'auto' ? '' : 'hidden'}>${autoBox}</div>
    <div ${mode === 'manual' ? '' : 'hidden'}>${manBox}</div>
    ${pauseRow}`;
}

function _renderIntervalBadge() {
  const b = document.getElementById('intervalBadge');
  if (!b) return;
  const cfg = window._pollCfg || {};
  const s = cfg.poll_interval ? `${cfg.poll_interval}s` : '—';
  const mode = cfg.interval_mode || 'auto';
  if (LivePause.on) {
    b.className = 'hdr-badge paused';
    b.innerHTML = '<span class="dot"></span><b>paused</b>';
    b.title = 'Live updates paused in this tab · open settings';
    return;
  }
  b.className = 'hdr-badge ' + mode;
  b.innerHTML = `<span class="dot"></span><b>${_esc(s)}</b> ${mode}`;
  b.title = `Refresh every ${s} (${mode}) · open settings`;
}

function renderSettingsPanel() {
  const scope = _sdScope();
  const has = scope.kind === 'cards', rows = scope.kind === 'rows';
  const none = document.getElementById('sdNoCards');
  const cards = document.getElementById('sdCards');
  const lay = document.getElementById('sdLayout');
  if (none) none.hidden = has || rows;
  if (cards) cards.hidden = !(has || rows);
  if (lay) lay.hidden = !has;
  _sdRenderAccount();
  _sdRenderHeader(scope);
  if (has) { _sdRenderCards(scope); _sdRenderLayout(scope); }
  else if (rows) _sdRenderCards(scope);
  _sdRenderAppearance();
  _sdRenderRefresh();
}

// Shows or hides every card of the page; a host page writes its panel list once, then repaints and saves.
function _sdSetAllCards(visible) {
  const scope = _sdScope();
  if (_activeTab === 'overall') return;
  if (scope.panelsHidden) {
    layout[scope.panelsHidden] = visible ? [] : Object.keys(_sdCardMap(scope));
    if (typeof hpRender === 'function') hpRender();
    saveLayout();
    return;
  }
  if (scope.kind !== 'cards') return;
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
    } else if (kind === 'row') {
      toggleOverallRow(t.dataset.id, t.getAttribute('aria-pressed') !== 'true'); _sdRerenderCards();
    } else if (kind === 'ahrow') {
      toggleAllHostsRow(t.dataset.id, t.getAttribute('aria-pressed') !== 'true'); _sdRerenderCards();
    } else if (kind === 'hprow') {
      toggleHostPageRow(t.dataset.id, t.getAttribute('aria-pressed') !== 'true'); _sdRerenderCards();
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
    } else if (kind === 'account-password') {
      closeSettings(); _accountChangePassword();
    } else if (kind === 'account-logout') {
      window.location.href = '/logout';
    } else if (kind === 'reset') {
      resetCurrentTabLayout();
    } else if (kind === 'theme') {
      applyTheme(t.dataset.id, true);
    } else if (kind === 'follow-system') {
      setThemeFollowSystem(t.getAttribute('aria-checked') !== 'true');
    } else if (kind === 'pause') {
      LivePause.set(t.getAttribute('aria-checked') !== 'true');
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
document.addEventListener('lsm:livepause', () => {
  _renderIntervalBadge();
  const ov = document.getElementById('settingsOverlay');
  if (ov && ov.classList.contains('open')) _sdRenderRefresh();
});

// Apply saved column counts from layout on load
function applyAllGridCols() {
  Object.values(SettingsLib.CARD_PAGES).forEach(p => {
    const el = document.getElementById(p.grid);
    const n = layout[p.cols];
    if (el && n) el.style.gridTemplateColumns = SettingsLib.gridTemplate(SettingsLib.normalizeCols(n));
  });
}

// Resets Dashboards · All hosts only: shows every row, restores the kit row and panel order,
// and brings back the busiest-five chart default; other pages keep their layout.
async function _ahResetLayout() {
  const ok = await _themedConfirm({
    title:        'Reset this page back to default?',
    bodyHtml:     'This shows every section again, restores the section and panel order, and shows the five busiest hosts on each chart. Other pages are unaffected.',
    confirmLabel: 'Reset',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  layout.allHostsRowsHidden = [];
  const root = document.getElementById('dash-all');
  if (root && typeof kitLayoutReset === 'function') kitLayoutReset('allhosts', root, window.mcShellStore);
  _applyRowsHidden();
  if (typeof ahResetChoices === 'function') ahResetChoices();
  saveLayout();
  if (typeof renderSettingsPanel === 'function') renderSettingsPanel();
}

// Resets the shown host page only: shows every row and panel and restores the kit row and panel order;
// other pages keep their layout.
async function _hpResetLayout() {
  const scope = _sdScope();
  const root = scope.kind === 'rows' && document.getElementById(scope.root);
  if (!root) return;
  const ok = await _themedConfirm({
    title:        'Reset this page back to default?',
    bodyHtml:     'This shows every section and panel again and restores the section and panel order. Other pages are unaffected.',
    confirmLabel: 'Reset',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  layout[scope.rowsHidden] = [];
  if (scope.panelsHidden) layout[scope.panelsHidden] = [];
  if (typeof kitLayoutReset === 'function') kitLayoutReset('host-' + root.dataset.hpPage, root, window.mcShellStore);
  root.querySelectorAll('section.row[data-row]').forEach(r => { r.hidden = false; });
  if (typeof hpResetFolds === 'function') hpResetFolds(root.dataset.hpPage);
  if (typeof hpRender === 'function') hpRender();
  saveLayout();
  if (typeof renderSettingsPanel === 'function') renderSettingsPanel();
}

// Resets a kit page of the shown rows scope: shows every row, restores the kit row order, folds and sorts under its
// kit key, repaints and saves; other pages keep their layout.
async function _kitPageReset(kitPage, root, repaint) {
  const scope = _sdScope();
  if (!root || scope.kind !== 'rows') return;
  const ok = await _themedConfirm({
    title:        'Reset this page back to default?',
    bodyHtml:     'This shows every section again and restores the section order. Other pages are unaffected.',
    confirmLabel: 'Reset',
    cancelLabel:  'Cancel',
  });
  if (!ok) return;
  layout[scope.rowsHidden] = [];
  if (typeof kitLayoutReset === 'function') kitLayoutReset(kitPage, root, window.mcShellStore);
  root.querySelectorAll('section.row[data-row]').forEach(r => { r.hidden = false; });
  if (typeof repaint === 'function') repaint();
  saveLayout();
  if (typeof renderSettingsPanel === 'function') renderSettingsPanel();
}

// The Manager page reset's repaint: the System row's kit layout and folds reset too, then both roots repaint.
function _mgResetRepaint() {
  const root = document.getElementById('hp-mgr');
  if (root && typeof kitLayoutReset === 'function') kitLayoutReset('host-mgr', root, window.mcShellStore);
  if (root) root.querySelectorAll('section.row[data-row]').forEach(r => { r.hidden = false; });
  if (typeof hpResetFolds === 'function') hpResetFolds('mgr');
  if (typeof hpRender === 'function') hpRender();
  if (typeof mgPaint === 'function') mgPaint();
  if (typeof mgRefit === 'function') mgRefit(document);
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
    if (sub === 'all') { await _ahResetLayout(); return; }
    if (['llamacpp', 'lmstudio', 'vllm', 'host'].includes(sub)) { await _hpResetLayout(); return; }
    if (sub === 'energy') {
      await _kitPageReset('energy', document.getElementById('en-root'), () => { if (typeof enRefit === 'function') enRefit(document); });
      return;
    }
    if (sub === 'forecast') {
      await _kitPageReset('forecast', document.getElementById('fc-root'), () => { if (typeof fcRefit === 'function') fcRefit(document); });
      return;
    }
    if (sub === 'manager') {
      await _kitPageReset('manager', document.getElementById('mg-root'), _mgResetRepaint);
      return;
    }
    return;
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
  if (_activeTab === 'overall') layout.overallRowsHidden = [];
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
  // The old per-proxy tab names select the Tools sub-tab of the same name (#847).
  let toolsSub = null;
  if (['openclaw', 'llmchat', 'imggen'].includes(tab)) { toolsSub = tab; tab = 'tools'; }
  // Leaving Overall: pinned cards go back to their home grids first (#565).
  if (_activeTab === 'overall' && tab !== 'overall'
      && typeof returnPinnedCards === 'function') returnPinnedCards();
  _activeTab = tab;
  if (typeof mcShellMark === 'function') mcShellMark(tab);
  if (typeof mcShellTitle === 'function') mcShellTitle(tab, _subTabState.dashboard);

  const tabs = ['overallTab','dashboardTab','llmTab','eventsTab','toolsTab','adminTab'];
  tabs.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });

  // Overall entry paints the live panels at once and seeds history and energy
  // alongside them; nothing on it fetches or redraws while hidden (#506, #1140, #1439).
  if (tab === 'overall')    {
    document.getElementById('overallTab').style.display = '';
    if (typeof adoptPinnedCards === 'function') adoptPinnedCards();
    // Sizes the pinned shells now that the tab is visible, before the first paint.
    if (typeof _relayoutGridCards === 'function') _relayoutGridCards(document.getElementById('overallGrid'));
    _ovBackfillPinnedProviders();
    fetchOverallMetrics();
    if (typeof ovLoad === 'function') ovLoad().catch(() => {});
  }
  if (tab === 'dashboard')  { document.getElementById('dashboardTab').style.display  = '';   }
  // All hosts fetches and polls only while Dashboards shows it.
  if (tab === 'dashboard' && typeof _subTabState !== 'undefined' && _subTabState.dashboard === 'all') {
    if (typeof ahEnter === 'function') ahEnter();
  } else if (typeof ahLeave === 'function') ahLeave();
  // Host pages poll only while Dashboards shows them, or while Overall shows one of their pinned panels.
  const hpPage = tab === 'dashboard' && typeof _subTabState !== 'undefined'
    ? { llamacpp: 'llama', lmstudio: 'lms', vllm: 'vllm', host: 'host', manager: 'mgr' }[_subTabState.dashboard] : null;
  if (hpPage) {
    if (typeof hpEnter === 'function') hpEnter(hpPage);
  } else if (typeof hpLeave === 'function') hpLeave();
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
  if (tab === 'tools')      {
    document.getElementById('toolsTab').style.display = '';
    switchSubTab('tools', toolsSub || _subTabState.tools);
  }
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
  if (typeof _flushDirtyCharts === 'function') _flushDirtyCharts();
  document.dispatchEvent(new CustomEvent('lsm:tab', { detail: { tab } }));
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
  // Admin-only controls are hidden for operators; the matching notes show instead (#1208).
  document.querySelectorAll('[data-admin-only]').forEach(el => { el.style.display = isAdmin ? '' : 'none'; });
  document.querySelectorAll('[data-operator-note]').forEach(el => { el.style.display = isAdmin ? 'none' : ''; });
  // Account entries live in the settings drawer and show only for a real
  // logged-in (non-bypass) session.
  _sdRenderAccount();
  if (!isAdmin && _activeTab === 'admin') switchTab('overall');
  // Re-reads Tower state so its header button matches the new role (#924).
  if (typeof towerRefreshState === 'function') towerRefreshState();
}

async function _accountChangePassword() {
  const changed = await _accountPasswordDialog();
  if (changed && _activeTab === 'admin' && typeof adminAuthLoad === 'function') adminAuthLoad();
}

const _ACCOUNT_PW_MIN = 8;

// Two-field change-password dialog; validates inline and stays open on a
// wrong current password or a server failure. Resolves true once changed.
function _accountPasswordDialog() {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.id = 'accountPwDialog';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;'
      + 'display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px);';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:8px;'
      + 'padding:20px 22px;min-width:380px;max-width:520px;color:var(--fg);'
      + 'font-family:system-ui,-apple-system,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.5);';
    const inputCss = 'width:100%;box-sizing:border-box;background:var(--bg-card-alt);color:var(--fg);'
      + 'border:1px solid var(--border);border-radius:5px;padding:8px 10px;font-size:0.9em;';
    const labelCss = 'display:block;font-size:0.82em;color:var(--fg-muted,#9aa);margin:0 0 4px;';
    const hintCss = 'font-size:0.8em;min-height:1.2em;margin:5px 0 10px;';
    box.innerHTML = `
      <div style="font-size:1.05em;font-weight:600;margin-bottom:12px;">Change my password</div>
      <label for="apCur" style="${labelCss}">Current password</label>
      <input id="apCur" type="password" autocomplete="current-password" maxlength="64" placeholder="current password" style="${inputCss}">
      <div id="apCurErr" style="${hintCss}color:var(--crit);"></div>
      <label for="apNew" style="${labelCss}">New password</label>
      <input id="apNew" type="password" autocomplete="new-password" maxlength="64" placeholder="${_ACCOUNT_PW_MIN}+ characters" style="${inputCss}">
      <div id="apHint" style="${hintCss}color:var(--warn);"></div>
      <label class="mc-toggle mc-ckt" style="font-size:0.82em;color:var(--fg-muted,#9aa);margin-bottom:14px;">
        <input type="checkbox" role="switch" id="apShow"><span class="track"></span><span class="tlbl">show passwords</span>
      </label>
      <div style="display:flex;justify-content:flex-end;gap:8px;">
        <button id="apCancel" style="background:var(--bg-card-alt);color:var(--fg);border:1px solid var(--border);border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;">Cancel</button>
        <button id="apSave" disabled style="background:var(--accent);color:#fff;border:1px solid var(--border);border-radius:5px;padding:7px 16px;cursor:pointer;font-size:0.88em;font-weight:500;">Save</button>
      </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    const cur = box.querySelector('#apCur');
    const np = box.querySelector('#apNew');
    const curErr = box.querySelector('#apCurErr');
    const hint = box.querySelector('#apHint');
    const save = box.querySelector('#apSave');
    let busy = false;
    const valid = () => !!cur.value && np.value.length >= _ACCOUNT_PW_MIN;
    const refresh = () => {
      const left = _ACCOUNT_PW_MIN - np.value.length;
      hint.textContent = (np.value && left > 0) ? `${left} more character${left === 1 ? '' : 's'} required.` : '';
      save.disabled = busy || !valid();
      save.style.opacity = save.disabled ? '0.55' : '1';
      save.style.cursor = save.disabled ? 'default' : 'pointer';
    };
    const setBusy = (v) => { busy = v; save.textContent = v ? 'Saving…' : 'Save'; refresh(); };
    const cleanup = (v) => { document.removeEventListener('keydown', keyHandler); overlay.remove(); resolve(v); };
    const cancel = () => { if (!busy) cleanup(false); };
    const submit = async () => {
      if (busy || !valid()) { refresh(); return; }
      setBusy(true);
      let r, d;
      try {
        r = await fetch('/api/account/password', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ current_password: cur.value, new_password: np.value }) });
        d = await r.json().catch(() => ({}));
      } catch (_) {
        setBusy(false);
        _themedToast('request failed — password not changed', { kind: 'err', sticky: true });
        return;
      }
      if (r.ok && d.ok) { _themedToast('Password changed', { kind: 'ok' }); cleanup(true); return; }
      setBusy(false);
      const err = d.error || ('HTTP ' + r.status);
      if (r.status === 403 && d.field === 'current_password') {
        curErr.textContent = err;
        cur.focus(); cur.select();
        return;
      }
      _themedToast(err + ' — password not changed', { kind: 'err', sticky: true });
    };
    const keyHandler = (e) => {
      if (e.key === 'Escape') cancel();
      else if (e.key === 'Enter' && (e.target === cur || e.target === np)) submit();
    };
    document.addEventListener('keydown', keyHandler);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cancel(); });
    for (const ev of ['input', 'change']) {
      cur.addEventListener(ev, () => { curErr.textContent = ''; refresh(); });
      np.addEventListener(ev, refresh);
    }
    box.querySelector('#apShow').addEventListener('change', (e) => {
      cur.type = np.type = e.target.checked ? 'text' : 'password';
    });
    box.querySelector('#apCancel').addEventListener('click', cancel);
    save.addEventListener('click', submit);
    refresh();
    setTimeout(() => cur.focus(), 0);
  });
}
