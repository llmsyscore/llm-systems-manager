"""#535 regression: limited window reads must return the NEWEST rows.

The Flux built by query_metrics must tail-limit (sort desc -> limit ->
re-sort asc), not head-limit an ascending sort.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.storage import influxdb_client as ic


def _built_query(monkeypatch):
    captured = {}

    class _QApi:
        def query(self, q, org=None):
            captured["q"] = q
            return []

    cli = ic.InfluxDBClient.__new__(ic.InfluxDBClient)
    cli.metrics_bucket = "b"
    cli.org = "o"
    cli._metrics_query = _QApi()
    cli._rollup_query = _QApi()
    cli._rollup_enabled = False
    cli._rollup_measurement = "metrics_1m"
    cli._rollup_read_bucket = "rb"
    cli._rollup_every = "1m"
    cli.query_metrics("system", "cpu_total", limit=3)
    return captured["q"]


def test_limit_takes_the_tail_not_the_head(monkeypatch):
    q = _built_query(monkeypatch)
    # Ordered pipeline: desc sort, then limit, then ascending re-sort.
    desc = q.index('sort(columns: ["_time"], desc: true)')
    lim = q.index("limit(n: 3)")
    asc = q.index('sort(columns: ["_time"], desc: false)')
    assert desc < lim < asc


def test_no_ascending_sort_before_limit(monkeypatch):
    q = _built_query(monkeypatch)
    before_limit = q[: q.index("limit(n: 3)")]
    assert 'desc: false' not in before_limit
