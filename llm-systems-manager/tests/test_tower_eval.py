"""#1047 Tower eval: cases, scoring through the real loop, store, jobs, the get-model job and the routes."""
from __future__ import annotations

import json
import sqlite3
import types

import pytest

import jobs as _jobs
import tower
import tower_eval as te
import tower_tools as tt

ENTRIES = [{"id": "qwen3-14b", "provider": "llama", "status": {"value": "loaded"}, "hosts": ["box"], "agent_ids": ["a1"]},
           {"id": "gemma-3-12b", "provider": "llama", "status": {"value": "unloaded"}, "hosts": [], "agent_ids": []}]
MODEL = {"model": "qwen3-14b", "provider": "llama", "hosts": ["box"], "agent_ids": ["a1"]}


def _cfg(**over):
    d = dict(enabled=True, model="auto", tool_mode="prompt", capabilities="read", off_topic="refuse", fallback=True,
             disabled_tools=[], max_tool_calls=4, max_tokens=256, temperature=0.2, request_timeout_s=0, history_days=30,
             report_violations=True)
    d.update(over)
    return types.SimpleNamespace(**d)


def _registry(hosts=("box", "box-two")):
    rows = [{"hostname": h, "primary": ["llama"] if i == 0 else []} for i, h in enumerate(hosts)]
    return tt.build_registry({
        "host": lambda n, section="all": {"hostname": n, "ram": {"used_pct": 41}},
        "hosts": lambda *a, **k: rows, "host_history": lambda *a, **k: {}, "models": lambda *a, **k: [{"id": "qwen3-14b"}],
        "alarms": lambda *a, **k: [], "alert": lambda a: None, "alarm_search": lambda a: {}, "alarm_history": lambda *a, **k: {},
        "energy": lambda *a, **k: {"kwh": 1.2}, "flow": lambda: {}, "runs": lambda *a, **k: [], "speed": lambda m: [],
        "health": lambda: {}, "log_tail": lambda *a, **k: [], "config_get": lambda p: {}, "help": lambda t: "",
        "audit": lambda *a, **k: []})


