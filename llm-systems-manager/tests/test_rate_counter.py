"""#797: sliding-window counters behind the flow rates and gateway p50."""
from __future__ import annotations

import pytest

import rate_counter as rc


def test_rate_counter_reports_per_second_over_its_window():
    c = rc.RateCounter(60.0)
    c.add(30, now=1000.0)
    c.add(30, now=1030.0)
    assert c.total(now=1030.0) == 60
    assert c.per_s(now=1030.0) == pytest.approx(1.0)
    assert c.per_min(now=1030.0) == pytest.approx(60.0)


def test_rate_counter_prunes_the_trailing_window():
    c = rc.RateCounter(60.0)
    c.add(10, now=1000.0)
    assert c.total(now=1059.0) == 10
    assert c.total(now=1061.0) == 0


def test_rate_counter_ignores_non_positive_and_resets():
    c = rc.RateCounter(60.0)
    c.add(0)
    c.add(-3)
    assert c.total() == 0
    c.add(5)
    c.reset()
    assert c.total() == 0


def test_sample_window_percentile_and_pruning():
    w = rc.SampleWindow(900.0)
    assert w.percentile(0.5) is None
    for v in (10.0, 20.0, 300.0):
        w.add(v, now=1000.0)
    assert w.percentile(0.5, now=1000.0) == 20.0
    assert w.count(now=1000.0) == 3
    assert w.percentile(0.5, now=2000.0) is None


def test_sample_window_skips_non_numeric():
    w = rc.SampleWindow(900.0)
    w.add("nope", now=1000.0)
    w.add(None, now=1000.0)
    assert w.count(now=1000.0) == 0


def test_timestamp_ring_counts_a_sub_window():
    r = rc.TimestampRing(900.0)
    for i in range(5):
        r.add(now=1000.0 + i * 30)
    assert r.count_since(60.0, now=1120.0) == 3   # 1060, 1090, 1120
    assert r.count(now=1120.0) == 5
    assert r.count(now=3000.0) == 0
