"""#468: download -> register -> restart -> load provisioning."""
from __future__ import annotations

import report_card as rc

AGENT = {"agent_id": "a" * 32, "token": "tok", "bind_url": "http://h:9899"}
SRC = None


def setup_module(_m):
    global SRC
    SRC = rc.preset_source("small", "llama")


def _calls_recorder(monkeypatch, config=None, fail_on=None, models_after=None):
    """Record every agent call; serve canned replies."""
    calls = []

    def fake(agent, method, path, timeout=15, **kw):
        calls.append((method, path, kw.get("json")))
        if fail_on and path == fail_on:
            return None
        if path == "/llama/config" and method == "GET":
            return config if config is not None else {}
        if path.endswith("/models"):
            ids = models_after if models_after is not None else [SRC["model_id"]]
            return {"data": [{"id": i} for i in ids]}
        return {"ok": True}

    monkeypatch.setattr(rc, "_agent_json", fake)
    monkeypatch.setattr(rc, "_follow_download", lambda a, e, should_cancel=None: None)
    monkeypatch.setattr(rc._time, "sleep", lambda *_a: None)
    return calls


def test_provision_runs_download_register_restart_load_in_order(monkeypatch):
    calls = _calls_recorder(monkeypatch)
    events = []
    out = rc.provision_model(AGENT, "llama", SRC, events.append)
    assert out["status"] == "ready" and out["model"] == SRC["model_id"]
    paths = [p for _m, p, _b in calls]
    assert paths.index("/llama/download") < paths.index("/llama/config")
    assert paths.index("/llama/config") < paths.index("/llama/server/restart")
    assert "/llama/load" in paths
    phases = [e["phase"] for e in events]
    assert phases == ["download", "downloading", "register", "restart",
                      "waiting", "load"]


def test_download_request_carries_exact_lowercase_patterns(monkeypatch):
    # Regression: hf --include globs are case-sensitive, so filtering on the
    # uppercase quant tag downloaded nothing (refs/trees only, no blobs).
    calls = _calls_recorder(monkeypatch)
    rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    body = next(b for _m, p, b in calls if p == "/llama/download")
    assert body["repo"] == SRC["repo"]
    assert body["patterns"] == ["qwen2.5-1.5b-instruct-q4_k_m.gguf"]
    assert "include" not in body


def test_sharded_mid_pattern_covers_every_shard():
    src = rc.preset_source("mid", "llama")
    import fnmatch
    for shard in ("qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
                  "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf"):
        assert any(fnmatch.fnmatch(shard, pat) for pat in src["patterns"])


def test_register_preserves_existing_config_sections(monkeypatch):
    existing = {"Other/Model:Q8_0": {"ctx-size": "8192"}}
    calls = _calls_recorder(monkeypatch, config=dict(existing))
    rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    written = next(b for m, p, b in calls if p == "/llama/config" and m == "POST")
    assert "Other/Model:Q8_0" in written
    assert written["Other/Model:Q8_0"]["ctx-size"] == "8192"
    assert SRC["model_id"] in written
    assert written[SRC["model_id"]]["hf-file"] == SRC["file"]


def test_register_does_not_duplicate_an_existing_section(monkeypatch):
    calls = _calls_recorder(monkeypatch,
                            config={SRC["model_id"]: {"ctx-size": "16384"}})
    rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    written = next(b for m, p, b in calls if p == "/llama/config" and m == "POST")
    assert written[SRC["model_id"]]["ctx-size"] == "16384"


def test_provision_reports_restart_failure(monkeypatch):
    _calls_recorder(monkeypatch, fail_on="/llama/server/restart")
    out = rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    assert out["status"] == "error" and "restart" in out["error"]


def test_provision_reports_download_start_failure(monkeypatch):
    _calls_recorder(monkeypatch, fail_on="/llama/download")
    out = rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    assert out["status"] == "error" and "download" in out["error"]


