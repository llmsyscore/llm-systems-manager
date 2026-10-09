"""#1238: long-range history reads ask InfluxDB for a bucket size that already
fits the response cap, and the history route builds its response from the
Flux rows without a per-row MetricPoint."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from _influx_stubs import make_client
from backend.api.auth import require_management_token
from backend.api.routes import metrics
from backend.models.metrics import MetricPoint
from backend.storage import repositories as repo_mod
from backend.storage.cache import MetricCache
from backend.storage.repositories import MetricRepository

DAY_S = 86400.0


# ── grain fitting ────────────────────────────────────────────────────────────

def test_fit_every_coarsens_to_the_cap():
    # 24 h, 8 hosts, cap 1500 → 187 per host → 10m (145 buckets) fits, 5m does not.
    assert repo_mod._fit_every("1m", DAY_S, 1500, 8) == "10m"
    # One host: 1m already fits (1441 ≤ 1500).
    assert repo_mod._fit_every("1m", DAY_S, 1500, 1) == "1m"
    # 7 d tier 5m, 8 hosts → 1h (169 buckets) fits.
    assert repo_mod._fit_every("5m", 7 * DAY_S, 1500, 8) == "1h"


def test_fit_every_keeps_the_downsampler_resolution():
    # 6 h, 8 hosts → the downsampler uses 2-minute buckets; 5m would lose
    # resolution, so the 1m tier grain stays and Python finishes the job.
    assert repo_mod.history_step_s(6 * 3600.0, 187) == 120
    assert repo_mod._fit_every("1m", 6 * 3600.0, 1500, 8) == "1m"


def test_fit_every_never_goes_finer_or_past_the_coarsest_grain():
    assert repo_mod._fit_every("30m", 3600.0, 1500, 1) == "30m"
    # 30 d, 8 hosts: nothing fits → coarsest grain, Python finishes the job.
    assert repo_mod._fit_every("30m", 30 * DAY_S, 1500, 8) == "1h"
    assert repo_mod._fit_every(None, DAY_S, 1500, 8) is None
    assert repo_mod._fit_every("1m", DAY_S, 0, 8) == "1m"


# ── cache host count ─────────────────────────────────────────────────────────

def test_cache_counts_hosts_reporting_a_series():
    cache = MetricCache()
    for h in ("a", "b", "c"):
        cache.add_metric_point(MetricPoint(source="system", metric_name="cpu_total",
                                           value=1.0, hostname=h))
    assert cache.metric_host_count("system", "cpu_total") == 3
    assert cache.metric_host_count("system", "nope") == 0


# ── repository rows ──────────────────────────────────────────────────────────

class _Rec:
    def __init__(self, t, v, host):
        self._t, self._v, self.values = t, v, {"unit": "%", "hostname": host}
    def get_time(self):
        return self._t
    def get_value(self):
        return self._v


def _tables(rows):
    return [SimpleNamespace(records=[_Rec(t, v, h) for t, v, h in rows])]


def test_history_rows_fit_the_grain_and_skip_point_objects(monkeypatch):
    t0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    cli, qapi = make_client(results=[_tables([(t0, 1.0, "a"), (t0 + timedelta(minutes=10), 2.0, "b")])])
    cache = MetricCache(metric_ttl_seconds=3600)
    for h in range(8):
        cache.add_metric_point(MetricPoint(source="system", metric_name="cpu_total",
                                           value=1.0, hostname=f"h{h}"))
    repo = MetricRepository(cache=cache, db=cli)
    built = []
    monkeypatch.setattr(repo_mod, "MetricPoint",
                        lambda **kw: built.append(kw) or MetricPoint(**kw))
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = repo.get_history_rows("system", "cpu_total", since=since, limit=1000,
                                 max_points=1500)
    assert "every: 10m" in qapi.queries[0]
    assert built == []
    assert [r["value"] for r in rows] == [1.0, 2.0]
    assert rows[0]["hostname"] == "a" and rows[0]["unit"] == "%"
    assert rows[0]["source"] == "system" and rows[0]["metric_name"] == "cpu_total"
    assert rows[0]["timestamp"] == t0.isoformat()
    assert set(rows[0]) == {"metric_id", "source", "metric_name", "value", "unit",
                            "timestamp", "hostname"}


def test_history_rows_host_filter_means_one_host(monkeypatch):
    cli, qapi = make_client(results=[[]])
    cache = MetricCache(metric_ttl_seconds=3600)
    for h in range(8):
        cache.add_metric_point(MetricPoint(source="system", metric_name="cpu_total",
                                           value=1.0, hostname=f"h{h}"))
    repo = MetricRepository(cache=cache, db=cli)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    repo.get_history_rows("system", "cpu_total", since=since, limit=1000,
                          hostname="h1", max_points=1500)
    # 1m is the rollup grain: served as stored, no aggregateWindow on top.
    assert "aggregateWindow" not in qapi.queries[0]
    assert '_measurement == "metrics_1m"' in qapi.queries[0]


def test_history_rows_skip_non_numeric_values_and_other_hosts():
    t0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    cli, _ = make_client(results=[_tables([
        (t0, None, "a"), (t0, float("nan"), "a"), (t0, 2.0, "b"), (t0, 3.0, "a"),
    ])])
    repo = MetricRepository(cache=MetricCache(metric_ttl_seconds=3600), db=cli)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = repo.get_history_rows("system", "cpu_total", since=since, limit=10,
                                 hostname="a", max_points=1500)
    assert [r["value"] for r in rows] == [3.0]


def test_history_rows_fall_back_to_cache_when_the_db_fails():
    class _Boom:
        def query_metrics(self, *a, **k):
            raise RuntimeError("influx down")

    cache = MetricCache(metric_ttl_seconds=3600)
    p = MetricPoint(source="system", metric_name="cpu_total", value=5.0, hostname="a")
    cache.add_metric_point(p)
    repo = MetricRepository(cache=cache, db=_Boom())
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    assert repo.get_history_rows("system", "cpu_total", since=since, limit=10,
                                 max_points=1500) == [p.to_dict()]


def test_history_rows_without_a_db_serve_the_cache_for_any_window():
    cache = MetricCache(metric_ttl_seconds=3600)
    p = MetricPoint(source="system", metric_name="cpu_total", value=5.0, hostname="a")
    cache.add_metric_point(p)
    repo = MetricRepository(cache=cache, db=None)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    assert repo.get_history_rows("system", "cpu_total", since=since, limit=10,
                                 max_points=1500) == [p.to_dict()]


def test_history_rows_in_window_serve_the_cache():
    cache = MetricCache(metric_ttl_seconds=3600)
    p = MetricPoint(source="system", metric_name="cpu_total", value=3.0, hostname="a")
    cache.add_metric_point(p)
    repo = MetricRepository(cache=cache, db=None)
    since = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert repo.get_history_rows("system", "cpu_total", since=since, limit=10,
                                 max_points=1500) == [p.to_dict()]


def test_get_points_keeps_the_tier_grain():
    cli, qapi = make_client(results=[[]])
    cache = MetricCache(metric_ttl_seconds=3600)
    for h in range(8):
        cache.add_metric_point(MetricPoint(source="system", metric_name="cpu_total",
                                           value=1.0, hostname=f"h{h}"))
    repo = MetricRepository(cache=cache, db=cli)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    repo.get_points("system", "cpu_total", since=since, limit=1000)
    assert "aggregateWindow" not in qapi.queries[0]
    assert '_measurement == "metrics_1m"' in qapi.queries[0]


# ── Flux window labels ───────────────────────────────────────────────────────

def test_aggregated_windows_are_labelled_by_aligned_start():
    t_edge = datetime(2026, 10, 8, 16, 53, 34, tzinfo=timezone.utc)
    t_mid = datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc)
    cli, qapi = make_client(results=[_tables([(t_edge, 1.0, "a"), (t_mid, 2.0, "a")])])
    rows = cli.query_metrics("system", "cpu_total", every="10m")
    assert 'timeSrc: "_start"' in qapi.queries[0]
    assert [r["timestamp"] for r in rows] == [
        datetime(2026, 10, 8, 16, 50, tzinfo=timezone.utc).isoformat(),
        t_mid.isoformat(),
    ]


def test_raw_reads_keep_their_labels():
    t_edge = datetime(2026, 10, 8, 16, 53, 34, tzinfo=timezone.utc)
    cli, qapi = make_client(results=[_tables([(t_edge, 1.0, "a")])])
    rows = cli.query_metrics("system", "cpu_total")
    assert "timeSrc" not in qapi.queries[0]
    assert rows[0]["timestamp"] == t_edge.isoformat()


# ── route ────────────────────────────────────────────────────────────────────

def test_route_reads_rows_not_points():
    calls = {}

    class _Repo:
        cache = None
        def get_history_rows(self, source, metric_name, **kw):
            calls.update(kw)
            return [{"metric_id": "x", "source": source, "metric_name": metric_name,
                     "value": 1.0, "unit": None,
                     "timestamp": "2026-10-08T12:00:00+00:00", "hostname": "a"}]
        def get_points(self, *a, **k):
            raise AssertionError("history route must not build MetricPoints")

    metrics.set_repository(_Repo())
    app = FastAPI()
    app.include_router(metrics.router)
    app.dependency_overrides[require_management_token] = lambda: None
    with TestClient(app) as client:
        r = client.get("/api/alarm/metrics/system/cpu_total",
                       params={"since_minutes": 1440, "max_points": 700, "agg": "max"})
    metrics.set_repository(None)
    assert r.status_code == 200
    assert r.json()[0]["value"] == 1.0
    assert calls["max_points"] == 700 and calls["agg"] == "max"
