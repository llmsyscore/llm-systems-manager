"""#1216: a global budget on the routes that need no session or bearer, a 429
with Retry-After once it is spent, and one log line per window instead of one
per refused request."""
from __future__ import annotations

import logging

import pytest

import auth
import manager_mod as M

LOGGER = "llm-systems-manager.auth"


@pytest.fixture
def budget(monkeypatch):
    """A fresh budget of 3 anonymous requests per window, at a fixed clock."""
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(auth, "_anon_budget_limit", lambda: 3)
    monkeypatch.setattr(auth, "_ANON_BUDGET", auth._AnonBudget(now=lambda: clock["now"]))
    return clock


def _status(c, i=0):
    return c.get(f"/api/agents/agent-{i}/status", environ_base={"REMOTE_ADDR": "10.0.0.9"})


class TestBudget:
    def test_anonymous_requests_past_the_budget_get_429_with_retry_after(self, budget):
        with M.app.test_client() as c:
            codes = [_status(c, i).status_code for i in range(3)]
            assert 429 not in codes
            r = _status(c, 3)
            assert r.status_code == 429
            assert r.headers.get("Retry-After") == "60"
            assert r.get_json() == {"ok": False, "error": "too many requests; try again later"}

    def test_login_page_is_counted_and_refused_as_text(self, budget):
        with M.app.test_client() as c:
            for _ in range(3):
                c.get("/login")
            r = c.get("/login")
            assert r.status_code == 429
            assert r.headers.get("Retry-After") == "60"
            assert b"Too many requests" in r.data
            assert "text/plain" in r.content_type

    def test_registration_is_counted(self, budget):
        with M.app.test_client() as c:
            for _ in range(3):
                c.get("/login")
            r = c.post("/api/agents/register", json={}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
            assert r.status_code == 429
            assert r.get_json()["error"] == "too many requests; try again later"

    def test_window_rolls_after_sixty_seconds(self, budget):
        with M.app.test_client() as c:
            for _ in range(3):
                c.get("/login")
            assert c.get("/login").status_code == 429
            budget["now"] += 60
            assert c.get("/login").status_code == 200

    def test_health_and_static_are_not_counted(self, budget):
        with M.app.test_client() as c:
            for _ in range(10):
                c.get("/health")
                c.get("/static/css/base.css")
            assert c.get("/login").status_code == 200

    def test_zero_disables_the_budget(self, budget, monkeypatch):
        monkeypatch.setattr(auth, "_anon_budget_limit", lambda: 0)
        with M.app.test_client() as c:
            assert all(c.get("/login").status_code == 200 for _ in range(20))

    def test_default_budget_is_600_per_minute(self):
        model = type(auth._settings.manager.security)
        assert model.model_fields["anon_request_budget_per_min"].default == 600

    def test_known_agent_bearer_is_not_counted(self, budget, monkeypatch):
        monkeypatch.setattr(auth, "_agent_by_token", lambda tok: {"agent_id": "a1"} if tok == "tok-a1" else None)
        with M.app.test_client() as c:
            for _ in range(10):
                c.get("/api/agents/a1/status", headers={"Authorization": "Bearer tok-a1"})
            assert _status(c).status_code != 429

    def test_logged_in_session_is_not_counted(self, budget):
        with M.app.test_client() as c:
            with c.session_transaction() as s:
                s["auth_ok"] = True
                s["user"] = "llmadmin"
                s["role"] = "admin"
            for _ in range(10):
                assert c.get("/login").status_code != 429


class TestLogging:
    def test_one_warning_when_the_budget_trips_and_one_summary_per_window(self, budget, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER)
        with M.app.test_client() as c:
            for _ in range(3):
                c.get("/login")
            for _ in range(5):
                c.get("/login")
            tripped = [r for r in caplog.records if "anonymous request budget" in r.getMessage()]
            assert len(tripped) == 1
            assert "budget 3/min" in tripped[0].getMessage()
            budget["now"] += 60
            c.get("/login")
            msgs = [r.getMessage() for r in caplog.records if "anonymous request" in r.getMessage()]
            assert len(msgs) == 2
            assert "refused 5 anonymous requests" in msgs[1]

    def test_quiet_window_logs_nothing(self, budget, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER)
        with M.app.test_client() as c:
            c.get("/login")
            budget["now"] += 60
            c.get("/login")
        assert not [r for r in caplog.records if "anonymous request" in r.getMessage()]


class TestAuditReason:
    def test_login_refused_by_budget_audits_reason_budget(self, budget, monkeypatch):
        rows = []
        monkeypatch.setattr(M, "_audit_record", lambda row: rows.append(row))
        monkeypatch.setitem(M._AUDIT_CFG, "disabled", set())
        with M.app.test_client() as c:
            for _ in range(3):
                c.get("/login")
            r = c.post("/login", data={"username": "x", "password": "y"})
            assert r.status_code == 429
        assert rows and '"reason": "budget"' in (rows[-1][11] or "")


class TestRegistrationRefusalLine:
    def test_per_address_refusal_logs_once_per_minute_with_the_address(self, monkeypatch, caplog):
        import agent_registry
        caplog.set_level(logging.WARNING, logger="llm-systems-manager.agent_registry")
        monkeypatch.setattr(agent_registry, "_reg_rate", {})
        monkeypatch.setattr(agent_registry, "_reg_refused_logged", {})
        now = 2_000_000.0
        limit = agent_registry._REG_RATE_PER_MIN
        for _ in range(limit):
            assert agent_registry._reg_rate_limited("10.0.0.7", now=now) is False
        for _ in range(5):
            assert agent_registry._reg_rate_limited("10.0.0.7", now=now + 1) is True
        lines = [r.getMessage() for r in caplog.records if "registration REFUSED" in r.getMessage()]
        assert lines == [f"registration REFUSED (rate limit) from 10.0.0.7: {limit}/min reached"]
        assert agent_registry._reg_rate_limited("10.0.0.7", now=now + 61) is False
        for _ in range(limit):
            agent_registry._reg_rate_limited("10.0.0.7", now=now + 61)
        assert agent_registry._reg_rate_limited("10.0.0.7", now=now + 62) is True
        lines = [r.getMessage() for r in caplog.records if "registration REFUSED" in r.getMessage()]
        assert len(lines) == 2


class TestRequestStats:
    def test_stats_report_the_last_full_window(self, budget):
        with M.app.test_client() as c:
            for _ in range(5):
                c.get("/login")
            assert auth.anon_request_stats() == {"anon_requests_per_min": 0, "anon_refused_per_min": 0}
            budget["now"] += 60
            assert auth.anon_request_stats() == {"anon_requests_per_min": 5, "anon_refused_per_min": 2}

    def test_stats_after_a_quiet_minute_are_zero(self, budget):
        with M.app.test_client() as c:
            for _ in range(5):
                c.get("/login")
            budget["now"] += 125
            assert auth.anon_request_stats() == {"anon_requests_per_min": 0, "anon_refused_per_min": 0}

    def test_stats_read_writes_the_pending_summary_line(self, budget, caplog):
        caplog.set_level(logging.WARNING, logger=LOGGER)
        with M.app.test_client() as c:
            for _ in range(5):
                c.get("/login")
        budget["now"] += 60
        auth.anon_request_stats()
        assert [r for r in caplog.records if "refused 2 anonymous requests" in r.getMessage()]

    def test_route_needs_an_agent_bearer_or_a_session(self, budget, monkeypatch):
        monkeypatch.setattr(auth, "_agent_by_token", lambda tok: {"agent_id": "a1"} if tok == "tok-a1" else None)
        with M.app.test_client() as c:
            assert c.get("/api/manager/request-stats").status_code == 401
            r = c.get("/api/manager/request-stats", headers={"Authorization": "Bearer tok-a1"})
            assert r.status_code == 200
            assert r.get_json() == {"ok": True, "anon_requests_per_min": 0, "anon_refused_per_min": 0}

    def test_system_health_flow_carries_the_counters(self, budget, monkeypatch):
        monkeypatch.setattr(auth, "_ANON_BUDGET", auth._AnonBudget(now=lambda: 0.0))
        monkeypatch.setattr(auth, "_session_must_change", lambda: False)
        with M.app.test_client() as c:
            with c.session_transaction() as s:
                s["auth_ok"] = True
                s["user"] = "llmadmin"
                s["role"] = "admin"
            r = c.get("/api/admin/system-health")
            assert r.status_code == 200, r.data[:200]
            flow = r.get_json()["flow"]
            assert flow["anon_req_per_min"] == 0 and flow["anon_refused_per_min"] == 0


class TestRefusalsSurface:
    def _admin(self, c):
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["user"] = "llmadmin"
            s["role"] = "admin"

    def test_system_health_warns_when_requests_were_refused(self, budget, monkeypatch):
        monkeypatch.setattr(auth, "_session_must_change", lambda: False)
        with M.app.test_client() as c:
            for _ in range(5):
                c.get("/login")
            budget["now"] += 60
            self._admin(c)
            d = c.get("/api/admin/system-health").get_json()
        assert d["flow"]["anon_req_per_min"] == 5 and d["flow"]["anon_refused_per_min"] == 2
        assert any(w.startswith("open routes: 2 of 5 requests refused") for w in d["warnings"])
        # "warn" on a host with a reachable alarm engine; CI has none, so only "ok" is wrong.
        assert d["overall"] != "ok"

    def test_system_health_is_quiet_without_refusals(self, budget, monkeypatch):
        monkeypatch.setattr(auth, "_session_must_change", lambda: False)
        with M.app.test_client() as c:
            c.get("/login")
            budget["now"] += 60
            self._admin(c)
            d = c.get("/api/admin/system-health").get_json()
        assert not [w for w in d["warnings"] if w.startswith("open routes")]

    def test_stream_stats_carry_the_counters(self, budget, monkeypatch):
        monkeypatch.setattr(auth, "_session_must_change", lambda: False)
        with M.app.test_client() as c:
            for _ in range(4):
                c.get("/login")
            budget["now"] += 60
            self._admin(c)
            d = c.get("/api/admin/stream-stats").get_json()
        assert d["anon_requests_per_min"] == 4 and d["anon_refused_per_min"] == 1
        assert d["anon_budget_per_min"] == 3
