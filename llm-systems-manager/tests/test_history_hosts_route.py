"""#1432: GET /api/history/hosts returns every host's history for a few fields, keyed by hostname."""
from __future__ import annotations

import pytest

import agent_registry
import auth
import manager_mod as M
import manager_users

DEFAULT = ("cpu_total", "ram_percent", "disk_root_pct", "net_recv", "io_write")


def _points(n=4, hosts=("beta", "alpha", "gamma", "stranger")):
    """n points a minute apart per host, newest last, value = minute index."""
    out = []
    for h in hosts:
        for i in range(n):
            out.append({"timestamp": f"2026-11-16T10:{i:02d}:00+00:00", "hostname": h, "value": float(i)})
    return out


@pytest.fixture
def fetches(monkeypatch):
    """Fake alarm-engine series reads; records (field, hostname) per call."""
    calls = []
    state = {"n": 4, "fail": set(), "limits": []}

    def fake(base, source, metric_name, field, since_minutes, limit, hostname=None):
        calls.append((field, hostname, since_minutes))
        state["limits"].append(limit)
        if field in state["fail"]:
            return field, None
        return field, _points(state["n"])

    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.test")
    monkeypatch.setattr(M, "_fetch_history_series", fake)
    monkeypatch.setattr(M, "_history_scoped_cache", {})
    monkeypatch.setattr(agent_registry, "load_agents", lambda *a, **k: {"agents": {
        "a1": {"status": "approved", "hostname": "alpha"},
        "a2": {"status": "approved", "hostname": "beta"},
        "a3": {"status": "pending", "hostname": "gamma"},
    }})
    return calls, state


def _client():
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_unknown_too_many_or_empty_fields_are_400(fetches):
    c = _client()
    assert c.get("/api/history/hosts?fields=cpu_total,nope").status_code == 400
    many = ",".join(f for _s, _n, f in M._HISTORY_LEGACY_FIELD_MAP[:9])
    assert c.get("/api/history/hosts?fields=" + many).status_code == 400
    assert c.get("/api/history/hosts?fields=").status_code == 400
    assert fetches[0] == []


def test_default_fields_split_by_host_one_read_per_field(fetches):
    calls, _ = fetches
    r = _client().get("/api/history/hosts?since_minutes=60")
    assert r.status_code == 200
    hosts = r.get_json()["hosts"]
    assert set(hosts) == {"alpha", "beta"}
    assert sorted(f for f, _h, _s in calls) == sorted(DEFAULT)
    assert all(h is None for _f, h, _s in calls)
    rows = hosts["alpha"]
    assert [x["ts"] for x in rows] == sorted(x["ts"] for x in rows)
    assert len(rows) == 4
    assert set(rows[0]) == {"ts", *DEFAULT}


def test_named_fields_only(fetches):
    calls, _ = fetches
    hosts = _client().get("/api/history/hosts?fields=cpu_total,gpu_util").get_json()["hosts"]
    assert sorted(f for f, _h, _s in calls) == ["cpu_total", "gpu_util"]
    assert set(hosts["beta"][0]) == {"ts", "cpu_total", "gpu_util"}


def test_each_host_thinned_to_max_rows(fetches):
    _, state = fetches
    state["n"] = 10
    hosts = _client().get("/api/history/hosts?max_rows=3").get_json()["hosts"]
    for h in ("alpha", "beta"):
        assert len(hosts[h]) == 3
        assert hosts[h][0]["cpu_total"] == 0.0 and hosts[h][-1]["cpu_total"] == 9.0


def test_one_cache_key_per_field_set_and_window(fetches):
    calls, _ = fetches
    c = _client()
    c.get("/api/history/hosts?since_minutes=1440&max_rows=864")
    c.get("/api/history/hosts?since_minutes=1440&max_rows=3")
    assert len(calls) == len(DEFAULT)
    keys = list(M._history_scoped_cache)
    assert len(keys) == 1
    assert keys[0][0] == ("hosts", DEFAULT) and keys[0][1] == 1440
    c.get("/api/history/hosts?since_minutes=5")
    assert len(M._history_scoped_cache) == 2


def test_no_alarm_engine_is_empty(fetches, monkeypatch):
    monkeypatch.setattr(M, "_alarm_engine_url", "")
    r = _client().get("/api/history/hosts")
    assert r.status_code == 200 and r.get_json() == {"hosts": {}}
    assert fetches[0] == []


def test_failed_field_leaves_the_others_lists_it_and_is_not_cached(fetches):
    calls, state = fetches
    state["fail"] = {"ram_percent"}
    c = _client()
    body = c.get("/api/history/hosts").get_json()
    rows = body["hosts"]["alpha"]
    assert "ram_percent" not in rows[0] and "cpu_total" in rows[0]
    assert body["failed"] == ["ram_percent"]
    assert M._history_scoped_cache == {}
    state["fail"] = set()
    body = c.get("/api/history/hosts").get_json()
    assert "failed" not in body and "ram_percent" in body["hosts"]["alpha"][0]
    assert len(calls) == 2 * len(DEFAULT)
    assert len(M._history_scoped_cache) == 1


def test_only_approved_hosts_are_kept(fetches):
    hosts = _client().get("/api/history/hosts").get_json()["hosts"]
    assert set(hosts) == {"alpha", "beta"}
    (cached,) = M._history_scoped_cache.values()
    assert set(cached[1]["hosts"]) == {"alpha", "beta"}


def test_read_limit_scales_with_approved_hosts(fetches):
    _, state = fetches
    c = _client()
    c.get("/api/history/hosts?limit=100")
    assert set(state["limits"]) == {200}
    state["limits"].clear()
    c.get("/api/history/hosts?limit=9000")
    assert set(state["limits"]) == {10000}
    state["limits"].clear()
    c.get("/api/history/hosts")
    assert set(state["limits"]) == {min(10000, M.HISTORY_FETCH_LIMIT * 2)}


def test_agent_token_not_allowed():
    assert auth._agent_bearer_allowed("/api/history/hosts") is False


class TestRoleGate:
    """Operator sessions read both All hosts routes; anonymous requests are refused."""

    @pytest.fixture
    def users(self, tmp_path, monkeypatch, fetches):
        monkeypatch.setattr(M, "_admin_ip_allowed", lambda _ip: True, raising=False)
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")
        monkeypatch.setattr(M.agent_registry, "load_agents", lambda *a, **k: {"agents": {}})
        manager_users.init(tmp_path / "mu.json", threshold=5, window_s=900, duration_s=900)
        manager_users.STORE.seed_admin("llmadmin", auth.scrypt_hash("ci-admin-pw-1"))
        manager_users.STORE.create("op1", auth.scrypt_hash("op1pass1"), "operator")
        M.app.config.update(TESTING=True)

    @pytest.mark.parametrize("path", ["/api/agents/hosts", "/api/history/hosts"])
    def test_operator_200(self, users, path):
        c = M.app.test_client()
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "operator"
            s["user"] = "op1"
        assert c.get(path).status_code == 200

    @pytest.mark.parametrize("path", ["/api/agents/hosts", "/api/history/hosts"])
    def test_anonymous_401(self, users, path):
        assert M.app.test_client().get(path).status_code == 401