def _stream(replies):
    """Scripted complete_stream: each reply is text, or a native call dict {name, arguments}."""
    it = iter(replies)
    seen = []

    def cs(body, *, label, **kw):
        seen.append(body)
        r = next(it)
        if isinstance(r, dict):
            yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c0", "function": r}]}}]}
        else:
            yield {"choices": [{"delta": {"content": r}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    cs.seen = seen
    return cs


def _fenced(name, args=None):
    return "```tool\n" + json.dumps({"name": name, "args": args or {}}) + "\n```"


# ── cases ──

def test_quant_and_ambiguous_token():
    assert te.quant_of("unsloth/Qwen3.5-9B-GGUF:Q4_K_M") == "Q4_K_M"
    assert te.quant_of("gemma-3-12b-it-iq3_xs.gguf") == "IQ3_XS"
    assert te.quant_of("qwen3-14b") is None
    assert te.ambiguous_token(["llm-agent-llama1", "llm-agent-llama2", "mac-mini"]) == "llm-agent-llama"
    assert te.ambiguous_token(["box", "mac"]) is None
    assert te.ambiguous_token(["box", "box"]) is None
    assert te.ambiguous_token(["alpha", "alphabet"]) is None


def test_build_cases_follow_the_fleet():
    plain = te.build_cases([], {})
    assert [c["id"] for c in plain] == ["hosts", "models", "alarms", "energy"]
    full = te.build_cases(["box", "box-two"], {"primary": "box", "primary llama": "box"}, "bo")
    ids = [c["id"] for c in full]
    assert ids == ["hosts", "models", "alarms", "energy", "host", "rank", "timer", "proposal", "question"]
    host = next(c for c in full if c["id"] == "host")
    assert "primary llama" in host["prompt"] and host["arg"] == "host"
    assert "bo" in next(c for c in full if c["id"] == "question")["prompt"]
    assert all(c["answer"] in ("text", "question", "timer", "proposal") for c in full)


# ── scoring ──

def _events(*evs):
    return [dict(e) for e in evs]


def test_score_text_case_passes_on_expected_tool_and_plain_answer():
    case = te.build_cases([], {})[0]
    ev = _events({"event": "tool", "name": "hosts_overview", "ok": True, "args": {}},
                 {"event": "delta", "text": "box is online."}, {"event": "done", "ok": True, "calls": 1})
    s = te.score_case(case, ev, {"ok": True, "calls": 1}, 900)
    assert s["passed"] and s["calls"] == 1 and s["detail"] == "ok" and s["ms"] == 900 and s["tools"] == ["hosts_overview"]


def test_score_text_case_fails_on_wrong_tool_canned_answer_or_card():
    case = te.build_cases([], {})[0]
    wrong = _events({"event": "tool", "name": "alarms", "ok": True, "args": {}}, {"event": "delta", "text": "none"},
                    {"event": "done", "ok": True, "calls": 1})
    s = te.score_case(case, wrong, {"ok": True, "calls": 1}, 10)
    assert not s["passed"] and "called alarms instead of hosts_overview" in s["detail"]
    canned = _events({"event": "tool", "name": "hosts_overview", "ok": True, "args": {}},
                     {"event": "delta", "text": tower._FALLBACK_GENERIC}, {"event": "done", "ok": True, "calls": 1})
    assert "canned answer" in te.score_case(case, canned, {"ok": True, "calls": 1}, 10)["detail"]
    asked = _events({"event": "question", "action_id": "q1", "tool": "ask_operator", "questions": []},
                    {"event": "tool", "name": "hosts_overview", "ok": True, "args": {}},
                    {"event": "delta", "text": "ok"}, {"event": "done", "ok": True, "calls": 1})
    assert "asked a question instead" in te.score_case(case, asked, {"ok": True, "calls": 1}, 10)["detail"]
    nothing = _events({"event": "delta", "text": "I cannot tell."}, {"event": "done", "ok": True, "calls": 0})
    assert "no tool call" in te.score_case(case, nothing, {"ok": True, "calls": 0}, 10)["detail"]
    corrected = _events({"event": "tool", "name": "hosts_overview", "ok": True, "args": {}}, {"event": "delta", "text": "fine"},
                        {"event": "done", "ok": True, "calls": 1, "note": "prose tool call corrected; retried after a length stop"})
    s = te.score_case(case, corrected, {"ok": True, "calls": 1}, 10)
    assert s["passed"] and s["corrections"] == 1 and s["retries"] == 1


def test_score_proposal_timer_and_question_kinds():
    cases = {c["id"]: c for c in te.build_cases(["box", "box-two"], {}, "bo")}
    prop = _events({"event": "confirm", "action_id": "a1", "tool": "wake_server", "args": {"host": "box"}},
                   {"event": "action", "action_id": "a1", "status": "denied"},
                   {"event": "delta", "text": "The wake was denied."}, {"event": "done", "ok": True, "calls": 1})
    assert te.score_case(cases["proposal"], prop, {"ok": True, "calls": 1}, 5)["passed"]
    no_card = _events({"event": "delta", "text": "I woke it."}, {"event": "done", "ok": True, "calls": 0})
    assert "no tool call" in te.score_case(cases["proposal"], no_card, {"ok": True, "calls": 0}, 5)["detail"]
    timer = _events({"event": "tool", "name": "schedule", "ok": True, "args": {"every_s": 300}},
                    {"event": "delta", "text": "Scheduled."}, {"event": "done", "ok": True, "calls": 1})
    assert te.score_case(cases["timer"], timer, {"ok": True, "calls": 1}, 5)["passed"]
    q = _events({"event": "question", "action_id": "q1", "tool": "ask_operator", "questions": [{"question": "Which host?", "choices": ["box", "box-two"]}]},
                {"event": "tool", "name": "host_detail", "ok": True, "args": {"host": "box"}},
                {"event": "delta", "text": "41 %"}, {"event": "done", "ok": True, "calls": 1})
    assert te.score_case(cases["question"], q, {"ok": True, "calls": 1}, 5)["passed"]
    unasked = _events({"event": "tool", "name": "host_detail", "ok": True, "args": {"host": "box"}},
                      {"event": "delta", "text": "41 %"}, {"event": "done", "ok": True, "calls": 1})
    assert "no question card" in te.score_case(cases["question"], unasked, {"ok": True, "calls": 1}, 5)["detail"]
    missing_arg = _events({"event": "tool", "name": "host_detail", "ok": True, "args": {}},
                          {"event": "delta", "text": "41 %"}, {"event": "done", "ok": True, "calls": 1})
    assert "host argument missing" in te.score_case(cases["host"], missing_arg, {"ok": True, "calls": 1}, 5)["detail"]


# ── through the real loop ──

def test_run_case_drives_the_loop_and_pins_the_eval_view():
    case = te.build_cases(["box"], {})[0]
    cs = _stream([_fenced("hosts_overview"), "box is online."])
    s = te.run_case(case, cfg=te.EvalView(_cfg(), "qwen3-14b"), registry=_registry(), complete_stream=cs, model=MODEL,
                    cancelled=lambda: False)
    assert s["passed"] and s["calls"] == 1 and s["answer"] == "box is online."
    assert "wake_server" in cs.seen[0]["messages"][0]["content"]   # act tools are in the eval's catalog


def test_run_case_proposal_is_denied_and_timer_is_recorded():
    cases = {c["id"]: c for c in te.build_cases(["box"], {})}
    cs = _stream([_fenced("wake_server", {"host": "box"}), "The operator denied the wake."])
    s = te.run_case(cases["proposal"], cfg=te.EvalView(_cfg(), "qwen3-14b"), registry=_registry(), complete_stream=cs,
                    model=MODEL, cancelled=lambda: False)
    assert s["passed"], s["detail"]
    assert "denied by the operator" in cs.seen[1]["messages"][-1]["content"]
    cs = _stream([_fenced("schedule", {"label": "GPU temp", "every_s": 300, "for_s": 900, "host": "box", "metric": "gpu_temp_c"}),
                  "Scheduled: GPU temperature on box every 5 minutes."])
    s = te.run_case(cases["timer"], cfg=te.EvalView(_cfg(), "qwen3-14b"), registry=_registry(), complete_stream=cs,
                    model=MODEL, cancelled=lambda: False)
    assert s["passed"], s["detail"]
    assert "nothing was scheduled" in cs.seen[1]["messages"][-1]["content"]


def test_run_case_question_card_is_auto_answered():
    cases = {c["id"]: c for c in te.build_cases(["box-one", "box-two"], {}, "box")}
    cs = _stream([_fenced("host_detail", {"host": "box"}), "box-one runs at 61 °C."])
    s = te.run_case(cases["question"], cfg=te.EvalView(_cfg(), "qwen3-14b"), registry=_registry(("box-one", "box-two")),
                    complete_stream=cs, model=MODEL, cancelled=lambda: False)
    assert s["passed"], s["detail"]
    assert s["tools"] == ["host_detail"]


# ── store ──

def test_store_keeps_latest_per_model_and_caps_history():
    st = te.EvalStore(":memory:")
    for i in range(te.EVAL_KEEP + 3):
        st.save(te.summarize(MODEL, [{"id": "hosts", "title": "Plain read", "passed": i % 2 == 0, "calls": 1, "corrections": 0,
                                      "retries": 0, "ms": 5, "detail": "ok"}], quant="Q4_K_M", server="llama.cpp b1",
                             tool_mode="auto", grade="native", ms=5, actor="alice", at=1000.0 + i))
    st.save(te.summarize({"model": "gemma-3-12b", "provider": "llama", "hosts": []}, [], quant=None, server=None,
                         tool_mode="auto", grade=None, ms=1, actor="alice", at=5.0))
    assert len(st.history("qwen3-14b", 100)) == te.EVAL_KEEP
    latest = st.latest()
    assert [r["model"] for r in latest] == ["qwen3-14b", "gemma-3-12b"]
    assert latest[0]["at"] == 1000.0 + te.EVAL_KEEP + 2 and latest[0]["quant"] == "Q4_K_M"
    row = st.get(latest[0]["id"])
    assert row["cases"][0]["title"] == "Plain read" and row["hosts"] == ["box"]
    b = te.brief(row)
    assert "prompt" not in b["cases"][0] and b["score_pct"] == row["score_pct"]
    assert st.latest_for("nope") is None


# ── evaluator + jobs ──

def _service():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    _jobs.init_table(conn)
    return _jobs.Service(_jobs.Store(lambda: conn), cfg=lambda: types.SimpleNamespace(workers=2, history_days=30), inline=True)


def _evaluator(replies, entries=None, **kw):
    svc = _service()
    ev = te.Evaluator(service=svc, store=te.EvalStore(":memory:"), cfg=_cfg, registry_factory=lambda: _registry(("box",)),
                      complete_stream=_stream(replies), entries=lambda: entries if entries is not None else ENTRIES,
                      server_of=lambda m: "llama.cpp b6400", now=lambda: 1234.0, **kw)
    return ev, svc


def test_eval_job_runs_every_case_and_stores_the_result():
    cases = te.build_cases(["box"], {})
    n = len(cases)
    want = sum(1 for c in cases if "hosts_overview" in c["tools"] and c["answer"] == "text")
    replies = [_fenced("hosts_overview"), "answer"] * n
    recorded = []
    ev, svc = _evaluator(replies, record_run=recorded.append)
    row, err = ev.start("qwen3-14b", "alice")
    assert err is None and row["kind"] == te.KIND_EVAL and row["label"] == "Tower eval · qwen3-14b"
    assert ev.start("qwen3-14b", "alice") == (None, "busy")
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "done", done
    res = ev.store.latest_for("qwen3-14b")
    assert res["total"] == n and res["passed"] == want == 4 and res["server"] == "llama.cpp b6400" and res["at"] == 1234.0
    assert res["cases"][0]["passed"] and not res["cases"][2]["passed"] and res["calls"] == n
    assert done["result"]["passed"] == want and "%d/%d passed" % (want, n) == done["message"]
    assert recorded and recorded[0]["id"] == res["id"]
    assert ev.live() is None


def test_eval_start_refuses_a_model_that_is_not_loaded_and_cancel_stores_nothing():
    ev, svc = _evaluator([])
    assert ev.start("gemma-3-12b", "alice") == (None, "no_model")
    assert ev.start("missing", "alice") == (None, "no_model")
    assert ev.run_eval(MODEL, cancelled=lambda: True) is None and ev.store.latest() == []


def test_eval_default_model_is_the_resolved_one_and_checks_are_used():
    calls = []

    class Chk:
        def run(self, m):
            calls.append(m["model"]); return {"model": m["model"], "grade": "fenced"}
        def get(self, mid): return {"model": mid, "grade": "fenced"}
    n = len(te.build_cases(["box"], {}))
    ev, svc = _evaluator(["plain answer"] * n, checks=Chk())
    row, err = ev.start(None, "bob")
    assert err is None and row["spec"]["model"] == "qwen3-14b"
    svc.tick()
    res = ev.store.latest_for("qwen3-14b")
    assert calls == ["qwen3-14b"] and res["grade"] == "fenced" and res["passed"] == 0


# ── curated list + get-model job ──

def test_curated_list_ships_valid_entries():
    models = te.load_curated()
    assert len(models) >= 5
    for m in models:
        assert m["model_id"] == f"{m['repo']}:{m['quant']}" and m["file"].endswith(".gguf")
        assert m["quant"].lower() in m["file"].lower()
        assert m["tier_gb"] in (6, 8, 12, 16, 24, 32, 48) and m["expected"] in ("good", "high") and m["size_gb"] < m["tier_gb"]
        assert (m["params_b"] <= 14) or "MoE" in m["name"], m["key"]   # capped: 14B dense, MoE excepted
        assert bool(m.get("small")) == (m["params_b"] < 7 and "E4B" not in m["name"]), m["key"]
    assert len({m["key"] for m in models}) == len(models)
    assert [m["tier_gb"] for m in models] == sorted(m["tier_gb"] for m in models)


class _Resp:
    def __init__(self, body=None, lines=None, status=200):
        self._body, self._lines, self.status_code = body, lines or [], status
        self.ok = status < 400

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_lines(self):
        for l in self._lines:
            yield l.encode()

    def close(self):
        pass


def _fake_agent_request(log, sections, lines, load_ok=True, listed=None):
    """`listed` counts /llama/models polls; the new model is listed from the second poll on."""
    listed = listed if listed is not None else {"n": 0}

    def req(method, agent, path, **kw):
        log.append((method, path, kw.get("json")))
        if path == "/llama/models":
            listed["n"] += 1
            ids = ["old-model"] + (["unsloth/Qwen3.5-9B-GGUF:Q4_K_M"] if listed["n"] >= 2 else [])
            return _Resp({"data": [{"id": i, "status": {"value": "unloaded"}} for i in ids]}), [], None
        if path == "/llama/download":
            return _Resp({"ok": True}), [], None
        if path == "/llama/download/stream":
            return _Resp(lines=lines), [], None
        if path == "/llama/config" and method == "GET":
            return _Resp(dict(sections)), [], None
        if path == "/llama/config":
            sections.clear(); sections.update(kw.get("json") or {})
            return _Resp({"ok": True}), [], None
        if path in ("/llama/server/restart", "/llama/download/cancel", "/llama/cache/rm"):
            return _Resp({"ok": True}), [], None
        if path == "/llama/cache/gguf":
            return _Resp({"ok": True, "data": []}), [], None
        if path == "/llama/load":
            return _Resp({"ok": load_ok} if load_ok else {"ok": False, "error": "no such model"}), [], None
        raise AssertionError(path)
    return req


def test_get_model_job_downloads_registers_loads_checks_and_evals(monkeypatch):
    log, sections = [], {"__DEFAULTS__": {"x": "1"}, "old-model": {"ctx-size": "4096"}}
    lines = ['data: {"type": "start", "cmd": "hf download"}', 'data: {"type": "line", "text": "Qwen3.5-9B-Q4_K_M.gguf: 40%", "progress": true}',
             '', 'data: {"type": "line", "text": "Qwen3.5-9B-Q4_K_M.gguf: 100%", "progress": true}', 'data: {"type": "done", "ok": true, "rc": 0}']
    entries = list(ENTRIES)
    agent = {"agent_id": "a1", "hostname": "box", "token": "t", "status": "approved"}
    named, profiles = [], []
    ev, svc = _evaluator([], entries=entries, agent_request=_fake_agent_request(log, sections, lines),
                         download_hosts=lambda: [{"provider": "llama", "host": "box", "agent_id": "a1", "primary": True, "agent": agent}],
                         refresh_index=lambda w: None, profile_put=lambda *a: profiles.append(a), alias_set=lambda m, a: named.append((m, a)))
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    import agent_registry
    monkeypatch.setattr(agent_registry, "resolve_agent_by_id", lambda aid, capability=None: agent if aid == "a1" else None)
    states = []
    real_set_state = svc.set_state
    monkeypatch.setattr(svc, "set_state", lambda jid, st: (states.append(dict(st)), real_set_state(jid, st))[1])

    def fake_eval(model, *, cancelled=lambda: False, progress=None, actor="eval"):
        r = te.summarize(model, [{"id": "hosts", "title": "Plain read", "passed": True, "calls": 1, "corrections": 0, "retries": 0,
                                  "ms": 3, "detail": "ok"}], quant="Q4_K_M", server="llama.cpp", tool_mode="auto", grade="native",
                         ms=3, actor=actor, at=9.0)
        ev.store.save(r)
        return r
    monkeypatch.setattr(ev, "run_eval", fake_eval)
    mid = "unsloth/Qwen3.5-9B-GGUF:Q4_K_M"

    fake = _fake_agent_request(log, sections, lines)

    def req_then_resident(method, agent_, path, **kw):
        out = fake(method, agent_, path, **kw)
        if path == "/llama/load":
            entries.append({"id": mid, "provider": "llama", "status": {"value": "loaded"}, "hosts": ["box"], "agent_ids": ["a1"]})
        return out
    ev._agent_request = req_then_resident
    monkeypatch.setattr(te, "SERVER_WAIT_S", 5.0)
    row, err = ev.start_get("qwen35-9b-q4", "alice")
    assert err is None and row["spec"]["model_id"] == mid and row["label"].startswith("Download Tower model · Qwen3.5 9B")
    assert row["spec"]["provider"] == "llama" and row["spec"]["host"] == "box"
    assert ev.start("qwen3-14b", "alice") == (None, "busy")
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "done", done
    assert done["result"]["pin_offer"] and done["result"]["model"] == mid and done["result"]["eval"]["passed"] == 1
    paths = [p for _m, p, _j in log]
    assert paths == ["/llama/download", "/llama/download/stream", "/llama/config", "/llama/config", "/llama/server/restart",
                     "/llama/models", "/llama/models", "/llama/load"]
    assert any(s.get("phase") == "restart" for s in states)
    assert sections[mid] == te.NEW_MODEL_INI and "__DEFAULTS__" not in sections and "old-model" in sections
    assert te.NEW_MODEL_INI["ubatch-size"] == "1024" and te.NEW_MODEL_INI["temperature"] == "0.2"
    assert te.NEW_MODEL_INI["reasoning"] == "on" and te.NEW_MODEL_INI["reasoning-budget"] == "2048" and te.NEW_MODEL_INI["ctx-size"] == "32768"
    assert profiles == [("a1", mid, "tower", te.NEW_MODEL_INI)] and named == [(mid, "Tower Model")]
    assert any(s.get("phase") == "download" and s.get("pct") == 40 for s in states)
    assert any(s.get("phase") == "load" for s in states) and any(s.get("phase") == "check" for s in states)
    view = ev.curated_view()
    got = next(m for m in view["models"] if m["key"] == "qwen35-9b-q4")
    assert got["present"] and got["loaded"] and got["eval"]["passed"] == 1 and view["host"] == "box"
    assert view["hosts"] == [{"provider": "llama", "label": "llama.cpp", "host": "box", "agent_id": "a1", "primary": True}]
    assert "agent" not in view["hosts"][0] and '"token": "t"' not in json.dumps(view)
    assert {"id": mid, "provider": "llama", "hosts": ["box"], "loaded": True} in view["index"]
    entries.append({"id": "idle-model", "provider": "llama", "status": {"value": "unloaded"}, "hosts": [], "catalog_hosts": ["box"]})
    assert {"id": "idle-model", "provider": "llama", "hosts": ["box"], "loaded": False} in ev.curated_view()["index"]


def test_get_model_job_fails_cleanly_on_a_download_error(monkeypatch):
    log, sections = [], {}
    lines = ['data: {"type": "done", "ok": false, "rc": 1}']
    agent = {"agent_id": "a1", "hostname": "box", "token": "t", "status": "approved"}
    ev, svc = _evaluator([], agent_request=_fake_agent_request(log, sections, lines),
                         download_hosts=lambda: [{"provider": "llama", "host": "box", "agent_id": "a1", "primary": True, "agent": agent}])
    import agent_registry
    monkeypatch.setattr(agent_registry, "resolve_agent_by_id", lambda aid, capability=None: agent)
    row, err = ev.start_get("qwen35-9b-q4", "alice")
    assert err is None
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "failed" and done["message"] == "download failed: hf exited 1; the partial download was removed from the cache"
    assert [p for _m, p, _j in log] == ["/llama/download", "/llama/download/stream", "/llama/cache/gguf", "/llama/cache/rm"]
    assert ev.start_get("nope", "alice") == (None, "unknown model")
    assert ev.start_get("qwen35-9b-q4", "alice", "zz") == (None, "unknown host")
    ev._download_hosts = lambda: []
    assert ev.start_get("qwen35-9b-q4", "alice") == (None, "no llama.cpp or LM Studio host to download to")


def test_lm_studio_download_reports_a_failed_job_at_once():
    ev, _svc = _evaluator([], agent_request=lambda *a, **k: (_Resp({"ok": True, "response": {"status": "failed", "error": "no space"}}), [], None))
    assert ev._download_lms({"token": "t"}, {"repo": "org/repo", "quant": "Q4_K_M"}, lambda: False, lambda **k: None) == ("", "no space")


def test_lm_studio_quant_names_drop_the_unsloth_prefix():
    assert te.Evaluator._lms_quant("UD-Q4_K_M") == "Q4_K_M" and te.Evaluator._lms_quant("Q6_K") == "Q6_K"


def _lms_status_agent(log, statuses, listed_after):
    """Fake agent: a download that returns a job id, then the given status replies, then a model list."""
    calls = {"status": 0, "models": 0}

    def req(method, agent_, path, **kw):
        log.append((method, path, kw.get("json")))
        if path == "/lms/download":
            return _Resp({"ok": True, "response": {"job_id": "job_1", "status": "downloading"}}), [], None
        if path.startswith("/lms/download/status/"):
            i = min(calls["status"], len(statuses) - 1)
            calls["status"] += 1
            st = statuses[i]
            if st == "route-missing":
                r = _Resp({"detail": "Not Found"}); r.ok, r.status_code = False, 404
                return r, [], None
            return _Resp(st), [], None
        if path == "/lms/models":
            calls["models"] += 1
            return _Resp({"data": [{"id": "qwen3.5-9b"}] if calls["models"] >= listed_after else []}), [], None
        raise AssertionError(path)
    return req


def test_lm_studio_download_follows_the_job_status(monkeypatch):
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    spec = {"repo": "unsloth/Qwen3.5-9B-GGUF", "quant": "UD-Q4_K_M"}
    log, seen = [], []
    st = [{"ok": True, "http": 200, "response": {"status": "downloading", "total_size_bytes": 1000, "downloaded_bytes": 500}},
          {"ok": True, "http": 200, "response": {"status": "completed"}}]
    ev, _svc = _evaluator([], agent_request=_lms_status_agent(log, st, 1))
    assert ev._download_lms({"token": "t"}, spec, lambda: False, lambda **k: seen.append(k)) == ("qwen3.5-9b", None)
    assert log[0][2] == {"model": "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF", "quantization": "Q4_K_M"}
    assert any(k.get("pct") == 50 and "0.0 of 0.0 GB" in k.get("line", "") for k in seen)
    assert [p for _m, p, _j in log].count("/lms/models") == 1
    # failed in LM Studio, or cancelled there (the job vanishes): the manager job ends at once
    ev, _svc = _evaluator([], agent_request=_lms_status_agent([], [{"ok": True, "http": 200, "response": {"status": "failed", "error": "disk full"}}], 9))
    assert ev._download_lms({"token": "t"}, spec, lambda: False, lambda **k: None) == ("", "disk full")
    ev, _svc = _evaluator([], agent_request=_lms_status_agent([], [{"ok": False, "http": 404, "response": {}}], 9))
    assert ev._download_lms({"token": "t"}, spec, lambda: False, lambda **k: None)[1].startswith("the download is gone from LM Studio")
    # an agent without the status route: back to polling the model list
    log = []
    ev, _svc = _evaluator([], agent_request=_lms_status_agent(log, ["route-missing"], 2))
    assert ev._download_lms({"token": "t"}, spec, lambda: False, lambda **k: None) == ("qwen3.5-9b", None)
    assert [p for _m, p, _j in log].count("/lms/download/status/job_1") == 1 and [p for _m, p, _j in log].count("/lms/models") == 2
    # Stop: the job ends here, the message says LM Studio keeps going
    ev, _svc = _evaluator([], agent_request=_lms_status_agent([], [], 9))
    assert ev._download_lms({"token": "t"}, spec, lambda: True, lambda **k: None) == ("", "cancelled; LM Studio keeps downloading, cancel it there")


def _stopped_llama_agent(log, cached, rm_replies):
    def req(method, agent_, path, **kw):
        log.append((method, path, kw.get("json")))
        if path == "/llama/download":
            return _Resp({"ok": True}), [], None
        if path == "/llama/download/stream":
            return _Resp(lines=['data: {"type": "line", "text": "x: 10%", "progress": true}']), [], None
        if path == "/llama/download/cancel":
            return _Resp({"ok": True}), [], None
        if path == "/llama/cache/gguf":
            return _Resp({"ok": True, "data": cached}), [], None
        if path == "/llama/cache/rm":
            code = rm_replies.pop(0)
            r = _Resp({"ok": code == 200} if code == 200 else {"detail": "busy"})
            r.ok, r.status_code = code == 200, code
            return r, [], None
        raise AssertionError(path)
    return req


def test_a_stopped_llama_download_is_removed_from_the_cache_unless_the_repo_has_finished_files(monkeypatch):
    import types as _t
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    agent = {"agent_id": "a1", "hostname": "box", "token": "t", "status": "approved"}
    import agent_registry
    monkeypatch.setattr(agent_registry, "resolve_agent_by_id", lambda aid, capability=None: agent)
    hosts = lambda: [{"provider": "llama", "host": "box", "agent_id": "a1", "primary": True, "agent": agent}]  # noqa: E731
    log = []
    ev, svc = _evaluator([], agent_request=_stopped_llama_agent(log, [], [409, 200]), download_hosts=hosts)
    row, err = ev.start_get("qwen35-9b-q4", "alice")
    assert err is None
    out = ev._run_get_job(_t.SimpleNamespace(id=row["id"], spec=row["spec"], cancelled=lambda: True, user="alice"))
    assert out.message == "stopped; the partial download was removed from the cache" and out.alert is False
    paths = [p for _m, p, _j in log]
    assert paths == ["/llama/download", "/llama/download/stream", "/llama/download/cancel", "/llama/cache/gguf", "/llama/cache/rm", "/llama/cache/rm"]
    assert log[-1][2] == {"repo": "unsloth/Qwen3.5-9B-GGUF"}
    # the repo already holds a finished quant: nothing is removed
    log = []
    ev, svc = _evaluator([], agent_request=_stopped_llama_agent(log, [{"repo": "unsloth/Qwen3.5-9B-GGUF", "file": "Qwen3.5-9B-Q6_K.gguf"}], []),
                         download_hosts=hosts)
    row, _err = ev.start_get("qwen35-9b-q4", "alice")
    out = ev._run_get_job(_t.SimpleNamespace(id=row["id"], spec=row["spec"], cancelled=lambda: True, user="alice"))
    assert out.message.startswith("stopped; unsloth/Qwen3.5-9B-GGUF keeps its finished files")
    assert "/llama/cache/rm" not in [p for _m, p, _j in log]
    # the note lands on the cancelled row
    svc.cancel(row["id"], actor="alice")
    assert svc.annotate(row["id"], "stopped; note") and svc.get(row["id"])["message"] == "stopped; note"


def test_hosts_list_every_capable_host_primaries_first_and_errors_name_the_provider_reason():
    a = {"agent_id": "a1", "hostname": "box", "token": "t"}
    b = {"agent_id": "a2", "hostname": "mac", "token": "t"}
    c = {"agent_id": "a3", "hostname": "alpha", "token": "t"}
    d = {"agent_id": "a4", "hostname": "notoken"}
    ev, _svc = _evaluator([], download_hosts=lambda: [
        {"provider": "lms", "host": "mac", "agent_id": "a2", "primary": True, "agent": b},
        {"provider": "llama", "host": "alpha", "agent_id": "a3", "primary": False, "agent": c},
        {"provider": "llama", "host": "box", "agent_id": "a1", "primary": True, "agent": a},
        {"provider": "llama", "host": "notoken", "agent_id": "a4", "primary": False, "agent": d},
        {"provider": "vllm", "host": "v", "agent_id": "a5", "primary": True, "agent": a}])
    assert [(h["host"], h["primary"]) for h in ev.hosts()] == [("box", True), ("mac", True), ("alpha", False)]
    row, err = ev.start_get("qwen35-9b-q4", "alice")
    assert err is None and row["spec"]["agent_id"] == "a1"
    why = te.Evaluator._why
    assert why({"ok": False, "response": {"error": {"message": "repo not found", "type": "model_not_found"}}}, None, _Resp({})) == "repo not found"
    assert why({"ok": False, "error": "boom"}, None, None) == "boom"
    assert why(None, "dial failed", None) == "dial failed"
    assert why(None, None, _Resp(status=500)) == "HTTP 500"
    assert why(None, None, None) == "no response"


def test_get_model_job_fails_when_the_restarted_server_never_lists_the_model(monkeypatch):
    log, sections = [], {}
    lines = ['data: {"type": "done", "ok": true, "rc": 0}']
    agent = {"agent_id": "a1", "hostname": "box", "token": "t", "status": "approved"}
    listed = {"n": -10_000}   # never reaches the second poll
    ev, svc = _evaluator([], agent_request=_fake_agent_request(log, sections, lines, listed=listed),
                         download_hosts=lambda: [{"provider": "llama", "host": "box", "agent_id": "a1", "primary": True, "agent": agent}])
    monkeypatch.setattr(te, "SERVER_WAIT_S", 0.01)
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    import agent_registry
    monkeypatch.setattr(agent_registry, "resolve_agent_by_id", lambda aid, capability=None: agent)
    row, _err = ev.start_get("qwen35-9b-q4", "alice")
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "failed" and "came back without unsloth/Qwen3.5-9B-GGUF:Q4_K_M" in done["message"]
    assert "/llama/load" not in [p for _m, p, _j in log]


def test_get_model_job_on_lm_studio_downloads_by_repo_and_waits_for_the_key(monkeypatch):
    log = []
    listed = {"n": 0}
    lms_id = "qwen3.5-9b"

    def req(method, agent_, path, **kw):
        log.append((method, path, kw.get("json")))
        if path == "/lms/download":
            return _Resp({"ok": True, "response": {"status": "downloading"}}), [], None
        if path == "/lms/models":
            listed["n"] += 1
            data = [{"id": "gemma-3-12b"}] + ([{"id": lms_id}] if listed["n"] >= 3 else [])
            return _Resp({"data": data}), [], None
        if path == "/lms/load":
            entries.append({"id": lms_id, "provider": "lms", "status": {"value": "loaded"}, "hosts": ["mac"], "agent_ids": ["a2"]})
            return _Resp({"ok": True}), [], None
        raise AssertionError(path)
    entries = list(ENTRIES)
    mac = {"agent_id": "a2", "hostname": "mac", "token": "t", "status": "approved"}
    box = {"agent_id": "a1", "hostname": "box", "token": "t", "status": "approved"}
    named, profiles = [], []
    ev, svc = _evaluator([], entries=entries, agent_request=req, refresh_index=lambda w: None,
                         download_hosts=lambda: [{"provider": "llama", "host": "box", "agent_id": "a1", "primary": True, "agent": box},
                                                 {"provider": "lms", "host": "mac", "agent_id": "a2", "primary": True, "agent": mac}],
                         profile_put=lambda *a: profiles.append(a), alias_set=lambda m, a: named.append((m, a)))
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    import agent_registry
    monkeypatch.setattr(agent_registry, "resolve_agent_by_id", lambda aid, capability=None: {"a1": box, "a2": mac}.get(aid))
    monkeypatch.setattr(ev, "run_eval", lambda model, **kw: ev.store.save(te.summarize(
        model, [{"id": "hosts", "title": "Plain read", "passed": True, "calls": 1, "corrections": 0, "retries": 0, "ms": 3, "detail": "ok"}],
        quant=None, server="LM Studio", tool_mode="auto", grade="native", ms=3, actor="eval", at=9.0)))
    assert [(h["host"], h["provider"]) for h in ev.hosts()] == [("box", "llama"), ("mac", "lms")]
    row, err = ev.start_get("qwen35-9b-q4", "alice", "a2")
    assert err is None and row["spec"]["provider"] == "lms" and row["spec"]["host"] == "mac"
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "done", done
    assert done["result"]["model"] == lms_id and done["result"]["host"] == "mac" and done["result"]["pin_offer"]
    assert log[0] == ("POST", "/lms/download", {"model": "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF", "quantization": "Q4_K_M"})
    assert named == [(lms_id, "Tower Model")] and profiles == []
    assert [p for _m, p, _j in log].count("/lms/models") == 3 and log[-1][1] == "/lms/load"
    assert log[-1][2] == {"model": lms_id, "context_length": 32768, "eval_batch_size": 2048}
    assert "/llama/config" not in [p for _m, p, _j in log]
    view = ev.curated_view()
    got = next(m for m in view["models"] if m["key"] == "qwen35-9b-q4")
    assert got["present"] and got["loaded"] and got["eval"]["model"] == lms_id
    assert te.Evaluator._lms_key("unsloth/Qwen3.5-9B-GGUF") == "qwen359b"


# ── routes ──

@pytest.fixture
def client(monkeypatch):
    import auth
    import manager_mod as M
    from flask import session as _flask_session
    settings = M.ctx.settings   # the object the routes read (a reloaded unified_config would hand out another)
    orig = settings.manager.tower.model_copy()
    settings.manager.tower.enabled = True
    monkeypatch.setattr(auth, "_live_role_for_session", lambda: (_flask_session.get("role"), True))
    monkeypatch.setattr(tower, "_gateway_entries", lambda: ENTRIES, raising=False)
    ev = M._tower_evals
    saved = (ev._svc, ev.store, ev._entries, ev._cs)
    svc = _service()
    svc.register(M._jobs_service.kind(te.KIND_EVAL))
    svc.register(M._jobs_service.kind(te.KIND_GET))
    ev._svc, ev.store, ev._entries = svc, te.EvalStore(":memory:"), lambda: ENTRIES
    ev._cs = _stream(["plain"] * 20)
    M.app.config["TESTING"] = True
    try:
        with M.app.test_client() as c:
            with c.session_transaction() as s:
                s["auth_ok"] = True; s["role"] = "operator"; s["user"] = "alice"
            yield c
    finally:
        ev._svc, ev.store, ev._entries, ev._cs = saved
        settings.manager.tower.enabled = orig.enabled


def test_routes_list_start_export_and_models(client):
    import manager_mod as M
    r = client.get("/api/tower/eval").get_json()
    assert r["ok"] and r["results"] == [] and r["live"] is None, r
    assert r["model"] == "qwen3-14b" and r["admin"] is False, r
    assert client.post("/api/tower/eval", json={"model": "qwen3-14b"}).status_code == 403
    with client.session_transaction() as s:
        s["role"] = "admin"
    r = client.post("/api/tower/eval", json={"model": "qwen3-14b"})
    assert r.status_code == 200 and r.get_json()["job"]["kind"] == te.KIND_EVAL
    assert client.post("/api/tower/eval", json={}).status_code == 409
    live = client.get("/api/tower/eval").get_json()["live"]
    assert live and live["status"] == "queued" and live["can_cancel"]
    M._tower_evals._svc.tick()
    r = client.get("/api/tower/eval").get_json()
    assert r["live"] is None and len(r["results"]) == 1 and r["results"][0]["model"] == "qwen3-14b"
    eid = r["results"][0]["id"]
    # a result for a model the gateway no longer lists stays in the store but leaves the list
    M._tower_evals.store.save(te.summarize({"model": "deleted-model", "provider": "llama", "hosts": []}, [], quant=None, server=None,
                                           tool_mode="auto", grade=None, ms=1, actor="x", at=99.0))
    assert [x["model"] for x in client.get("/api/tower/eval").get_json()["results"]] == ["qwen3-14b"]
    assert client.get("/api/tower/eval?model=deleted-model").get_json()["results"][0]["model"] == "deleted-model"
    assert client.get("/api/tower/eval?model=qwen3-14b").get_json()["results"][0]["id"] == eid
    full = client.get(f"/api/tower/eval/{eid}").get_json()["result"]
    assert full["cases"] and "prompt" in full["cases"][0]
    exp = client.get(f"/api/tower/eval/{eid}?export=1")
    assert exp.status_code == 200 and exp.headers["Content-Disposition"].startswith("attachment; filename=tower-eval-qwen3-14b-")
    assert json.loads(exp.data)["id"] == eid
    assert client.get("/api/tower/eval/nope").status_code == 404
    assert client.post("/api/tower/eval", json={"model": "gemma-3-12b"}).status_code == 503
    m = client.get("/api/tower/models").get_json()
    assert m["ok"] and len(m["models"]) >= 5 and m["live"] is None and m["admin"]
    assert client.post("/api/tower/models/get", json={"key": "nope"}).status_code == 404


def test_routes_are_gated(client):
    import manager_mod as M
    with client.session_transaction() as s:
        s["role"] = "operator"
    assert client.post("/api/tower/models/get", json={"key": "qwen35-9b-q4"}).status_code == 403
    M.ctx.settings.manager.tower.enabled = False
    assert client.get("/api/tower/eval").status_code == 404
    assert client.get("/api/tower/models").status_code == 404