def test_provision_errors_when_model_never_appears(monkeypatch):
    _calls_recorder(monkeypatch, models_after=[])
    monkeypatch.setattr(rc, "wait_for_model", lambda *a, **k: None)
    out = rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    assert out["status"] == "error" and "did not appear" in out["error"]


def test_provision_stops_when_cancelled(monkeypatch):
    _calls_recorder(monkeypatch)
    monkeypatch.setattr(rc, "_follow_download",
                        lambda a, e, should_cancel=None: rc._CANCELLED)
    out = rc.provision_model(AGENT, "llama", SRC, lambda _e: None,
                             should_cancel=lambda: True)
    assert out["status"] == "cancelled"


def test_a_download_error_named_cancelled_is_not_a_cancellation(monkeypatch):
    # Cancellation travels as a sentinel, so error text can never impersonate it.
    _calls_recorder(monkeypatch)
    monkeypatch.setattr(rc, "_follow_download",
                        lambda a, e, should_cancel=None: "cancelled")
    out = rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    assert out["status"] == "error" and out["error"] == "cancelled"


def test_lms_skips_the_llama_only_register_and_restart(monkeypatch):
    calls = _calls_recorder(monkeypatch)
    src = rc.preset_source("small", "lms")
    out = rc.provision_model(AGENT, "lms", src, lambda _e: None)
    assert out["status"] == "ready"
    paths = [p for _m, p, _b in calls]
    assert "/llama/server/restart" not in paths and "/llama/config" not in paths
    assert "/lms/download" in paths


def test_lms_download_sends_hf_url_and_quantization(monkeypatch):
    # Regression: LM Studio's download API takes catalog names or full HF
    # URLs; a bare "repo:QUANT" id fails with a kebab-case artifact error.
    calls = _calls_recorder(monkeypatch)
    src = rc.preset_source("small", "lms")
    rc.provision_model(AGENT, "lms", src, lambda _e: None)
    body = next(b for _m, p, b in calls if p == "/lms/download")
    assert body["model"] == "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF"
    assert body["quantization"] == "Q4_K_M"
    assert ":" not in body["model"].split("//", 1)[1]


# ── download stream follower ─────────────────────────────────────────

def test_follow_download_forwards_lines_and_completes():
    msgs = [{"type": "start"}, {"type": "line", "text": "12%"},
            {"type": "line", "text": "100%"}, {"type": "done"}]
    seen = []
    err = rc._follow_download(AGENT, seen.append, stream_lines=lambda _a: iter(msgs))
    assert err is None
    assert [e["text"] for e in seen] == ["12%", "100%"]
    assert all(e["phase"] == "download_progress" for e in seen)


def test_follow_download_surfaces_a_reported_error():
    msgs = [{"type": "done", "error": "hf: 401 unauthorized"}]
    err = rc._follow_download(AGENT, lambda _e: None,
                              stream_lines=lambda _a: iter(msgs))
    assert "401" in err


def test_follow_download_errors_if_stream_ends_early():
    err = rc._follow_download(AGENT, lambda _e: None,
                              stream_lines=lambda _a: iter([{"type": "line",
                                                             "text": "x"}]))
    assert "without completing" in err


def test_follow_download_cancels_the_agent_download(monkeypatch):
    calls = []
    monkeypatch.setattr(rc, "_agent_json",
                        lambda a, m, p, timeout=15, **k: calls.append(p))
    err = rc._follow_download(AGENT, lambda _e: None, should_cancel=lambda: True,
                              stream_lines=lambda _a: iter([{"type": "line",
                                                             "text": "5%"}]))
    assert err is rc._CANCELLED and "/llama/download/cancel" in calls


# ── wait_for_model ───────────────────────────────────────────────────

