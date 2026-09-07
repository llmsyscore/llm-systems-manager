"""#878: source precedence, base-model hop, fetch limits, cache TTL."""
from __future__ import annotations

import json
import sqlite3
import time

import model_meta as mm

Q = "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF"
B = "Qwen/Qwen3-30B-A3B-Instruct-2507"


def _api(siblings, base=None):
    return json.dumps({"siblings": [{"rfilename": s} for s in siblings],
                       "cardData": ({"base_model": base} if base else {}), "gated": False})


def _fake(routes):
    calls = []

    def fetch(url):
        calls.append(url)
        return routes.get(url, (404, ""))
    fetch.calls = calls
    return fetch


def test_sidecar_beats_card_beats_base():
    routes = {
        f"{mm.HF_HOST}/api/models/{Q}": (200, _api(["README.md", "generation_config.json"], [B])),
        f"{mm.HF_HOST}/{Q}/raw/main/generation_config.json": (200, '{"temperature": 0.6, "top_p": 0.9}'),
        f"{mm.HF_HOST}/{Q}/raw/main/README.md": (200, "## Tips\nUse `Temperature=0.7`, `TopK=20`."),
        f"{mm.HF_HOST}/api/models/{B}": (200, _api(["README.md", "generation_config.json"])),
        f"{mm.HF_HOST}/{B}/raw/main/generation_config.json": (200, '{"top_k": 40, "min_p": 0}'),
        f"{mm.HF_HOST}/{B}/raw/main/README.md": (200, "## Best\n`MinP=0.05`, presence_penalty=1.5"),
    }
    doc = mm.build_meta(Q, _fake(routes))
    assert doc["repo"] == Q and doc["base_model"] == B and doc["error"] is None
    assert doc["sources"]["sidecar"] == {"found": True, "values": {"temperature": 0.6, "top-p": 0.9}}
    assert doc["sources"]["model_card"]["values"] == {"temperature": 0.7, "top-k": 20.0}
    assert doc["sources"]["model_card"]["context"] == "Tips"
    base = doc["sources"]["base_model"]
    assert base["found"] is True and base["repo"] == B
    assert base["values"] == {"top-k": 40.0, "min-p": 0.0, "presence-penalty": 1.5}
    assert base["via"] == {"top-k": "sidecar", "min-p": "sidecar", "presence-penalty": "model_card"}
    assert doc["suggestions"] == [
        {"key": "temperature", "value": 0.6, "source": "sidecar"},
        {"key": "top-p", "value": 0.9, "source": "sidecar"},
        {"key": "top-k", "value": 20.0, "source": "model_card"},
        {"key": "min-p", "value": 0.0, "source": "base_model"},
        {"key": "presence-penalty", "value": 1.5, "source": "base_model"},
    ]


def test_no_sidecar_in_listing_is_not_fetched_and_base_hop_once():
    routes = {
        f"{mm.HF_HOST}/api/models/{Q}": (200, _api(["README.md"], [B])),
        f"{mm.HF_HOST}/{Q}/raw/main/README.md": (200, "---\nbase_model: " + B + "\n---\nnothing"),
        f"{mm.HF_HOST}/api/models/{B}": (200, _api(["README.md"], ["Qwen/Deeper"])),
        f"{mm.HF_HOST}/{B}/raw/main/README.md": (200, "## S\ntemperature: 0.7"),
    }
    f = _fake(routes)
    doc = mm.build_meta(Q, f)
    assert doc["sources"]["sidecar"] == {"found": False, "values": {}}
    assert f"{mm.HF_HOST}/{Q}/raw/main/generation_config.json" not in f.calls
    assert not any("Qwen/Deeper" in u for u in f.calls)
    assert doc["suggestions"] == [{"key": "temperature", "value": 0.7, "source": "base_model"}]


def test_base_equal_to_repo_is_skipped_and_redirect_is_not_found():
    routes = {
        f"{mm.HF_HOST}/api/models/{Q}": (200, _api(["README.md", "generation_config.json"], [Q])),
        f"{mm.HF_HOST}/{Q}/raw/main/generation_config.json": (302, ""),
        f"{mm.HF_HOST}/{Q}/raw/main/README.md": (200, "temp 0.2"),
    }
    doc = mm.build_meta(Q, _fake(routes))
    assert doc["sources"]["sidecar"]["found"] is False
    assert doc["sources"]["base_model"] == {"found": False, "repo": None, "values": {}, "context": None, "via": {}}
    assert doc["suggestions"] == [{"key": "temperature", "value": 0.2, "source": "model_card"}]


def test_api_failure_sets_error():
    doc = mm.build_meta(Q, _fake({f"{mm.HF_HOST}/api/models/{Q}": (0, "")}))
    assert doc["error"] == "repo listing unavailable"
    assert doc["suggestions"] == []
    assert mm.ttl_for(doc, 7 * 86400) == mm.ERROR_TTL_S
    ok = mm.build_meta(Q, _fake({f"{mm.HF_HOST}/api/models/{Q}": (200, _api([]))}))
    assert ok["error"] is None and mm.ttl_for(ok, 7 * 86400) == 7 * 86400


def test_fetcher_headers_only_with_token():
    assert "Authorization" not in mm.Fetcher("").headers
    assert mm.Fetcher("hf_abc").headers["Authorization"] == "Bearer hf_abc"
    assert mm.Fetcher("").session.trust_env is False


def test_gated_repo_without_sources_reports_error():
    routes = {f"{mm.HF_HOST}/api/models/{Q}": (200, json.dumps(
        {"siblings": [], "cardData": {}, "gated": True}))}
    doc = mm.build_meta(Q, _fake(routes))
    assert doc["gated"] is True
    assert doc["error"] == "repo is gated; set a Hugging Face token"


def test_gated_repo_with_sources_is_fine():
    routes = {
        f"{mm.HF_HOST}/api/models/{Q}": (200, json.dumps(
            {"siblings": [{"rfilename": "README.md"}], "cardData": {}, "gated": True})),
        f"{mm.HF_HOST}/{Q}/raw/main/README.md": (200, "temperature: 0.5"),
    }
    doc = mm.build_meta(Q, _fake(routes))
    assert doc["gated"] is True
    assert doc["error"] is None


def test_fetcher_body_cap(monkeypatch):
    """Fetcher.get rejects bodies over the cap and passes through status codes."""
    class FakeRaw:
        def read(self, n, decode_content=True):
            return b"x" * n

    class FakeResp:
        status_code = 200
        raw = FakeRaw()

        def close(self):
            pass

    f = mm.Fetcher("")
    monkeypatch.setattr(f.session, "get", lambda *a, **k: FakeResp())
    assert f.get(f"{mm.HF_HOST}/a/b/raw/main/README.md") == (0, "")
    assert f.get("https://example.com/a/b") == (0, "")

    class FakeResp302:
        status_code = 302

        def close(self):
            pass

    monkeypatch.setattr(f.session, "get", lambda *a, **k: FakeResp302())
    assert f.get(f"{mm.HF_HOST}/a/b") == (302, "")


def test_cache_ttl_and_replace(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db", check_same_thread=False)
    cache = mm.MetaCache(lambda: conn)
    cache.init()
    assert cache.get(Q, 100) is None
    cache.put(Q, {"repo": Q, "error": None})
    assert cache.get(Q, 100)["repo"] == Q
    assert cache.get(Q, 0) is None
    cache.put(Q, {"repo": Q, "error": "x"})
    row = cache.get(Q, 100)
    assert row["error"] == "x" and row["fetched_ts"] <= int(time.time())
