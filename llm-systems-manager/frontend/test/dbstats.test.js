// Database Performance card: manager SQLite tiles derive from /api/admin/dbstats/sqlite (#1036, #1037).
import { describe, test, expect } from 'vitest';
import DashboardManagerDb from '../js/lib/dbstats.js';

const { summarize } = DashboardManagerDb;

describe('DashboardManagerDb.summarize', () => {
  test('flattens the three files into tile values', () => {
    const s = summarize({
      ok: true,
      manager_db: { size_bytes: 1560576, wal_size_bytes: 0, query_ms: 0.01, benchmarks: 12, report_cards: 3, tool_runs: 40 },
      audit_db: { size_bytes: 4640768, wal_size_bytes: 4713312, query_ms: 0.05, audit_rows: 19750 },
      energy_db: { size_bytes: 4096, wal_size_bytes: 1153632, query_ms: 0.02, energy_rows: 6432 },
    });
    expect(s).toEqual({
      manager_size: 1560576, audit_size: 4640768, energy_size: 4096,
      wal_total: 0 + 4713312 + 1153632, audit_rows: 19750, energy_rows: 6432,
      run_rows: 55, query_ms: 0.05,
    });
  });

  test('missing or errored files leave their tiles empty', () => {
    const s = summarize({ ok: true, manager_db: { file: 'manager.db', error: 'locked' },
                          audit_db: { size_bytes: 10, audit_rows: 1 } });
    expect(s.manager_size).toBeNull();
    expect(s.energy_rows).toBeNull();
    expect(s.run_rows).toBeNull();
    expect(s.wal_total).toBeNull();
    expect(s.audit_rows).toBe(1);
    expect(s.query_ms).toBeNull();
  });

  test('tolerates a non-object payload (fetch failure or 403)', () => {
    for (const bad of [undefined, null, 'x', { ok: false, error: 'admin role required' }]) {
      expect(summarize(bad)).toEqual({ manager_size: null, audit_size: null, energy_size: null,
        wal_total: null, audit_rows: null, energy_rows: null, run_rows: null, query_ms: null });
    }
  });
});
