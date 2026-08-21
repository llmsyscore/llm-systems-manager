"""#596: parallel max rollup — agg=max routes reads to the max measurement
(or fn:max raw scans) and provisioning creates both tasks plus a one-shot
backfill when the max task is first created."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _influx_stubs import QApi as _QApi, make_client as _client
from backend.storage import influxdb_client as ic


# ── read path ────────────────────────────────────────────────────────────────

def test_agg_max_reads_max_measurement():
    cli, qapi = _client()
    cli.query_metrics("llama", "tokens_per_second", every="5m", agg="max")
    q = qapi.queries[0]
    assert '_measurement == "metrics_1m_max"' in q
    assert "fn: max" in q


def test_agg_default_stays_on_mean_measurement():
    cli, qapi = _client()
    cli.query_metrics("llama", "tokens_per_second", every="5m")
    q = qapi.queries[0]
    assert '_measurement == "metrics_1m"' in q
    assert "fn: mean" in q


def test_agg_max_rollup_grain_skips_reaggregation():
    cli, qapi = _client()
    cli.query_metrics("llama", "tokens_per_second", every="1m", agg="max")
    q = qapi.queries[0]
    assert '_measurement == "metrics_1m_max"' in q
    assert "aggregateWindow" not in q


def test_agg_max_raw_path_uses_fn_max():
    cli, qapi = _client(rollup_enabled=False)
    cli.query_metrics("llama", "tokens_per_second", every="5m", agg="max")
    q = qapi.queries[0]
    assert '_measurement == "metrics"' in q
    assert "fn: max" in q


def test_disallowed_agg_falls_back_to_mean():
    cli, qapi = _client()
    cli.query_metrics("llama", "tokens_per_second", every="5m", agg="p99")
    assert "fn: mean" in qapi.queries[0]


def test_empty_max_rollup_falls_back_to_raw_with_max():
    cli, qapi = _client()
    cli.query_metrics("llama", "tokens_per_second", every="5m", agg="max")
    assert len(qapi.queries) == 2
    assert '_measurement == "metrics_1m_max"' in qapi.queries[0]
    assert '_measurement == "metrics"' in qapi.queries[1]
    assert "fn: max" in qapi.queries[1]
    assert cli._rollup_enabled is True


# ── provisioning ─────────────────────────────────────────────────────────────

class _Task:
    def __init__(self, name):
        self.name = name
        self.id = "tid-" + name
        self.status = "active"
        self.every = "1m"


class _TasksApi:
    def __init__(self, existing=()):
        self.existing = {n: _Task(n) for n in existing}
        self.created = []

    def find_tasks(self, name=None):
        return [self.existing[name]] if name in self.existing else []

    def create_task(self, task_create_request=None):
        self.created.append(task_create_request)
        return _Task("created")


def test_ensure_one_task_flux_per_aggregate():
    cli, _ = _client()
    api = _TasksApi()
    out = cli._ensure_one_rollup_task(api, "max", "metrics_1m_max")
    assert out.get("created") is True
    flux = api.created[0].flux
    assert "fn: max" in flux
    assert 'value: "metrics_1m_max"' in flux
    assert 'name: "metrics_1m_max_rollup_v2"' in flux
    assert 'from(bucket: "raw_b")' in flux
    assert 'to(bucket: "roll_b")' in flux


def test_ensure_one_task_existing_is_untouched():
    cli, _ = _client()
    api = _TasksApi(existing=["metrics_1m_rollup_v2"])
    out = cli._ensure_one_rollup_task(api, "mean", "metrics_1m")
    assert "created" not in out
    assert api.created == []


def test_ensure_rollup_task_provisions_both_and_backfills(monkeypatch):
    cli, _ = _client()
    calls = []
    monkeypatch.setattr(cli, "_ensure_one_rollup_task",
                        lambda api, fn, m: calls.append((fn, m)) or
                        ({"created": True} if fn == "max" else {}))
    backfilled = []
    monkeypatch.setattr(cli, "_start_max_backfill",
                        lambda: backfilled.append(True))
    monkeypatch.setattr(ic, "_InfluxDBClient",
                        lambda **kw: type("C", (), {
                            "tasks_api": lambda self: None,
                            "close": lambda self: None})())
    out = cli.ensure_rollup_task()
    assert calls == [("mean", "metrics_1m"), ("max", "metrics_1m_max")]
    assert backfilled == [True]
    assert set(out) == {"mean", "max"}


def test_ensure_rollup_task_isolates_per_task_failure(monkeypatch):
    cli, _ = _client()

    def _ensure(api, fn, m):
        if fn == "mean":
            raise RuntimeError("boom")
        return {"created": True}

    monkeypatch.setattr(cli, "_ensure_one_rollup_task", _ensure)
    backfilled = []
    monkeypatch.setattr(cli, "_start_max_backfill",
                        lambda: backfilled.append(True))
    monkeypatch.setattr(ic, "_InfluxDBClient",
                        lambda **kw: type("C", (), {
                            "tasks_api": lambda self: None,
                            "close": lambda self: None})())
    out = cli.ensure_rollup_task()
    assert "error" in out["mean"]
    assert backfilled == [True]


def test_ensure_rollup_task_no_backfill_when_max_exists(monkeypatch):
    cli, _ = _client()
    monkeypatch.setattr(cli, "_ensure_one_rollup_task", lambda api, fn, m: {})
    backfilled = []
    monkeypatch.setattr(cli, "_start_max_backfill",
                        lambda: backfilled.append(True))
    monkeypatch.setattr(ic, "_InfluxDBClient",
                        lambda **kw: type("C", (), {
                            "tasks_api": lambda self: None,
                            "close": lambda self: None})())
    cli.ensure_rollup_task()
    assert backfilled == []


def test_backfill_writes_day_chunks_with_max(monkeypatch):
    cli, _ = _client()
    qapi = _QApi()

    class _FakeClient:
        def __init__(self, **kw):
            pass

        def query_api(self):
            return qapi

        def close(self):
            pass

    monkeypatch.setattr(ic, "_InfluxDBClient", _FakeClient)
    cli._backfill_max_rollup(days=3)
    assert len(qapi.queries) == 3
    for q in qapi.queries:
        assert "fn: max" in q
        assert 'value: "metrics_1m_max"' in q
        assert 'to(bucket: "roll_b")' in q