def test_wait_for_model_returns_the_matching_id(monkeypatch):
    monkeypatch.setattr(rc, "_agent_json",
                        lambda a, m, p, timeout=15, **k:
                            {"data": [{"id": SRC["model_id"]}]})
    got = rc.wait_for_model(AGENT, "llama", SRC, now=lambda: 0.0,
                            sleep=lambda _s: None)
    assert got == SRC["model_id"]


def test_wait_for_model_gives_up_after_the_timeout(monkeypatch):
    monkeypatch.setattr(rc, "_agent_json",
                        lambda a, m, p, timeout=15, **k: {"data": []})
    clock = {"t": 0.0}

    def now():
        clock["t"] += 10.0
        return clock["t"]

    assert rc.wait_for_model(AGENT, "llama", SRC, timeout_s=30.0, now=now,
                             sleep=lambda _s: None) is None


def test_wait_for_model_aborts_on_cancel(monkeypatch):
    monkeypatch.setattr(rc, "_agent_json",
                        lambda a, m, p, timeout=15, **k: {"data": []})
    assert rc.wait_for_model(AGENT, "llama", SRC, should_cancel=lambda: True,
                             now=lambda: 0.0, sleep=lambda _s: None) is None


# ── agents report expected failures as HTTP 200 + {"ok": false} ──────

def _ok_false(monkeypatch, failing_path, payload):
    calls = []

    def fake(agent, method, path, timeout=15, **kw):
        calls.append(path)
        if path == failing_path:
            return payload
        if path == "/llama/config" and method == "GET":
            return {}
        if path.endswith("/models"):
            return {"data": [{"id": SRC["model_id"]}]}
        return {"ok": True}

    monkeypatch.setattr(rc, "_agent_json", fake)
    monkeypatch.setattr(rc, "_follow_download", lambda a, e, should_cancel=None: None)
    monkeypatch.setattr(rc._time, "sleep", lambda *_a: None)
    return calls


def test_config_write_failure_is_not_treated_as_registered(monkeypatch):
    # {"ok": false} is a truthy dict; a bare bool() check called it success
    # and restarted llama.cpp against an unwritten config.
    calls = _ok_false(monkeypatch, "/llama/config",
                      {"ok": False, "error": "Permission denied"})
    out = rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    assert out["status"] == "error"
    assert "Permission denied" in out["error"]
    assert "/llama/server/restart" not in calls


def test_restart_failure_surfaces_systemctl_stderr(monkeypatch):
    _ok_false(monkeypatch, "/llama/server/restart",
              {"ok": False, "error": "Job for llama.service failed"})
    out = rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    assert out["status"] == "error" and "llama.service" in out["error"]


def test_lms_download_failure_is_not_treated_as_started(monkeypatch):
    src = rc.preset_source("small", "lms")
    calls = _ok_false(monkeypatch, "/lms/download",
                      {"ok": False, "response": {"error": "model not found"}})
    out = rc.provision_model(AGENT, "lms", src, lambda _e: None)
    assert out["status"] == "error" and "model not found" in out["error"]
    assert "/lms/load" not in calls


def test_load_failure_surfaces_the_agent_error(monkeypatch):
    _ok_false(monkeypatch, "/llama/load", {"ok": False, "error": "OOM"})
    out = rc.provision_model(AGENT, "llama", SRC, lambda _e: None)
    assert out["status"] == "error" and "OOM" in out["error"]


def test_agent_call_maps_every_failure_shape(monkeypatch):
    cases = [
        (None, False),
        ({"ok": False, "error": "boom"}, False),
        ({"ok": False, "response": {"error": "nested"}}, False),
        ({"ok": True}, True),
        ({"data": []}, True),        # no ok key -> not a failure signal
    ]
    for payload, expect_ok in cases:
        monkeypatch.setattr(rc, "_agent_json",
                            lambda a, m, p, timeout=15, **k: payload)
        ok, err = rc._agent_call(AGENT, "POST", "/x")
        assert ok is expect_ok, payload
        assert (err is None) is expect_ok, payload
