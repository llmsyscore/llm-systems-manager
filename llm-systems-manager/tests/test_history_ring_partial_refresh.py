"""#1239: a refresh in which some field fetches failed keeps the prior ring's
rows for those fields instead of blanking them."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import manager_mod as M


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _fake_fetch_factory(results: dict):
    def _fake(base, source, name, field, since, limit, hostname=None):
        return field, results.get(field, [])
    return _fake


def _prime_ring(monkeypatch, rows):
    monkeypatch.setattr(M, "_history_rows", list(rows))


def test_fetch_reports_failure_as_none(monkeypatch):
    class _Boom:
        def get(self, *a, **k):
            raise ConnectionError("refused")
    monkeypatch.setattr(M, "_ae_session", _Boom())
    assert M._fetch_history_series("http://ae", "s", "m", "cpu_total", 60, 10) == ("cpu_total", None)

    class _R503:
        status_code = 503
    class _S503:
        def get(self, *a, **k):
            return _R503()
    monkeypatch.setattr(M, "_ae_session", _S503())
    assert M._fetch_history_series("http://ae", "s", "m", "cpu_total", 60, 10) == ("cpu_total", None)

    class _R404:
        status_code = 404
    class _S404:
        def get(self, *a, **k):
            return _R404()
    monkeypatch.setattr(M, "_ae_session", _S404())
    assert M._fetch_history_series("http://ae", "s", "m", "cpu_total", 60, 10) == ("cpu_total", [])


def test_build_rows_collects_failed_fields(monkeypatch):
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae")
    ts = _iso(30)
    monkeypatch.setattr(M, "_fetch_history_series", _fake_fetch_factory({
        "cpu_total": None,
        "ram_percent": [{"timestamp": ts, "value": 50.0, "hostname": "h1"}],
    }))
    failed: set[str] = set()
    rows = M._build_history_rows(60, 100, failed=failed)
    assert failed == {"cpu_total"}
    assert rows == [{"ts": ts, "ram_percent": 50.0}]


def test_tick_carries_forward_rows_for_failed_fields(monkeypatch):
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae")
    old_ts, new_ts, stale_ts = _iso(600), _iso(10), _iso(M.HISTORY_WINDOW_MINUTES * 60 + 120)
    _prime_ring(monkeypatch, [
        {"ts": stale_ts, "cpu_total": 1.0, "ram_percent": 1.0},
        {"ts": old_ts, "cpu_total": 42.0, "ram_percent": 40.0},
    ])
    monkeypatch.setattr(M, "_fetch_history_series", _fake_fetch_factory({
        "cpu_total": None,
        "ram_percent": [
            {"timestamp": old_ts, "value": 41.0, "hostname": "h1"},
            {"timestamp": new_ts, "value": 45.0, "hostname": "h1"},
        ],
    }))
    rows, failed = M._history_refresh_tick()
    assert failed == {"cpu_total"}
    assert rows == [
        {"ts": old_ts, "ram_percent": 41.0, "cpu_total": 42.0},
        {"ts": new_ts, "ram_percent": 45.0},
    ]
    assert M._history_rows == rows


def test_tick_without_failures_replaces_ring(monkeypatch):
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae")
    old_ts, new_ts = _iso(600), _iso(10)
    _prime_ring(monkeypatch, [{"ts": old_ts, "cpu_total": 42.0}])
    monkeypatch.setattr(M, "_fetch_history_series", _fake_fetch_factory({
        "cpu_total": [{"timestamp": new_ts, "value": 7.0, "hostname": "h1"}],
    }))
    rows, failed = M._history_refresh_tick()
    assert failed == set()
    assert rows == [{"ts": new_ts, "cpu_total": 7.0}]
    assert M._history_rows == rows


def test_tick_all_failed_keeps_prior_ring(monkeypatch):
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae")
    prior = [{"ts": _iso(600), "cpu_total": 42.0}]
    _prime_ring(monkeypatch, prior)
    monkeypatch.setattr(M, "_fetch_history_series",
                        lambda base, source, name, field, since, limit, hostname=None: (field, None))
    rows, failed = M._history_refresh_tick()
    assert rows == [] and failed
    assert M._history_rows == prior
