"""#602: host-scoped reads carry the hostname filter inside the Flux query;
the repository forwards hostname to the DB layer."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _influx_stubs import make_client
from backend.storage.cache import MetricCache
from backend.storage.repositories import MetricRepository


def test_hostname_filter_in_raw_query():
    cli, qapi = make_client(rollup_enabled=False)
    cli.query_metrics("system", "cpu_total", hostname="host7")
    assert 'r.hostname == "host7"' in qapi.queries[0]


def test_hostname_filter_in_rollup_query():
    cli, qapi = make_client()
    cli.query_metrics("system", "cpu_total", every="5m", hostname="host7")
    q = qapi.queries[0]
    assert '_measurement == "metrics_1m"' in q
    assert 'r.hostname == "host7"' in q


def test_no_hostname_filter_by_default():
    cli, qapi = make_client(rollup_enabled=False)
    cli.query_metrics("system", "cpu_total")
    assert "r.hostname" not in qapi.queries[0]


def test_hostname_is_flux_escaped():
    cli, qapi = make_client(rollup_enabled=False)
    cli.query_metrics("system", "cpu_total", hostname='h"x')
    assert 'r.hostname == "h\\"x"' in qapi.queries[0]


def test_fallback_recursion_keeps_hostname():
    # QApi returns no rows, so the empty-rollup fallback re-queries raw.
    cli, qapi = make_client()
    cli.query_metrics("system", "cpu_total", every="5m", hostname="host7")
    assert len(qapi.queries) == 2
    assert 'r.hostname == "host7"' in qapi.queries[1]
    assert '_measurement == "metrics"' in qapi.queries[1]


def test_get_points_pushes_hostname_to_db():
    captured = {}

    class _Db:
        def query_metrics(self, source, metric_name, **kw):
            captured.update(kw)
            return [{"timestamp": "2026-08-21T00:00:00+00:00", "value": 1.0,
                     "unit": "%", "hostname": "host7"}]

    repo = MetricRepository(cache=MetricCache(), db=_Db())
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    pts = repo.get_points("system", "cpu_total", since=since, hostname="host7")
    assert captured.get("hostname") == "host7"
    assert [p.hostname for p in pts] == ["host7"]
