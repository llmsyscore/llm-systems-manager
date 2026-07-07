"""Per-host metric-cache retention: N hosts sharing one (source,
metric_name) series each keep a full capped per-host buffer, bounded by
_MAX_HOSTS_PER_SERIES distinct hosts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.models.metrics import MetricPoint
from backend.storage.cache import MetricCache


def _points(host, n, start, step_s=1.0, source="cpu", name="usage_percent"):
    return [
        MetricPoint(
            source=source,
            metric_name=name,
            value=float(i),
            timestamp=start + timedelta(seconds=i * step_s),
            hostname=host,
        )
        for i in range(n)
    ]


def _now():
    return datetime.now(timezone.utc)


def test_per_host_cap_no_cross_host_starvation():
    cache = MetricCache()
    cap = MetricCache._MAX_POINTS_PER_SERIES
    start = _now() - timedelta(minutes=30)
    hosts = ["host-a", "host-b", "host-c"]
    # Each host pushes a full cap's worth into the SAME series.
    for h in hosts:
        cache.add_metric_points(_points(h, cap, start, step_s=0.4))

    for h in hosts:
        pts = cache.get_metric_points(
            "cpu", "usage_percent", hostname=h, limit=cap * 2
        )
        assert len(pts) == cap, f"{h} lost points to another host's ingest"
        assert all(p.hostname == h for p in pts)


def test_merged_read_is_timestamp_sorted():
    cache = MetricCache()
    start = _now() - timedelta(minutes=10)
    # Interleaved timestamps across hosts, ingested per-host (out of order
    # relative to each other).
    cache.add_metric_points(_points("host-b", 50, start + timedelta(seconds=0.5)))
    cache.add_metric_points(_points("host-a", 50, start))

    pts = cache.get_metric_points("cpu", "usage_percent", limit=1000)
    assert len(pts) == 100
    ts = [p.timestamp for p in pts]
    assert ts == sorted(ts)


def test_hostname_filter_and_since_window():
    cache = MetricCache()
    start = _now() - timedelta(minutes=10)
    cache.add_metric_points(_points("host-a", 60, start, step_s=10))
    cache.add_metric_points(_points("host-b", 60, start, step_s=10))

    since = start + timedelta(seconds=300)
    pts = cache.get_metric_points(
        "cpu", "usage_percent", since=since, hostname="host-a", limit=1000
    )
    assert pts
    assert all(p.hostname == "host-a" for p in pts)
    assert all(p.timestamp >= since for p in pts)


def test_metric_keys_format_unchanged():
    # api/routes/metrics.py parses keys as "source:metric_name" — hostname
    # must not leak into the key.
    cache = MetricCache()
    cache.add_metric_points(_points("host-a", 3, _now()))
    cache.add_metric_points(_points("host-b", 3, _now()))
    assert cache.metric_keys == ["cpu:usage_percent"]


def test_hostless_points_still_stored_and_readable():
    cache = MetricCache()
    cache.add_metric_points(_points(None, 5, _now()))
    pts = cache.get_metric_points("cpu", "usage_percent", limit=10)
    assert len(pts) == 5


def test_summary_spans_all_hosts():
    cache = MetricCache()
    start = _now() - timedelta(minutes=5)
    cache.add_metric_points(_points("host-a", 10, start))
    cache.add_metric_points(_points("host-b", 10, start + timedelta(seconds=0.5)))
    summary = cache.get_metric_summary("cpu", "usage_percent", window_minutes=60)
    assert summary is not None
    assert summary.data_points == 20


def test_sweep_prunes_idle_host_but_keeps_live_ones():
    cache = MetricCache(metric_ttl_seconds=3600)
    stale_start = _now() - timedelta(hours=3)
    cache.add_metric_points(_points("host-old", 5, stale_start))
    cache.add_metric_points(_points("host-live", 5, _now()))

    removed = cache.sweep_metric_points()
    # Series still has a live host: not removed, stale host pruned.
    assert removed == 0
    assert cache.get_metric_points("cpu", "usage_percent", hostname="host-old") == []
    assert len(cache.get_metric_points("cpu", "usage_percent", hostname="host-live")) == 5


def test_host_cardinality_capped_evicts_stalest():
    cache = MetricCache()
    cap = MetricCache._MAX_HOSTS_PER_SERIES
    start = _now() - timedelta(minutes=30)
    # host-0 is the stalest (oldest newest-point); the rest are fresher.
    for n in range(cap):
        cache.add_metric_points(
            _points(f"host-{n}", 5, start + timedelta(seconds=n * 10))
        )
    cache.add_metric_points(_points("host-new", 5, _now()))

    assert cache.get_metric_points("cpu", "usage_percent", hostname="host-0") == []
    assert len(cache.get_metric_points("cpu", "usage_percent", hostname="host-new")) == 5
    assert len(cache.get_metric_points("cpu", "usage_percent", hostname="host-1")) == 5


def test_limit_one_returns_global_newest():
    cache = MetricCache()
    start = _now() - timedelta(minutes=5)
    cache.add_metric_points(_points("host-a", 10, start))
    cache.add_metric_points(_points("host-b", 5, start + timedelta(seconds=60)))
    pts = cache.get_metric_points("cpu", "usage_percent", limit=1)
    assert len(pts) == 1
    assert pts[0].hostname == "host-b"
    assert pts[0].value == 4.0


def test_sweep_removes_fully_idle_series():
    cache = MetricCache(metric_ttl_seconds=3600)
    stale_start = _now() - timedelta(hours=3)
    cache.add_metric_points(_points("host-a", 5, stale_start))
    cache.add_metric_points(_points("host-b", 5, stale_start))
    removed = cache.sweep_metric_points()
    assert removed == 1
    assert cache.metric_keys == []
