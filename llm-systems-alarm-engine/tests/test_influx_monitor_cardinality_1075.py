"""#1075: the self-monitor's cardinality probe must not scan a day of points every cycle."""
import asyncio

import pytest

from backend.storage import influx_monitor as im


class _Rec:
    def __init__(self, v):
        self._v = v

    def get_value(self):
        return self._v


class _Table:
    def __init__(self, v):
        self.records = [_Rec(v)]


class _Unauthorized(Exception):
    status = 401


class FakeQuery:
    def __init__(self, primary=None, fallback=None):
        self.primary, self.fallback, self.calls = primary, fallback, []

    def query(self, flux, org=None):
        kind = "primary" if "influxdb.cardinality" in flux else "fallback"
        self.calls.append(kind)
        out = self.primary if kind == "primary" else self.fallback
        if isinstance(out, Exception):
            raise out
        return [] if out is None else [_Table(out)]


class FakeDB:
    org = "o"
    metrics_bucket = "metrics"


@pytest.fixture(autouse=True)
def _reset():
    im._CARD_CACHE.clear(); im._cardinality_fallback.clear(); im._cardinality_warned.clear()
    yield
    im._CARD_CACHE.clear(); im._cardinality_fallback.clear(); im._cardinality_warned.clear()


def test_primary_query_reads_the_index_not_points():
    flux = im._cardinality_flux("metrics")
    assert "influxdb.cardinality(" in flux and 'import "influxdata/influxdb"' in flux
    assert "map(" not in flux and "last()" not in flux


def test_fallback_never_maps_before_last():
    flux = im._cardinality_fallback_flux("metrics")
    assert "map(" not in flux
    assert flux.index("filter(") < flux.index("last()") < flux.index("count()")


def test_bucket_name_is_escaped():
    assert 'bucket: "a\\"b"' in im._cardinality_flux('a"b')
    assert 'from(bucket: "a\\"b")' in im._cardinality_fallback_flux('a"b')


def test_result_is_cached_for_the_ttl():
    q = FakeQuery(primary=13210)
    assert im._cardinality(FakeDB, "metrics", q, now=1000.0) == 13210
    assert im._cardinality(FakeDB, "metrics", q, now=1000.0 + im._CARDINALITY_TTL_S - 1) == 13210
    assert q.calls == ["primary"]
    assert im._cardinality(FakeDB, "metrics", q, now=1000.0 + im._CARDINALITY_TTL_S + 1) == 13210
    assert q.calls == ["primary", "primary"]
    assert im._CARD_CACHE["metrics"]["query_ms"] >= 0


def test_refused_primary_falls_back_and_stays_on_the_fallback():
    q = FakeQuery(primary=_Unauthorized("401 Unauthorized"), fallback=999)
    assert im._cardinality(FakeDB, "metrics", q, now=0.0) == 999
    assert im._cardinality(FakeDB, "metrics", q, now=im._CARDINALITY_TTL_S + 1) == 999
    assert q.calls == ["primary", "fallback", "fallback"]


def test_other_primary_failure_falls_back_without_sticking():
    q = FakeQuery(primary=RuntimeError("undefined identifier"), fallback=42)
    assert im._cardinality(FakeDB, "metrics", q, now=0.0) == 42
    assert "metrics" not in im._cardinality_fallback


def test_both_refused_reports_zero_and_warns_once(caplog):
    q = FakeQuery(primary=_Unauthorized("401"), fallback=_Unauthorized("401"))
    with caplog.at_level("WARNING"):
        assert im._cardinality(FakeDB, "metrics", q, now=0.0) == 0
        assert im._cardinality(FakeDB, "metrics", q, now=im._CARDINALITY_TTL_S + 1) == 0
    assert caplog.text.count("returned 401") == 1


def test_transport_failure_returns_none_and_is_not_cached():
    q = FakeQuery(primary=RuntimeError("boom"), fallback=RuntimeError("timeout"))
    assert im._cardinality(FakeDB, "metrics", q, now=0.0) is None
    assert "metrics" not in im._CARD_CACHE


def test_empty_bucket_counts_zero():
    assert im._cardinality(FakeDB, "metrics", FakeQuery(primary=None), now=0.0) == 0


def test_loop_runs_the_probe_off_the_event_loop(monkeypatch):
    import threading
    seen = {}

    class Repo:
        def create(self, point, sync=False):
            seen.setdefault("metrics", []).append(point.metric_name)

    class DB(FakeDB):
        url = "http://influx.test"
        _metrics_query = None

    def card(db, bucket, api, now=None):
        seen["thread"] = threading.current_thread() is threading.main_thread()
        im._CARD_CACHE[bucket] = {"value": 7, "at": 0.0, "query_ms": 12.5}
        return 7

    monkeypatch.setattr(im, "_ping", lambda url: (True, 1.0))
    monkeypatch.setattr(im, "_query_latency_ms", lambda db: 1.0)
    monkeypatch.setattr(im, "_cardinality", card)
    monkeypatch.setattr(im, "_bytes_on_disk", lambda: 1)

    async def go():
        task = asyncio.create_task(im.run(DB(), Repo(), interval_s=3600, initial_delay_s=0))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "bytes_on_disk" in seen.get("metrics", []):
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert seen["thread"] is False
    assert {"cardinality_metrics", "cardinality_query_ms", "bytes_on_disk"} <= set(seen["metrics"])
