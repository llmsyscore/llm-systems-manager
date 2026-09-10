// Renders the host CPU perf-mode note for a tool's run strip from a perf_mode event.
// Empty string whenever the note would be absent or misleading (controller off, governor unknown).
function perfModeNote(ev) {
  if (!ev || ev.enabled === false) return '';
  const gov = ev.governor;
  if (!gov || typeof gov !== 'string') return '';
  return ev.ok === false ? `cpu ${gov} · switch failed` : `cpu ${gov}`;
}

const _PERFMODE_API = { perfModeNote };
if (typeof window !== 'undefined') window.perfModeNote = perfModeNote;
if (typeof module !== 'undefined' && module.exports) module.exports = _PERFMODE_API;
