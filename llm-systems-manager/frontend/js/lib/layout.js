// Pure layout helpers shared by the dashboard script and frontend unit tests.
// Classic <script> in the browser (window.LMLayout), CommonJS under Vitest.

// The hidden-card array saved under a layout key, created empty when missing or not an array.
function resolveHiddenList(layout, hiddenKey) {
  if (!layout || typeof layout !== 'object') return [];
  if (!Array.isArray(layout[hiddenKey])) layout[hiddenKey] = [];
  return layout[hiddenKey];
}

const _LAYOUT_API = { resolveHiddenList };
if (typeof window !== 'undefined') window.LMLayout = _LAYOUT_API;
if (typeof module !== 'undefined' && module.exports) module.exports = _LAYOUT_API;
