// ---------------------------------------------------------------------------
// DOM structure fix — move elements to correct tabs on load
// ---------------------------------------------------------------------------
// Close bench dropdowns when clicking outside
document.addEventListener('click', e => {
  if (!e.target.closest('.bench-dropdown')) {
    document.querySelectorAll('.bench-drop-panel.open').forEach(p => p.classList.remove('open'));
  }
});

document.addEventListener('DOMContentLoaded', () => {
  // Load stored benchmark data so model-card badges render on first paint
  loadBenchmarkData().then(() => {
    if (typeof renderModelCards === 'function') {
      try { renderModelCards(); } catch (_) {}
    }
  });

  // Move cardGrid (dashboard cards) into dash-llamacpp
  const dashPanel  = document.getElementById('dash-llamacpp');
  const cardGrid   = document.getElementById('cardGrid');
  if (dashPanel && cardGrid) dashPanel.appendChild(cardGrid);

  // Move llm-ctrl-wrap (LLM control content) into llm-llamacpp
  const llmPanel   = document.getElementById('llm-llamacpp');
  const llmCtrl    = document.getElementById('llm-ctrl-wrap');
  if (llmPanel && llmCtrl) {
    // Move all children of llm-ctrl-wrap into llm-llamacpp
    while (llmCtrl.firstChild) llmPanel.appendChild(llmCtrl.firstChild);
    llmCtrl.remove();
  }

  // openclawTab and llmchatTab are nested inside dashboardTab in the HTML.
  // Move them to body level so they aren't hidden when dashboardTab hides.
  ['openclawTab', 'llmchatTab', 'imggenTab', 'eventsTab', 'adminTab'].forEach(id => {
    const el = document.getElementById(id);
    if (el && el.closest('#dashboardTab')) document.body.appendChild(el);
  });
});

// ---------------------------------------------------------------------------
// Sub-tab switching
// ---------------------------------------------------------------------------
const _subTabState = { dashboard: 'llamacpp', llm: 'llamacpp', admin: 'agents' };

// Latched true once the manager perf sparklines have real history, so a failed
// or empty boot-time backfill retries on manager-tab entry (#131).
let _mgrPerfBackfilled = false;

const _SUB_TAB_MAP = {
  dashboard: { tabId: 'dashboardTab', prefix: 'dash',  subs: ['llamacpp','lmstudio','openclaw','manager'] },
  llm:       { tabId: 'llmTab',       prefix: 'llm',   subs: ['llamacpp','lmstudio'] },
  admin:     { tabId: 'adminTab',     prefix: 'admin', subs: ['agents','pool','backup','auth','users'] },
};

function switchSubTab(parent, sub) {
  const cfg = _SUB_TAB_MAP[parent];
  if (!cfg) return;
  const tab = document.getElementById(cfg.tabId);
  const nav = tab && tab.querySelector('.sub-tab-nav');
  if (nav) nav.querySelectorAll('.sub-tab-btn').forEach(b => {
    b.classList.toggle('active', b.onclick?.toString().includes(`'${sub}'`));
  });

  cfg.subs.forEach(s => {
    const el = document.getElementById(`${cfg.prefix}-${s}`);
    if (el) el.classList.toggle('active', s === sub);
  });

  _subTabState[parent] = sub;

  // Stop the llama log SSE/retry + the 5s model-card poll whenever the LLM
  // tab's sub-tab is not llama.cpp — otherwise they keep hitting /llama/log
  // and /api/llm/models from the LM Studio sub-tab (#115/#120).
  if (parent === 'llm' && sub !== 'llamacpp') {
    stopLogStream();
    stopPerfRefresh();
  }
  // Conversely, when returning to llama.cpp, restart the model-card poll and
  // the log stream (if its panel was open) — matches the top-level tab switch.
  if (parent === 'llm' && sub === 'llamacpp') {
    if (_logPanelOpen) startLogStream();
    startPerfRefresh();
  }

  // Stop the LMS log timer when the new sub-tab isn't LM Studio, so it doesn't
  // keep polling /api/lmstudio/server/log from a non-LMS view (#115).
  if (sub !== 'lmstudio') {
    stopLmsLogRefresh();
  }
  // Trigger LM Studio fetch when switching to lmstudio sub-tab
  if (sub === 'lmstudio') {
    fetchLMStudioMetrics();
    _initLMSSections();
    // Always restart log polling on every visit (timer is stopped on tab leave)
    if (_lmsLogOpen) startLmsLogRefresh();
  }
  if (parent === 'dashboard' && sub === 'openclaw') {
    fetchOpenclawAnalytics();
  }
  // Manager sub-tab is poll-gated to skip when not active; kick a one-shot
  // refresh on entry so cards aren't stale up to the 10s interval boundary.
  if (parent === 'dashboard' && sub === 'manager') {
    fetchServicesAndInflux();
    fetchManagerAgentsCard();
    fetchManagerStreamsCard();
    // Retry the perf-sparkline backfill on entry until it lands; the boot-time
    // shot can run before the alarm engine has history (#131).
    if (!_mgrPerfBackfilled && typeof loadManagerPerfHistory === 'function') {
      loadManagerPerfHistory().then(ok => { if (ok) _mgrPerfBackfilled = true; });
    }
  }
  if (parent === 'admin' && sub === 'users') {
    if (typeof adminUsersLoad === 'function') adminUsersLoad();
  }
}

