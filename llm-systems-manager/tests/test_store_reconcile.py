"""#1009: leftover finder for the profile and alias stores + admin routes."""
from __future__ import annotations

import pytest

import manager_mod
import model_profiles
import store_reconcile as sr

AGENTS = {"agents": {"a1": {"hostname": "box-1", "status": "approved"},
                     "a2": {"hostname": "box-2", "status": "approved"}}}
INDEX = ({"llama:m1": ["a1"], "llama:m2": ["a2"], "lms:m3": ["a2"]}, {"llama:m1": ["a1"]})


def _store(tmp_path):
    s = model_profiles.ProfileStore(tmp_path / "p.json")
    s.put_profile("a1", "m1", "default", {"x": "1"})
    s.put_profile("a1", "gone", "tuned", {"x": "2"})
    s.put_profile("a1", "gone", "second", {"x": "3"})
    s.put_profile("a1", "smoke-test-model", "smoke-profile", {})
    s.put_profile("a2", "m2", "default", {})
    s.put_profile("dead", "m1", "default", {})
    s.put_profile("dead", "old", "a", {})
    s.put_profile("dead", "smoke-test-model", "smoke-profile", {})
    return s


class _Aliases:
    def __init__(self, data):
        self.data = dict(data)
        self.saved = 0

    def load(self):
        return dict(self.data)

    def save(self, d):
        self.data = dict(d)
        self.saved += 1


def _rec(tmp_path, index=INDEX, aliases=None, agents=AGENTS):
    box = _Aliases(aliases if aliases is not None else {"m1": "One", "zzz": "Gone"})
    r = sr.Reconciler(_store(tmp_path), box.load, box.save, lambda: agents, lambda: index, now=lambda: 1000.0)
    return r, box


def test_by_agent_merges_catalog_and_serving():
    assert sr.by_agent({"llama:m1": ["a1", "a2"]}, {"vllm:m9": ["a2"]}) == {"a1": {"m1"}, "a2": {"m1", "m9"}}


def test_classify_groups_every_kind_of_leftover(tmp_path):
    store = _store(tmp_path)
    g = sr.classify(store.snapshot(), {"m1": "One", "zzz": "Gone"}, AGENTS["agents"], INDEX)
    assert sorted((x["agent"], x["model"]) for x in g["smoke"]) == [("a1", "smoke-test-model"), ("dead", "smoke-test-model")]
    assert g["removed_agents"] == [{"agent": "dead", "models": ["m1", "old"], "profiles": 2}]
    assert g["absent_models"] == [{"agent": "a1", "host": "box-1", "model": "gone", "profiles": 2}]
    assert g["absent_aliases"] == [{"model": "zzz", "alias": "Gone"}]
    assert g["aliases_checked"] is True and g["unverified"] == []


def test_classify_skips_hosts_without_an_index_entry(tmp_path):
    store = _store(tmp_path)
    g = sr.classify(store.snapshot(), {}, AGENTS["agents"], ({"llama:m1": ["a1"]}, {}))
    assert g["unverified"] == ["box-2"]
    assert g["unverified_models"] == [{"agent": "a2", "host": "box-2", "model": "m2", "profiles": 1}]
    assert [x["model"] for x in g["absent_models"]] == ["gone"]
    assert g["unverified_aliases"] == []


def test_classify_without_an_index_lists_everything_unverified(tmp_path):
    store = _store(tmp_path)
    g = sr.classify(store.snapshot(), {"zzz": "Gone"}, AGENTS["agents"], None)
    assert g["absent_models"] == [] and g["absent_aliases"] == []
    assert g["aliases_checked"] is False and g["unverified"] == ["box-1", "box-2"]
    assert [(x["host"], x["model"]) for x in g["unverified_models"]] == [("box-1", "gone"), ("box-1", "m1"), ("box-2", "m2")]
    assert g["unverified_aliases"] == [{"model": "zzz", "alias": "Gone"}]
    assert len(g["smoke"]) == 2 and [x["agent"] for x in g["removed_agents"]] == ["dead"]


def test_clean_all_includes_unverified_rows(tmp_path):
    r, box = _rec(tmp_path, index=None, aliases={"zzz": "Gone"})
    out = r.clean({"all": True})
    assert out["removed"] == {"agents": 1, "models": 3, "aliases": 1}
    assert r._store.snapshot() == {} and box.data == {}


def test_run_prunes_smoke_entries_and_records_counts(tmp_path):
    r, _ = _rec(tmp_path)
    out = r.run("test")
    assert out["pruned_smoke"] == 2 and out["at"] == 1000.0 and out["reason"] == "test"
    assert out["counts"] == {"removed_agents": 1, "absent_models": 1, "absent_aliases": 1,
                             "unverified_models": 0, "unverified_aliases": 0} and out["total"] == 3
    snap = r._store.snapshot()
    assert "smoke-test-model" not in snap["a1"] and "smoke-test-model" not in snap["dead"]
    assert r.leftovers()["total"] == 3
    assert r.run("again")["pruned_smoke"] == 0


