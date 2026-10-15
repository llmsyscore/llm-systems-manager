"""#1258: the System Health "SSE stream pool" warning reflects the pool now —
every slot in use, or refusals in the recent window — not the lifetime count."""
from __future__ import annotations

import pytest

import manager_mod as M
import stream_pool


@pytest.fixture
def pool(monkeypatch):
    """A fresh 1-slot pool on a fixed clock, installed as the live POOL."""
    clock = {"now": 1_000.0}
    p = stream_pool.StreamPool(now=lambda: clock["now"])
    monkeypatch.setattr(p, "limit", lambda: 1)
    monkeypatch.setattr(stream_pool, "POOL", p)
    return p, clock


def _pool_warnings(c):
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    d = c.get("/api/admin/system-health").get_json()
    return [w for w in d["warnings"] if w.startswith("SSE stream pool")]


class TestStreamPoolRecentWindow:
    def test_recent_refusals_age_out_but_lifetime_count_stays(self, pool):
        p, clock = pool
        assert p.try_acquire() is True
        assert p.try_acquire() is False
        assert p.try_acquire() is False
        p.release()
        s = p.stats()
        assert s["refusals"] == 2 and s["refusals_recent"] == 2
        clock["now"] += stream_pool.REFUSAL_WINDOW_S + 1
        s = p.stats()
        assert s["refusals"] == 2 and s["refusals_recent"] == 0


class TestSystemHealthWarning:
    def test_quiet_when_nothing_was_refused(self, pool):
        with M.app.test_client() as c:
            assert _pool_warnings(c) == []

    def test_full_pool_warns_without_restart_advice(self, pool):
        p, _ = pool
        assert p.try_acquire() is True
        with M.app.test_client() as c:
            ws = _pool_warnings(c)
        assert len(ws) == 1 and "active=1/1" in ws[0]
        assert "restart" not in ws[0].lower()

    def test_recent_refusal_warns_then_clears_after_the_window(self, pool):
        p, clock = pool
        assert p.try_acquire() is True
        assert p.try_acquire() is False
        p.release()
        with M.app.test_client() as c:
            ws = _pool_warnings(c)
            assert len(ws) == 1
            assert "refused 1 stream" in ws[0] and "active=0/1" in ws[0]
            assert "restart" not in ws[0].lower()
            clock["now"] += stream_pool.REFUSAL_WINDOW_S + 1
            assert _pool_warnings(c) == []
            assert stream_pool.POOL.stats()["refusals"] == 1
