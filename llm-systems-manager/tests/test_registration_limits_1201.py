"""#1201: registration body limit, field validation, per-address rate limit,
pending caps, hostname coexistence for pending records, and the expiry sweep."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import agent_registry
import manager_mod as M

FP = "sha256:" + "ab" * 32
VER = agent_registry.FP_REAUTH_FROM_VERSION


def _rec(aid, hostname="box", status="pending", registered_from="10.0.0.5", age_s=0, fp=FP):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return {"agent_id": aid, "hostname": hostname, "os": "linux", "role": "auto",
            "bind_url": "http://10.0.0.5:8765", "fingerprint": fp, "version": VER,
            "status": status, "token": "tok-" + aid if status == "approved" else None,
            "registered_from": registered_from, "capabilities": {},
            "first_seen": ts, "last_register": ts}


@pytest.fixture
def store(monkeypatch):
    data = {"agents": {}, "global": {}}
    saved = {}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: data)
    monkeypatch.setattr(agent_registry, "save_agents", lambda d: saved.update(d))
    monkeypatch.setattr(agent_registry, "_reg_rate", {})
    data["saved"] = saved
    return data


def _register(c, remote="10.0.0.9", **over):
    body = {"hostname": "box", "os": "linux", "bind_url": "http://10.0.0.9:8765",
            "fingerprint": "sha256:" + "cd" * 32, "version": VER}
    body.update(over)
    return c.post("/api/agents/register", json=body, environ_base={"REMOTE_ADDR": remote})


class TestBodyAndFields:
    def test_oversized_body_is_413(self, store):
        with M.app.test_client() as c:
            r = c.post("/api/agents/register", data=b"{" + b" " * (64 * 1024) + b"}",
                       content_type="application/json", environ_base={"REMOTE_ADDR": "10.0.0.9"})
            assert r.status_code == 413

    @pytest.mark.parametrize("over", [
        {"hostname": "x" * 254},
        {"bind_url": "ftp://10.0.0.9/"},
        {"bind_url": 12},
        {"description": "d" * 1001},
        {"capabilities": ["llama"]},
        {"image_gen_port": 70000},
        {"image_gen_port": "1234"},
        {"image_gen_port": True},
        {"role": {"a": 1}},
    ])
    def test_bad_fields_are_400(self, store, over):
        with M.app.test_client() as c:
            r = _register(c, **over)
            assert r.status_code == 400, r.get_json()
            assert store["agents"] == {}

    def test_a_valid_body_registers_as_pending(self, store):
        with M.app.test_client() as c:
            r = _register(c, image_gen_port=1234, capabilities={"llama": True})
            assert r.status_code == 200 and r.get_json()["status"] == "pending"
            (rec,) = store["agents"].values()
            assert rec["registered_from"] == "10.0.0.9"


class TestRateLimit:
    def test_seventh_registration_in_a_minute_is_429(self, store):
        # Loopback registrations auto-approve, so only the rate limit can stop them.
        with M.app.test_client() as c:
            codes = [_register(c, hostname=f"h{i}", remote="127.0.0.1").status_code for i in range(7)]
        assert codes[:6] == [200] * 6 and codes[6] == 429
        assert agent_registry._REG_RATE_PER_MIN == 6

    def test_the_address_table_is_bounded_even_when_every_address_is_active(self, monkeypatch):
        monkeypatch.setattr(agent_registry, "_reg_rate", {})
        monkeypatch.setattr(agent_registry, "_REG_RATE_TABLE_MAX", 5)
        for i in range(50):
            assert agent_registry._reg_rate_limited(f"10.0.{i // 250}.{i % 250}", now=1000.0) is False
        assert len(agent_registry._reg_rate) == 5
        # Idle addresses go first; the newest ones are the ones kept.
        assert "10.0.0.49" in agent_registry._reg_rate

    def test_the_window_slides(self, monkeypatch):
        monkeypatch.setattr(agent_registry, "_reg_rate", {})
        for _ in range(6):
            assert agent_registry._reg_rate_limited("1.2.3.4", now=1000.0) is False
        assert agent_registry._reg_rate_limited("1.2.3.4", now=1000.0) is True
        assert agent_registry._reg_rate_limited("1.2.3.4", now=1061.0) is False
        assert agent_registry._reg_rate_limited("5.6.7.8", now=1000.0) is False


class TestPendingCaps:
    def test_total_cap(self, store):
        for i in range(100):
            store["agents"][f"p{i}"] = _rec(f"p{i}", hostname=f"h{i}", registered_from=f"10.1.{i // 250}.{i % 250}")
        with M.app.test_client() as c:
            r = _register(c, hostname="fresh")
            assert r.status_code == 429 and "waiting for approval" in r.get_json()["error"]

    def test_per_address_cap(self, store):
        for i in range(10):
            store["agents"][f"p{i}"] = _rec(f"p{i}", hostname=f"h{i}", registered_from="10.0.0.9")
        with M.app.test_client() as c:
            assert _register(c, hostname="fresh", remote="10.0.0.9").status_code == 429
            assert _register(c, hostname="fresh", remote="10.0.0.10").status_code == 200

    def test_loopback_auto_approval_is_not_capped(self, store):
        for i in range(100):
            store["agents"][f"p{i}"] = _rec(f"p{i}", hostname=f"h{i}")
        with M.app.test_client() as c:
            r = _register(c, hostname="local", remote="127.0.0.1")
            assert r.status_code == 200 and r.get_json()["status"] == "approved"


class TestHostnameCoexistence:
    def test_a_pending_record_does_not_block_a_new_registration_from_another_address(self, store):
        store["agents"]["p1"] = _rec("p1", registered_from="10.0.0.5")
        with M.app.test_client() as c:
            r = _register(c, remote="10.0.0.9")
            assert r.status_code == 200
            assert r.get_json()["agent_id"] != "p1"
            assert len(store["agents"]) == 2

    def test_a_repeat_from_the_same_address_returns_the_waiting_record(self, store):
        store["agents"]["p1"] = _rec("p1", registered_from="10.0.0.9", fp="sha256:" + "ee" * 32)
        before = dict(store["agents"]["p1"])
        with M.app.test_client() as c:
            for _ in range(3):
                r = _register(c, remote="10.0.0.9", bind_url="http://10.0.0.9:9999")
                assert r.status_code == 200
                assert r.get_json() == {"ok": True, "agent_id": "p1", "status": "pending",
                                        "approval_url": "/?tab=admin#agent=p1"}
        assert len(store["agents"]) == 1
        assert store["agents"]["p1"] == before          # nothing overwritten
        assert store["saved"] == {}                      # nothing written

    def test_an_approved_record_still_blocks(self, store):
        store["agents"]["a1"] = _rec("a1", status="approved")
        with M.app.test_client() as c:
            r = _register(c)
            assert r.status_code == 403 and r.get_json()["agent_id"] == "a1"

    def test_the_fingerprint_owner_updates_its_own_pending_record(self, store):
        store["agents"]["p1"] = _rec("p1", fp=FP)
        store["agents"]["p2"] = _rec("p2", fp="sha256:" + "ee" * 32)
        with M.app.test_client() as c:
            r = _register(c, fingerprint=FP, remote="10.0.0.42")
            assert r.status_code == 200 and r.get_json()["agent_id"] == "p1"
            assert store["agents"]["p1"]["registered_from"] == "10.0.0.42"
            assert len(store["agents"]) == 2


class TestExpirySweep:
    def test_old_pending_records_go_young_and_approved_stay(self, store):
        store["agents"]["old"] = _rec("old", hostname="old", age_s=8 * 86400)
        store["agents"]["young"] = _rec("young", hostname="young", age_s=6 * 86400)
        store["agents"]["appr"] = _rec("appr", hostname="appr", status="approved", age_s=30 * 86400)
        store["agents"]["nodate"] = {**_rec("nodate", hostname="nd"), "last_register": None, "first_seen": None}
        assert agent_registry.sweep_expired_pending() == 1
        assert set(store["agents"]) == {"young", "appr", "nodate"}
        assert set(store["saved"]["agents"]) == {"young", "appr", "nodate"}

    def test_nothing_to_remove_saves_nothing(self, store):
        store["agents"]["young"] = _rec("young", age_s=60)
        assert agent_registry.sweep_expired_pending() == 0
        assert store["saved"] == {}

    def test_a_registration_request_never_runs_the_sweep(self, store, monkeypatch):
        calls = []
        monkeypatch.setattr(agent_registry, "sweep_expired_pending", lambda *a, **k: calls.append(1))
        with M.app.test_client() as c:
            assert _register(c).status_code == 200
        assert calls == []

    def test_the_sweep_runs_on_its_own_thread(self):
        import re
        src = open(agent_registry.__file__).read()
        assert re.search(r"Thread\(target=_pending_sweep_loop", src)
        assert agent_registry._REG_PENDING_TTL_S == 7 * 86400
