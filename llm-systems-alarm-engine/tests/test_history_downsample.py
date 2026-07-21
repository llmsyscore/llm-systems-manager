"""#460: history responses are downsampled server-side to max_points so large
windows stop serializing 4k+ raw points per request. Raw remains available
via max_points=0; small responses pass through byte-identical."""
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import metrics
from backend.models.metrics import MetricPoint
from backend.storage.cache import MetricCache
from backend.storage.repositories import MetricRepository

T0 = datetime(2026, 7, 21, 0, 0, 0, tzinfo=timezone.utc)


def _mk(host: str, offset_s: float, value: float) -> MetricPoint:
    return MetricPoint(source="system", metric_name="cpu_total", value=value,
                       unit="%", hostname=host,
                       timestamp=T0 + timedelta(seconds=offset_s))


def _series(hosts: int, seconds: int, cadence_s: int = 5) -> list[MetricPoint]:
    pts = []
    for h in range(hosts):
        for i in range(0, seconds, cadence_s):
            pts.append(_mk(f"host{h}", i, float(i % 100)))
    pts.sort(key=lambda p: p.timestamp)
    return pts


# ── pure helper ──────────────────────────────────────────────────────────────

def test_under_cap_is_passthrough():
    pts = _series(hosts=2, seconds=300)
    assert metrics.downsample_history(pts, 1500) == [p.to_dict() for p in pts]


def test_zero_cap_disables():
    pts = _series(hosts=7, seconds=3600)
    assert metrics.downsample_history(pts, 0) == [p.to_dict() for p in pts]


def test_over_cap_shrinks_below_cap():
    pts = _series(hosts=7, seconds=3600)  # 5040 raw points
    out = metrics.downsample_history(pts, 1500)
    assert len(out) <= 1500
    assert len(out) > 0


def test_all_hosts_survive_downsampling():
    pts = _series(hosts=7, seconds=3600)
    out = metrics.downsample_history(pts, 1500)
    assert {p["hostname"] for p in out} == {f"host{h}" for h in range(7)}


def test_bucket_values_are_means():
    # 1 host, 20 points over 100s, cap 5 -> 30s ladder step; bucket [0,30)
    # holds offsets 0..25 with value == offset.
    pts = [_mk("h", i, float(i)) for i in range(0, 100, 5)]
    out = metrics.downsample_history(pts, 5)
    assert len(out) <= 5
    expected = sum(range(0, 30, 5)) / 6.0
    assert abs(out[0]["value"] - expected) < 1e-9


def test_timestamps_epoch_aligned_and_sorted():
    pts = _series(hosts=7, seconds=3600)
    out = metrics.downsample_history(pts, 1500)
    ladder = {5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600}
    steps = {int(datetime.fromisoformat(p["timestamp"]).timestamp()) for p in out}
    # all bucket starts share one grid: differences divisible by some ladder step
    diffs = sorted({b - a for a, b in zip(sorted(steps), sorted(steps)[1:])})
    assert diffs, "expected multiple buckets"
    assert any(all(d % s == 0 for d in diffs) for s in ladder)
    ts_list = [p["timestamp"] for p in out]
    assert ts_list == sorted(ts_list)


def test_metadata_preserved():
    pts = _series(hosts=2, seconds=3600)
    out = metrics.downsample_history(pts, 100)
    for p in out:
        assert p["source"] == "system"
        assert p["metric_name"] == "cpu_total"
        assert p["unit"] == "%"


# ── route integration ────────────────────────────────────────────────────────

def _client_with_seeded_repo(n_hosts=7, seconds=3000):
    # Seed relative to now so the route's `now - since_minutes` window and
    # the cache TTL both cover every point.
    start = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    pts = []
    for h in range(n_hosts):
        for i in range(0, seconds, 5):
            pts.append(MetricPoint(source="system", metric_name="cpu_total",
                                   value=float(i % 100), unit="%",
                                   hostname=f"host{h}",
                                   timestamp=start + timedelta(seconds=i)))
    pts.sort(key=lambda p: p.timestamp)
    cache = MetricCache()
    cache.add_metric_points(pts)
    metrics.set_repository(MetricRepository(cache=cache, db=None))
    app = FastAPI()
    app.include_router(metrics.router)
    return TestClient(app)


def test_route_caps_by_default():
    client = _client_with_seeded_repo()
    r = client.get("/api/alarm/metrics/system/cpu_total",
                   params={"since_minutes": 60})
    assert r.status_code == 200
    body = r.json()
    assert 0 < len(body) <= metrics._HIST_MAX_POINTS_DEFAULT
    metrics.set_repository(None)


def test_route_max_points_zero_returns_raw():
    client = _client_with_seeded_repo()
    r = client.get("/api/alarm/metrics/system/cpu_total",
                   params={"since_minutes": 60, "max_points": 0})
    assert r.status_code == 200
    assert len(r.json()) > metrics._HIST_MAX_POINTS_DEFAULT
    metrics.set_repository(None)


def test_route_custom_max_points():
    client = _client_with_seeded_repo()
    r = client.get("/api/alarm/metrics/system/cpu_total",
                   params={"since_minutes": 60, "max_points": 100})
    assert r.status_code == 200
    assert 0 < len(r.json()) <= 100
    metrics.set_repository(None)
