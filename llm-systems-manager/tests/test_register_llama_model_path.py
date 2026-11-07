"""#1419: register_llama_model writes model = <path> for cached files and upper-cases the section."""
from __future__ import annotations

import report_card as rc

AGENT = {"agent_id": "a" * 32, "token": "tok", "bind_url": "http://h:9899"}
PATH = "/home/u/.cache/huggingface/hub/models--a--b/snapshots/s/x.gguf"


def _run(monkeypatch, src, existing=None):
    posted = []

    def fake_json(agent, method, path, timeout=15, **kw):
        return dict(existing or {})

    def fake_call(agent, method, path, timeout=15, **kw):
        posted.append(kw.get("json"))
        return True, None

    monkeypatch.setattr(rc, "_agent_json", fake_json)
    monkeypatch.setattr(rc, "_agent_call", fake_call)
    assert rc.register_llama_model(AGENT, src) == (True, None)
    return posted[0]


def test_writes_model_path_when_src_has_path(monkeypatch):
    body = _run(monkeypatch, {"model_id": "a/b:Q4_K_M", "file": "x.gguf", "path": PATH})
    assert body["a/b:Q4_K_M"] == {"model": PATH, "hf-file": "x.gguf", "ctx-size": "4096"}


def test_section_name_quant_upper(monkeypatch):
    body = _run(monkeypatch, {"model_id": "a/b:q4_k_m", "file": "x.gguf"})
    assert list(body) == ["a/b:Q4_K_M"]


def test_legacy_src_without_path_unchanged(monkeypatch):
    other = {"keep/me:Q8_0": {"hf-file": "k.gguf"}}
    body = _run(monkeypatch, {"model_id": "a/b:Q4_K_M", "file": "x.gguf"}, existing=other)
    assert body["a/b:Q4_K_M"] == {"hf-file": "x.gguf", "ctx-size": "4096"}
    assert body["keep/me:Q8_0"] == {"hf-file": "k.gguf"}
