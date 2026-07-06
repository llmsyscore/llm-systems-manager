"""#249: the InfluxDB self-monitor write probe must detect real write outages.

The metrics firehose is a batched/async write_api that swallows outage errors
in its background flush, so the probe uses a synchronous write. These tests
prove the probe observes failure and that write_ok_rate reflects it.
"""
import asyncio

from backend.models.metrics import MetricPoint
from backend.storage import influx_monitor as im


class FakeCache:
    def __init__(self):
        self.points = []

    def add_metric_point(self, p):
        self.points.append(p)


class FakeDB:
    def __init__(self, fail_sync=False):
        self.url = "http://influx.test"
        self.fail_sync = fail_sync
        self.sync_writes = 0
        self.batched_writes = 0
        # accessed as args to the (patched) _cardinality probe
        self.metrics_bucket = "metrics"
        self._metrics_query = None

    def write_metric(self, rec):
        self.batched_writes += 1  # batched path never raises on outage

    def write_metric_sync(self, rec):
        self.sync_writes += 1
        if self.fail_sync:
            raise RuntimeError("influx write refused (token revoked)")


class FakeRepo:
    """Mirrors MetricRepository.create: cache first, then DB (sync may raise)."""
    def __init__(self, db):
        self.db = db
        self.cache = FakeCache()

    def create(self, point: MetricPoint, sync: bool = False):
        self.cache.add_metric_point(point)
        if self.db is not None:
            if sync:
                self.db.write_metric_sync({})
            else:
                self.db.write_metric({})
        return point

    def emitted(self, name):
        return [p.value for p in self.cache.points if p.metric_name == name]


def test_write_probe_true_on_success():
    repo = FakeRepo(FakeDB(fail_sync=False))
    assert im._write_probe(repo, "h") is True
    assert repo.db.sync_writes == 1
    assert [p.metric_name for p in repo.cache.points] == ["selfwrite_probe"]


def test_write_probe_false_on_outage():
    repo = FakeRepo(FakeDB(fail_sync=True))
    assert im._write_probe(repo, "h") is False
    # cache still got the point (cache write precedes the raising DB write)
    assert repo.cache.points[0].metric_name == "selfwrite_probe"


def _patch_probes(monkeypatch, ping=(True, 1.0)):
    monkeypatch.setattr(im, "_ping", lambda url: ping)
    monkeypatch.setattr(im, "_query_latency_ms", lambda db: 1.0)
    monkeypatch.setattr(im, "_cardinality", lambda *a, **k: 1)
    monkeypatch.setattr(im, "_bytes_on_disk", lambda: 1)


class _StopLoop(Exception):
    pass


def _run_n_cycles(monkeypatch, db, n, ping=(True, 1.0)):
    """Drive run() for n cycles, then break out of the loop from sleep."""
    _patch_probes(monkeypatch, ping=ping)
    repo = FakeRepo(db)
    calls = {"n": 0}

    async def fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] >= n:
            raise _StopLoop()

    monkeypatch.setattr(im.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(im.run(db, repo, interval_s=1, initial_delay_s=0))
    except _StopLoop:
        # expected: fake_sleep raises this to end the loop after n cycles
        pass
    return repo


def test_healthy_writes_report_rate_1(monkeypatch):
    repo = _run_n_cycles(monkeypatch, FakeDB(fail_sync=False), n=3)
    assert repo.emitted("write_ok_rate")[-1] == 1.0
    assert repo.emitted("write_errors_consecutive")[-1] == 0.0
    assert repo.db.sync_writes >= 2


def test_outage_drops_rate_and_counts_errors(monkeypatch):
    repo = _run_n_cycles(monkeypatch, FakeDB(fail_sync=True), n=3)
    rates = repo.emitted("write_ok_rate")
    errs = repo.emitted("write_errors_consecutive")
    # every probe failed → windowed rate is 0.0 and consecutive errors climb
    assert rates[-1] == 0.0
    assert errs[-1] >= 2.0
    assert errs == sorted(errs)  # monotonically increasing during the outage
    # write_ms is only emitted on a successful probe — none here
    assert repo.emitted("write_ms") == []


def test_influx_down_records_write_failure(monkeypatch):
    # ping fails (InfluxDB fully down): the write-health series must reflect
    # the outage, not freeze — and no sync write is attempted while down.
    db = FakeDB(fail_sync=True)
    repo = _run_n_cycles(monkeypatch, db, n=3, ping=(False, -1.0))
    assert repo.emitted("up")[-1] == 0.0
    assert repo.emitted("write_ok_rate")[-1] == 0.0
    assert repo.emitted("write_errors_consecutive")[-1] >= 2.0
    assert db.sync_writes == 0
