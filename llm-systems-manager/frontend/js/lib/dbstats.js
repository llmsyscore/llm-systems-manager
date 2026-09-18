// Manager SQLite stats for the Database Performance card (#1036, #1037).
// Dual-mode: window.DashboardManagerDb in the browser, module export under Node.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.DashboardManagerDb = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

const num = v => (typeof v === 'number' && Number.isFinite(v)) ? v : null;
const sumIfAny = (...vs) => {
  const nums = vs.map(num).filter(v => v != null);
  return nums.length ? nums.reduce((x, y) => x + y, 0) : null;
};

// Flatten /api/admin/dbstats/sqlite into the eight tile values; null = "—".
function summarize(payload) {
  const p = payload && typeof payload === 'object' ? payload : {};
  const m = p.manager_db || {}, a = p.audit_db || {}, e = p.energy_db || {};
  const qs = [m.query_ms, a.query_ms, e.query_ms].map(num).filter(v => v != null);
  return {
    manager_size: num(m.size_bytes),
    audit_size: num(a.size_bytes),
    energy_size: num(e.size_bytes),
    wal_total: sumIfAny(m.wal_size_bytes, a.wal_size_bytes, e.wal_size_bytes),
    audit_rows: num(a.audit_rows),
    energy_rows: num(e.energy_rows),
    run_rows: sumIfAny(m.benchmarks, m.report_cards, m.tool_runs),
    query_ms: qs.length ? Math.max(...qs) : null,
  };
}

return { summarize };
});