// Boot
// ---------------------------------------------------------------------------
(async () => {
  await loadLayout();
  if (typeof loadMe === 'function') await loadMe();
  // Populate the multi-agent picker (chips auto-hide at ≤1 agent). Awaited so
  // the restored/default selection is set BEFORE loadHistory backfills the
  // selected agent's host history. Refreshed on the 30s tick below so
  // newly-approved/online agents appear without a reload.
  if (typeof _loadAgentsByProvider === 'function') await _loadAgentsByProvider();
  initSortable();
  // Sync the live poll interval before any backfill so history and live
  // appends bucket to the same resolution grid (#129).
  if (typeof syncInterval === 'function') await syncInterval();
  await loadHistory();
  // Independent backfills — fired async so a slow alarm-engine response doesn't
  // serialize startup. Each fails silently if its host isn't injected yet; the
  // manager perf shot latches _mgrPerfBackfilled so a miss retries on entry (#131).
  loadLmsHistory().catch(() => {});
  loadManagerPerfHistory().then(ok => { if (ok) _mgrPerfBackfilled = true; }).catch(() => {});
  await checkConfig();
  pollServerState();
  fetchMetrics();
  startFetching(fetchInterval);
  // /api/config drives the interval badge and tab visibility, both of which
  // change only on operator action (agent approve/remove, manual interval
  // override). pollServerState already calls checkConfig on every llama
  // state transition, so the periodic poll is only here to catch agent
  // approval. 60s lag on that is fine.
  setInterval(checkConfig, 60000);
  // Default to LLM Overall when its button is visible; otherwise the
  // fresh-install path (no llama/lms agent yet) lands on Dashboard so
  // the operator isn't dropped onto an empty panel.
  {
    const overallBtn = document.getElementById('tabBtnOverall');
    const overallHidden = overallBtn && overallBtn.style.display === 'none';
    switchTab(overallHidden ? 'dashboard' : 'overall');
  }
  // Poll LM Studio metrics every 6 seconds
  fetchLMStudioMetrics();
  setInterval(fetchLMStudioMetrics, 6000);
  // Services + InfluxDB cards poll the alarm engine catalog every 10s.
  fetchServicesAndInflux();
  setInterval(fetchServicesAndInflux, 10000);
  fetchManagerAgentsCard();
  setInterval(fetchManagerAgentsCard, 10000);
  fetchManagerStreamsCard();
  setInterval(fetchManagerStreamsCard, 10000);
  // Tab status dots (Events / Admin) update regardless of the active tab.
  refreshTabIndicators();
  setInterval(refreshTabIndicators, 30000);
  // Fetch alarm-rule threshold lines now, then every 30s.
  if (typeof refreshAlarmRules === 'function') {
    refreshAlarmRules();
    setInterval(refreshAlarmRules, 30000);
  }
  // Refresh the agent picker list (online state + newly-approved agents).
  if (typeof _loadAgentsByProvider === 'function') {
    setInterval(_loadAgentsByProvider, 30000);
  }
  // Release the manager worker threads held by long-lived SSE streams while
  // this tab is backgrounded; resume polls + reopen the streams on re-focus.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (typeof stopLogStream === 'function') stopLogStream();
      if (typeof _stopLlamaStateStream === 'function') _stopLlamaStateStream();
      return;
    }
    pollServerState();
    fetchLMStudioMetrics();
    fetchServicesAndInflux();
    fetchManagerAgentsCard();
    refreshTabIndicators();
    checkConfig();
    if (typeof _startLlamaStateStream === 'function') _startLlamaStateStream();
    if (_logPanelOpen && _activeTab === 'llm' && _subTabState && _subTabState.llm === 'llamacpp'
        && typeof startLogStream === 'function') startLogStream();
  });
})();
