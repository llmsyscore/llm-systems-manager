"""#889: draft-model discovery — family match, size ceiling, instruct preference, file choice, route."""
from __future__ import annotations

import configparser
import json
import types

import pytest

import draft_candidates as dc

TARGET = "unsloth/Qwen3.8-27B-GGUF"
ROWS = [
    {"id": "unsloth/Qwen3.8-0.6B-GGUF", "downloads": 900, "tags": ["gguf"]},
    {"id": "unsloth/Qwen3.8-4B-GGUF", "downloads": 5000, "tags": ["gguf"]},
    {"id": "unsloth/Qwen3.8-0.6B-Base-GGUF", "downloads": 100, "tags": ["gguf"]},
    {"id": "bartowski/Qwen3.8-1.7B-Instruct-GGUF", "downloads": 300, "tags": ["gguf", "instruct"]},
    {"id": "someone/Llama-1B-GGUF", "downloads": 9000, "tags": ["gguf"]},
    {"id": "unsloth/Qwen3.8-9B-GGUF", "downloads": 100, "tags": ["gguf"]},
]


def test_family_prefix_and_params():
    assert dc.family_prefix(TARGET) == "qwen3.8"
    assert dc.params_b("unsloth/Qwen3.8-0.6B-GGUF") == 0.6
    assert dc.params_b("org/NoSizeHere-GGUF") is None


def test_pick_repo_prefers_an_instruct_repo_then_the_smallest_of_the_family():
    got = dc.pick_repo(TARGET, ROWS)
    assert got["id"] == "bartowski/Qwen3.8-1.7B-Instruct-GGUF"
    rows = [r for r in ROWS if "Instruct" not in r["id"]]
    assert dc.pick_repo(TARGET, rows)["id"] == "unsloth/Qwen3.8-0.6B-GGUF"


def test_pick_repo_rejects_base_repos_other_families_and_oversize():
    only_base = [ROWS[2], ROWS[4], ROWS[5]]
    assert dc.pick_repo(TARGET, only_base) is None
    assert dc.pick_repo("org/Tiny-1B-GGUF", ROWS) is None      # nothing ≤ 25 % of 1B in the family


SIBS = [{"rfilename": "m-0.6B-Q8_0.gguf", "size": 700}, {"rfilename": "m-0.6B-Q4_K_M.gguf", "size": 400},
        {"rfilename": "m-0.6B-Q5_K_S.gguf", "size": 500}, {"rfilename": "mmproj-m.gguf", "size": 9},
        {"rfilename": "README.md", "size": 1}]


def test_pick_file_takes_q4_k_m_else_the_smallest_q4_or_q5():
    assert dc.pick_file(SIBS) == {"file": "m-0.6B-Q4_K_M.gguf", "size_bytes": 400}
    no_q4km = [s for s in SIBS if "Q4_K_M" not in s["rfilename"]]
    assert dc.pick_file(no_q4km) == {"file": "m-0.6B-Q5_K_S.gguf", "size_bytes": 500}
    assert dc.pick_file([{"rfilename": "m-Q8_0.gguf", "size": 1}]) is None


class _Fetch:
    def __init__(self, rows, sibs, search_status=200):
        self.rows, self.sibs, self.search_status, self.urls = rows, sibs, search_status, []

    def __call__(self, url):
        self.urls.append(url)
        if "/api/models?" in url:
            return self.search_status, json.dumps(self.rows)
        if url.endswith("blobs=true"):
            return 200, json.dumps({"siblings": self.sibs})
        return 404, ""


def test_build_candidate_searches_the_family_and_lists_only_the_chosen_repo():
    f = _Fetch(ROWS, SIBS)
    doc = dc.build_candidate(TARGET, f)
    assert doc["candidate"] == {"repo": "bartowski/Qwen3.8-1.7B-Instruct-GGUF", "file": "m-0.6B-Q4_K_M.gguf",
                                "size_bytes": 400, "params_b": 1.7}
    assert doc["reason"].startswith("smallest instruct")
    assert f.urls[0] == "https://huggingface.co/api/models?search=Qwen3.8&filter=gguf&sort=downloads&direction=-1&limit=50"
    assert sum("blobs=true" in u for u in f.urls) == 1


def test_build_candidate_explains_an_empty_result():
    doc = dc.build_candidate("org/NoSizeHere-GGUF", _Fetch(ROWS, SIBS))
    assert doc["candidate"] is None and doc["reason"] == "target parameter count unknown"
    doc = dc.build_candidate(TARGET, _Fetch([], SIBS))
    assert doc["candidate"] is None and doc["reason"] == "no smaller GGUF of the qwen3.8 family on Hugging Face"
    doc = dc.build_candidate(TARGET, _Fetch(ROWS, SIBS, search_status=503))
    assert doc["candidate"] is None and doc["error"] == "search unavailable"


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

    def get(self, url):
        return _Fetch(ROWS, SIBS)(url)


@pytest.fixture
def client(tmp_path):
    from flask import Flask
    app = Flask(__name__)
    ctx = types.SimpleNamespace(settings=types.SimpleNamespace(manager=types.SimpleNamespace(
        model_meta=types.SimpleNamespace(hf_token="hf_x", ttl_days=7))))
    ini = _ini({"qwen-local": {"hf-repo": TARGET, "hf-file": "Q4_K_M"}})
    _FakeFetcher.made.clear()
    dc.register_routes(app, ctx, db_path=str(tmp_path / "t.db"), read_ini=lambda: ini, fetcher_factory=_FakeFetcher)
    return app.test_client()


def test_route_requires_model_id_and_a_resolvable_repo(client):
    assert client.get("/api/llm/draft-candidates").status_code == 400
    assert client.get("/api/llm/draft-candidates?model_id=nope").status_code == 400


def test_route_resolves_a_slashless_id_through_config_and_caches(client):
    body = client.get("/api/llm/draft-candidates?model_id=qwen-local").get_json()
    assert body["ok"] is True and body["cached"] is False
    assert body["candidate"]["repo"] == "bartowski/Qwen3.8-1.7B-Instruct-GGUF"
    assert _FakeFetcher.made == ["hf_x"]
    again = client.get(f"/api/llm/draft-candidates?model_id={TARGET}:Q4_K_XL").get_json()
    assert again["cached"] is True
    fresh = client.get(f"/api/llm/draft-candidates?model_id={TARGET}:Q4_K_XL&refresh=1").get_json()
    assert fresh["cached"] is False and len(_FakeFetcher.made) == 2
