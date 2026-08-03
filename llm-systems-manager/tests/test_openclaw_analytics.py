"""OpenClaw analytics unit tests (#498): anomaly baselines, trend windows,
velocity, delivery-merge robustness, and the single-flight cache guard."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import openclaw


@pytest.fixture(autouse=True)
def _reset_agg_cache():
    saved = dict(openclaw._openclaw_agg_cache)
    openclaw._openclaw_agg_cache.update({"ts": 0, "payload": None})
    yield
    openclaw._openclaw_agg_cache.update(saved)


# ── _detect_cost_anomalies ───────────────────────────────────────────

def _sessions(costs, prefix):
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return [
        {"id": f"{prefix}-{i}", "cost": c,
         "last_ts": (base + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(costs)
    ]


def test_anomalies_use_per_agent_baseline():
    by_agent = {
        "cheap":  _sessions([0.01, 0.01, 0.01, 0.01, 0.01, 0.10], "c"),
        "pricey": _sessions([1.0, 1.0, 1.0, 1.0, 1.0, 1.0], "p"),
    }
    out = openclaw._detect_cost_anomalies(by_agent)
    assert [a["agent"] for a in out] == ["cheap"]
    assert out[0]["session_id"] == "c-5"
    assert out[0]["ratio"] == pytest.approx(10.0, abs=0.1)


def test_anomalies_need_three_prior_costed_sessions():
    by_agent = {"a": _sessions([0.01, 0.01, 5.0], "a")}
    assert openclaw._detect_cost_anomalies(by_agent) == []


# ── _analyze_usage_trends ────────────────────────────────────────────

def test_trend_excludes_todays_partial_bucket():
    today = datetime.now(timezone.utc).date()
    daily = {(today - timedelta(days=i)).isoformat(): 1000 for i in range(1, 5)}
    daily[today.isoformat()] = 0  # partial day must not read as a crash
    out = openclaw._analyze_usage_trends(daily)
    assert out["trend"] == "stable"
    assert out["dailyAvg"] == 1000


def test_trend_keeps_today_when_too_few_days():
    today = datetime.now(timezone.utc).date()
    daily = {(today - timedelta(days=i)).isoformat(): 500 for i in range(0, 3)}
    out = openclaw._analyze_usage_trends(daily)
    assert out["trend"] == "stable"


# ── _oc_velocity_from_agents ─────────────────────────────────────────

def _hour_key(dt):
    return dt.strftime("%Y-%m-%dT%H")


def test_velocity_hourly_window():
    now = datetime.now(timezone.utc)
    sess = {"last_ts": now.isoformat(),
            "hourly": {_hour_key(now): {"input": 600, "output": 600},
                       _hour_key(now - timedelta(hours=5)): {"input": 99999, "output": 0}},
            "daily": {}}
    out = openclaw._oc_velocity_from_agents([("a", sess)])
    assert out["window"] == "1h"
    assert out["tokens_1h"] == 1200
    assert out["active_sessions"] == 1
    assert 1200 / 120 <= out["tokens_per_min"] <= 1200 / 60


def test_velocity_legacy_agent_falls_back_to_day_window():
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    sess = {"last_ts": now.isoformat(),
            "daily": {day: {"input": 5000, "output": 5000}}}
    out = openclaw._oc_velocity_from_agents([("a", sess)])
    assert out["window"] == "day"
    assert out["tokens_1h"] == 10000


def test_velocity_empty_hourly_is_not_legacy():
    now = datetime.now(timezone.utc)
    sess = {"last_ts": now.isoformat(), "hourly": {}, "daily": {
        now.date().isoformat(): {"input": 5000, "output": 5000}}}
    out = openclaw._oc_velocity_from_agents([("a", sess)])
    assert out["window"] == "1h"
    assert out["tokens_1h"] == 0


def test_velocity_mixed_fleet_rates_each_source_over_its_own_span():
    now = datetime.now(timezone.utc)
    hourly_sess = {"last_ts": now.isoformat(),
                   "hourly": {_hour_key(now): {"input": 600, "output": 600}},
                   "daily": {}}
    legacy_sess = {"last_ts": now.isoformat(),
                   "daily": {now.date().isoformat(): {"input": 12000, "output": 12000}}}
    out = openclaw._oc_velocity_from_agents([("a", hourly_sess), ("b", legacy_sess)])
    assert out["window"] == "mixed"
    assert out["tokens_1h"] == 25200
    # Hourly part rates over <=120 min; day part over minutes-since-midnight.
    # The blended rate must never treat the 24000 day tokens as one-hour data.
    assert out["tokens_per_min"] <= 1200 / 60 + 24000 / 60
    assert out["tokens_per_min"] >= 1200 / 120


def test_velocity_ignores_idle_sessions():
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    sess = {"last_ts": stale, "hourly": {}, "daily": {}}
    out = openclaw._oc_velocity_from_agents([("a", sess)])
    assert out["active_sessions"] == 0
    assert out["tokens_1h"] == 0


# ── _oc_merge_delivery ───────────────────────────────────────────────

def test_merge_delivery_tolerates_malformed_error_entries():
    hosts = [{
        "total": 2, "by_channel": {"discord": 2}, "total_retries": 3,
        "common_errors": [{"error": "timeout", "count": 2}, {"count": 1},
                          None, "junk"],
        "oldest_enqueue_iso": "2026-08-01T00:00:00+00:00",
    }]
    out = openclaw._oc_merge_delivery(hosts)
    assert out["total"] == 2
    assert out["common_errors"] == [{"error": "timeout", "count": 2}]


# ── single-flight cache ──────────────────────────────────────────────

def test_collect_is_single_flight(monkeypatch):
    calls = []

    def _slow_collect():
        calls.append(1)
        time.sleep(0.3)
        return {"ts": 1, "agents": []}

    monkeypatch.setattr(openclaw, "_do_collect_openclaw_analytics", _slow_collect)
    threads = [threading.Thread(target=openclaw._collect_openclaw_analytics)
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(calls) == 1


def test_collect_serves_stale_while_refreshing(monkeypatch):
    stale = {"ts": 0, "marker": "stale"}
    openclaw._openclaw_agg_cache.update({"ts": time.time() - 60, "payload": stale})
    release = threading.Event()

    def _blocked_collect():
        release.wait(timeout=5)
        return {"marker": "fresh"}

    monkeypatch.setattr(openclaw, "_do_collect_openclaw_analytics", _blocked_collect)
    refresher = threading.Thread(target=openclaw._collect_openclaw_analytics)
    refresher.start()
    time.sleep(0.1)  # let the refresher take the lock
    t0 = time.time()
    got = openclaw._collect_openclaw_analytics()
    assert got is stale
    assert time.time() - t0 < 0.2
    release.set()
    refresher.join(timeout=5)
    assert openclaw._openclaw_agg_cache["payload"]["marker"] == "fresh"