def test_clean_removes_the_selected_entries_only(tmp_path):
    r, box = _rec(tmp_path)
    out = r.clean({"models": [{"agent": "a1", "model": "gone"}], "aliases": ["zzz", "nope"]})
    assert out["removed"] == {"agents": 0, "models": 1, "aliases": 1}
    assert box.data == {"m1": "One"} and box.saved == 1
    snap = r._store.snapshot()
    assert "gone" not in snap["a1"] and "m1" in snap["a1"] and "dead" in snap
    assert out["total"] == 1 and out["removed_agents"][0]["agent"] == "dead"


def test_clean_all_empties_every_group(tmp_path):
    r, box = _rec(tmp_path)
    out = r.clean({"all": True})
    assert out["removed"] == {"agents": 1, "models": 1, "aliases": 1}
    assert out["total"] == 0 and "dead" not in r._store.snapshot()
    assert r.clean({"all": True})["removed"] == {"agents": 0, "models": 0, "aliases": 0}


def test_drop_model_removes_an_emptied_agent(tmp_path):
    s = model_profiles.ProfileStore(tmp_path / "p.json")
    s.put_profile("a1", "m1", "default", {})
    assert s.drop_model("a1", "nope") is False
    assert s.drop_model("a1", "m1") is True and s.snapshot() == {}
    assert s.drop_agent("a1") is False


@pytest.fixture
def client(monkeypatch, tmp_path):
    r, box = _rec(tmp_path)
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    monkeypatch.setattr(manager_mod, "_store_reconciler", r)
    calls = []
    monkeypatch.setattr(manager_mod, "_refresh_model_index_blocking", lambda: calls.append(1))
    manager_mod.app.config["TESTING"] = True
    with manager_mod.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c, r, box, calls


def test_leftovers_route_reports_groups_and_refresh_reruns(client):
    c, r, _box, calls = client
    d = c.get("/api/admin/stores/leftovers").get_json()
    assert d["ok"] is True and d["total"] == 3 and d["reason"] == "first read"
    assert calls == []
    d = c.get("/api/admin/stores/leftovers?refresh=1").get_json()
    assert d["reason"] == "operator check" and calls == [1]


def test_clean_route_validates_the_body_and_removes(client):
    c, r, box, _calls = client
    assert c.post("/api/admin/stores/clean", json={}).status_code == 400
    assert c.post("/api/admin/stores/clean", json=["x"]).status_code == 400
    d = c.post("/api/admin/stores/clean", json={"aliases": ["zzz"]}).get_json()
    assert d["ok"] is True and d["removed"] == {"agents": 0, "models": 0, "aliases": 1}
    d = c.post("/api/admin/stores/clean", json={"all": True}).get_json()
    assert d["removed"] == {"agents": 1, "models": 1, "aliases": 0} and d["total"] == 0


def test_on_model_deleted_drops_profiles_and_an_unshared_name(tmp_path, monkeypatch):
    r, box = _rec(tmp_path, aliases={"m1": "One", "m2": "Two"})
    monkeypatch.setattr(r, "run_bg", lambda reason: None)
    assert r.on_model_deleted("a1", "m1") == {"profiles": True, "alias": True}
    assert "m1" not in r._store.snapshot()["a1"] and box.data == {"m2": "Two"}
    # m2 is listed by a2, so deleting it on a1 keeps its name; a1 has no m2 profiles.
    assert r.on_model_deleted("a1", "m2") == {"profiles": False, "alias": False}
    assert box.data == {"m2": "Two"}


def test_on_agent_deleted_drops_every_profile_under_the_id(tmp_path, monkeypatch):
    r, _ = _rec(tmp_path)
    monkeypatch.setattr(r, "run_bg", lambda reason: None)
    assert r.on_agent_deleted("dead") is True and "dead" not in r._store.snapshot()
    assert r.on_agent_deleted("dead") is False


def test_agent_delete_hook_is_wired():
    import agent_registry
    # Other tests load a second manager module copy, so compare by method, not by instance.
    hook = agent_registry.on_agent_deleted
    assert getattr(hook, "__func__", None) is sr.Reconciler.on_agent_deleted
    assert isinstance(getattr(hook, "__self__", None), sr.Reconciler)


def test_config_delete_route_cascades_on_upstream_success(client, monkeypatch):
    c, r, box, _calls = client
    monkeypatch.setattr(r, "run_bg", lambda reason: None)

    class _Resp:
        def __init__(self, ok): self.ok = ok

    def fake_proxy(kind, method, path, **kw):
        kw["on_target"]({"agent_id": "a1"}, _Resp(fake_proxy.ok))
        return manager_mod.jsonify({"ok": fake_proxy.ok})

    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    fake_proxy.ok = False
    c.delete("/api/llm/config/gone")
    assert "gone" in r._store.snapshot()["a1"]
    fake_proxy.ok = True
    c.delete("/api/llm/config/gone")
    assert "gone" not in r._store.snapshot()["a1"]
    c.delete("/api/llm/config/m1")
    assert box.data == {"zzz": "Gone"}
