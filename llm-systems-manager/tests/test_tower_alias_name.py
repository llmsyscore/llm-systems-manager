"""Tower model naming: only the newest is "Tower Model · <model>"; operator names are kept."""
from __future__ import annotations

import json

import manager_mod


def _store(monkeypatch, tmp_path, data):
    f = tmp_path / "model_aliases.json"
    f.write_text(json.dumps(data))
    monkeypatch.setattr(manager_mod, "ALIASES_FILE", f)
    return f


def test_newest_tower_model_gets_named_and_older_ones_lose_the_name(monkeypatch, tmp_path):
    f = _store(monkeypatch, tmp_path, {"old-a": "Tower Model", "old-b": "Tower Model · old-b", "mine": "My model"})
    manager_mod._tower_alias_set("org/qwen3.5-9b@q6_k", "Tower Model")
    assert json.loads(f.read_text()) == {"mine": "My model", "org/qwen3.5-9b@q6_k": "Tower Model · qwen3.5-9b@q6_k"}


def test_operator_name_on_the_new_model_is_kept(monkeypatch, tmp_path):
    f = _store(monkeypatch, tmp_path, {"new": "Pinned", "old": "Tower Model"})
    manager_mod._tower_alias_set("new", "Tower Model")
    assert json.loads(f.read_text()) == {"new": "Pinned"}


def test_renaming_the_same_model_again_is_a_no_op(monkeypatch, tmp_path):
    f = _store(monkeypatch, tmp_path, {"m": "Tower Model · m"})
    before = f.stat().st_mtime_ns
    manager_mod._tower_alias_set("m", "Tower Model")
    assert json.loads(f.read_text()) == {"m": "Tower Model · m"} and f.stat().st_mtime_ns == before


def test_legacy_bare_name_on_the_new_model_is_upgraded(monkeypatch, tmp_path):
    f = _store(monkeypatch, tmp_path, {"m": "Tower Model"})
    manager_mod._tower_alias_set("m", "Tower Model")
    assert json.loads(f.read_text()) == {"m": "Tower Model · m"}


def test_alias_route_keeps_the_at_sign_of_lm_studio_ids(monkeypatch, tmp_path):
    f = _store(monkeypatch, tmp_path, {"qwen3.5-9b@q6_k": "Tower Model · qwen3.5-9b@q6_k"})
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    r = c.post("/api/llm/aliases", json={"model_id": "qwen3.5-9b@q6_k", "alias": "Quick 9B"})
    assert r.status_code == 200
    assert json.loads(f.read_text()) == {"qwen3.5-9b@q6_k": "Quick 9B"}
