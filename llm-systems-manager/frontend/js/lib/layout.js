// Pure layout helpers shared by the dashboard script and frontend unit tests.
// Classic <script> in the browser (window.LMLayout), CommonJS under Vitest.

// Hidden-list / order-list key -> provider for the dashboard surfaces whose
// single grid is re-pointed per picker-selected agent (llama.cpp, LM Studio, vLLM).
const PER_AGENT_HIDDEN = { hidden: 'llama', lmsHidden: 'lms', vllmHidden: 'vllm' };
const PER_AGENT_ORDER = { order: 'llama', lmsOrder: 'lms', vllmOrder: 'vllm' };

// Per-agent array bucket for a surface, seeded from the global list.
// Returns the global list when no agent is selected.
function _resolveAgentList(layout, bucketKey, globalKey, provider, agentId) {
  if (!layout || typeof layout !== 'object') return [];
  if (!Array.isArray(layout[globalKey])) layout[globalKey] = [];
  if (!provider || !agentId) return layout[globalKey];
  if (!layout[bucketKey] || typeof layout[bucketKey] !== 'object') layout[bucketKey] = {};
  if (!layout[bucketKey][provider] || typeof layout[bucketKey][provider] !== 'object')
    layout[bucketKey][provider] = {};
  const byAgent = layout[bucketKey][provider];
  if (!Array.isArray(byAgent[agentId])) byAgent[agentId] = layout[globalKey].slice();
  return byAgent[agentId];
}

// Effective hidden-card array for a surface (per selected agent, else global).
function resolveHiddenList(layout, hiddenKey, agentId) {
  const provider = (layout && PER_AGENT_HIDDEN[hiddenKey]) || null;
  return _resolveAgentList(layout, 'hiddenByAgent', hiddenKey, provider, agentId);
}

// Effective card-order array for a surface (per selected agent, else global).
function resolveOrderList(layout, orderKey, agentId) {
  const provider = (layout && PER_AGENT_ORDER[orderKey]) || null;
  return _resolveAgentList(layout, 'orderByAgent', orderKey, provider, agentId);
}

// Effective cardId->size map for a surface: the selected agent's map (seeded
// once from the global cardSizes entries in seedIds), else global cardSizes.
function resolveSizeMap(layout, provider, agentId, seedIds) {
  if (!layout || typeof layout !== 'object') return {};
  if (!layout.cardSizes || typeof layout.cardSizes !== 'object') layout.cardSizes = {};
  if (!provider || !agentId) return layout.cardSizes;
  if (!layout.sizesByAgent || typeof layout.sizesByAgent !== 'object') layout.sizesByAgent = {};
  if (!layout.sizesByAgent[provider] || typeof layout.sizesByAgent[provider] !== 'object')
    layout.sizesByAgent[provider] = {};
  const byAgent = layout.sizesByAgent[provider];
  if (!byAgent[agentId] || typeof byAgent[agentId] !== 'object') {
    const seed = {};
    (seedIds || []).forEach(id => { if (layout.cardSizes[id] != null) seed[id] = layout.cardSizes[id]; });
    byAgent[agentId] = seed;
  }
  return byAgent[agentId];
}

const _LAYOUT_API = { PER_AGENT_HIDDEN, PER_AGENT_ORDER, resolveHiddenList, resolveOrderList, resolveSizeMap };
if (typeof window !== 'undefined') window.LMLayout = _LAYOUT_API;
if (typeof module !== 'undefined' && module.exports) module.exports = _LAYOUT_API;
