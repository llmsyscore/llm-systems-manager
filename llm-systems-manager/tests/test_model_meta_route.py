"""#878: GET /api/llm/model-meta — validation, cache, refresh, token plumbing."""
from __future__ import annotations

import configparser
import json
import types

import pytest

import model_meta as mm
import settings_catalog as sc
from config.unified_config import settings as live_settings

Q = "unsloth/Qwen3-GGUF"


def _ini(sections):
    cp = configparser.ConfigParser(default_section="__DEFAULTS__", interpolation=None)
    cp.optionxform = str
    for name, kv in sections.items():
        cp.add_section(name)
        for k, v in kv.items():
            cp.set(name, k, v)
    return cp


class _FakeFetcher:
    made = []

    def __init__(self, token=""):
        _FakeFetcher.made.append(token)
        self.calls = 0

    def get(self, url):
        self.calls += 1
        if url.endswith(f"/api/models/{Q}"):
            return 200, json.dumps({"siblings": [{"rfilename": "generation_config.json"}], "cardData": {}})
        if url.endswith("generation_config.json"):
            return 200, '{"temperature": 0.7}'
        return 404, ""


@pytest.fixture
def client(tmp_path):
    from flask import Flask
    app = Flask(__name__)
    ctx = types.SimpleNamespace(settings=types.SimpleNamespace(manager=types.SimpleNamespace(
        model_meta=types.SimpleNamespace(hf_token="hf_x", ttl_days=7))))
    ini = _ini({"qwen-local": {"hf-repo": Q, "hf-file": "Q4_K_M"}, "no-repo": {"ctx-size": "4096"}})
    _FakeFetcher.made.clear()
    calls = []
    mm.register_routes(app, ctx, db_path=str(tmp_path / "t.db"),
                       read_ini=lambda: (calls.append(1), ini)[1],
                       fetcher_factory=_FakeFetcher)
    tc = app.test_client()
    tc.read_ini_calls = calls
    return tc


def test_requires_model_id(client):
    assert client.get("/api/llm/model-meta").status_code == 400


def test_unresolvable_repo_is_400(client):
    r = client.get("/api/llm/model-meta?model_id=no-repo")
    assert r.status_code == 400 and "repo" in r.get_json()["error"]
    assert client.get("/api/llm/model-meta?model_id=../x").status_code == 400


def test_section_repo_then_cache_then_refresh(client):
    r1 = client.get("/api/llm/model-meta?model_id=qwen-local").get_json()
    assert r1["ok"] and r1["repo"] == Q and r1["cached"] is False
    assert r1["suggestions"] == [{"key": "temperature", "value": 0.7, "source": "sidecar"}]
    r2 = client.get("/api/llm/model-meta?model_id=qwen-local").get_json()
    assert r2["cached"] is True and r2["fetched_ts"] == r1["fetched_ts"]
    r3 = client.get("/api/llm/model-meta?model_id=qwen-local&refresh=1").get_json()
    assert r3["cached"] is False
    assert _FakeFetcher.made == ["hf_x", "hf_x"]


def test_model_id_with_repo_prefix_resolves_without_section(client):
    r = client.get(f"/api/llm/model-meta?model_id={Q}:Q4_K_M").get_json()
    assert r["ok"] and r["repo"] == Q
    assert client.read_ini_calls == []


def test_settings_schema_and_catalog():
    from config.unified_config import ManagerModelMeta
    assert hasattr(live_settings.manager, "model_meta")
    assert ManagerModelMeta().hf_token == "" and ManagerModelMeta().ttl_days == 7
    paths = {e["path"]: e for e in sc.CATALOG}
    assert paths["manager.model_meta.hf_token"]["secret"] is True
    assert paths["manager.model_meta.ttl_days"]["type"] == "int"
    assert ("models", "Models & Metadata") in sc.GROUPS
