"""#1230: the per-metric host cap is a setting (default 256) and hitting it
is logged once per metric, not on every ingest."""
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.models.metrics import MetricPoint
from backend.storage.cache import Cache, MetricCache

_AE_SRC = (Path(__file__).resolve().parent.parent / "backend" / "alarm_engine.py").read_text()


def _now():
    return datetime.now(timezone.utc)


def _points(host: str, n: int, start: datetime) -> list[MetricPoint]:
    return [MetricPoint(source="cpu", metric_name="usage_percent", value=float(i),
                        hostname=host, timestamp=start + timedelta(seconds=i))
            for i in range(n)]


def _fill(cache: MetricCache, hosts: int) -> None:
    start = _now() - timedelta(minutes=30)
    for n in range(hosts):
        cache.add_metric_points(_points(f"host-{n}", 3, start + timedelta(seconds=n * 10)))


def test_default_cap_is_256():
    assert MetricCache().max_hosts_per_series == 256
    assert MetricCache._MAX_HOSTS_PER_SERIES == 256


def test_cap_is_configurable_and_evicts_stalest():
    cache = MetricCache(max_hosts_per_series=3)
    _fill(cache, 4)
    assert cache.get_metric_points("cpu", "usage_percent", hostname="host-0") == []
    for h in ("host-1", "host-2", "host-3"):
        assert len(cache.get_metric_points("cpu", "usage_percent", hostname=h)) == 3


def test_cap_warning_logged_once_per_series(caplog):
    cache = MetricCache(max_hosts_per_series=3)
    with caplog.at_level(logging.DEBUG, logger="backend.storage.cache"):
        _fill(cache, 9)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "hosts" in r.getMessage()]
    assert len(warns) == 1
    assert "3" in warns[0].getMessage()
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "evicted" in r.getMessage()]
    assert len(debugs) == 5


def test_cache_alias_passes_cap_through():
    assert Cache(max_hosts_per_series=7).max_hosts_per_series == 7


def test_setting_exists_with_default_256():
    from config.unified_config import AlarmEngineCaches
    assert AlarmEngineCaches().max_hosts_per_metric == 256


def test_alarm_engine_wires_setting_into_cache():
    assert re.search(r"Cache\(\s*max_hosts_per_series=settings\.alarm_engine\.caches\.max_hosts_per_metric", _AE_SRC)
