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
    assert te.quant_of("Qwen/Qwen3-8B-GGUF:Q4_K_M") == "Q4_K_M"
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
        assert m["model_id"] == f"{m['repo']}:{m['quant']}" and m["file"].endswith(".gguf") and m["quant"] in m["file"]
        assert m["tier_gb"] in (8, 12, 16, 24, 32, 48) and m["expected"] in ("good", "high") and m["size_gb"] < m["tier_gb"]
    assert len({m["key"] for m in models}) == len(models)


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


def _fake_agent_request(log, sections, lines, load_ok=True):
    def req(method, agent, path, **kw):
        log.append((method, path, kw.get("json")))
        if path == "/llama/download":
            return _Resp({"ok": True}), [], None
        if path == "/llama/download/stream":
            return _Resp(lines=lines), [], None
        if path == "/llama/config" and method == "GET":
            return _Resp(dict(sections)), [], None
        if path == "/llama/config":
            sections.clear(); sections.update(kw.get("json") or {})
            return _Resp({"ok": True}), [], None
        if path in ("/llama/server/restart", "/llama/download/cancel"):
            return _Resp({"ok": True}), [], None
        if path == "/llama/load":
            return _Resp({"ok": load_ok} if load_ok else {"ok": False, "error": "no such model"}), [], None
        raise AssertionError(path)
    return req


def test_get_model_job_downloads_registers_loads_checks_and_evals(monkeypatch):
    log, sections = [], {"__DEFAULTS__": {"x": "1"}, "old-model": {"ctx-size": "4096"}}
    lines = ['data: {"type": "start", "cmd": "hf download"}', 'data: {"type": "line", "text": "Qwen3-8B-Q4_K_M.gguf: 40%", "progress": true}',
             '', 'data: {"type": "line", "text": "Qwen3-8B-Q4_K_M.gguf: 100%", "progress": true}', 'data: {"type": "done", "ok": true, "rc": 0}']
    entries = list(ENTRIES)
    agent = {"agent_id": "a1", "hostname": "box", "token": "t", "status": "approved"}
    ev, svc = _evaluator([], entries=entries, agent_request=_fake_agent_request(log, sections, lines),
                         primary_agent=lambda: agent, refresh_index=lambda w: None)
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
    mid = "Qwen/Qwen3-8B-GGUF:Q4_K_M"

    def req_then_resident(method, agent_, path, **kw):
        out = _fake_agent_request(log, sections, lines)(method, agent_, path, **kw)
        if path == "/llama/load":
            entries.append({"id": mid, "provider": "llama", "status": {"value": "loaded"}, "hosts": ["box"], "agent_ids": ["a1"]})
        return out
    ev._agent_request = req_then_resident
    row, err = ev.start_get("qwen3-8b-q4", "alice")
    assert err is None and row["spec"]["model_id"] == mid and row["label"].startswith("Get Tower model · Qwen3 8B")
    assert ev.start("qwen3-14b", "alice") == (None, "busy")
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "done", done
    assert done["result"]["pin_offer"] and done["result"]["model"] == mid and done["result"]["eval"]["passed"] == 1
    paths = [p for _m, p, _j in log]
    assert paths == ["/llama/download", "/llama/download/stream", "/llama/config", "/llama/config", "/llama/server/restart", "/llama/load"]
    assert sections[mid] == te.NEW_MODEL_INI and "__DEFAULTS__" not in sections and "old-model" in sections
    assert any(s.get("phase") == "download" and s.get("pct") == 40 for s in states)
    assert any(s.get("phase") == "load" for s in states) and any(s.get("phase") == "check" for s in states)
    view = ev.curated_view()
    got = next(m for m in view["models"] if m["key"] == "qwen3-8b-q4")
    assert got["present"] and got["loaded"] and got["eval"]["passed"] == 1 and view["host"] == "box"


def test_get_model_job_fails_cleanly_on_a_download_error(monkeypatch):
    log, sections = [], {}
    lines = ['data: {"type": "done", "ok": false, "rc": 1}']
    agent = {"agent_id": "a1", "hostname": "box", "token": "t", "status": "approved"}
    ev, svc = _evaluator([], agent_request=_fake_agent_request(log, sections, lines), primary_agent=lambda: agent)
    import agent_registry
    monkeypatch.setattr(agent_registry, "resolve_agent_by_id", lambda aid, capability=None: agent)
    row, err = ev.start_get("qwen3-8b-q4", "alice")
    assert err is None
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "failed" and "download failed" in done["message"]
    assert [p for _m, p, _j in log] == ["/llama/download", "/llama/download/stream"]
    assert ev.start_get("nope", "alice") == (None, "unknown model")


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
    assert client.post("/api/tower/models/get", json={"key": "qwen3-8b-q4"}).status_code == 403
    M.ctx.settings.manager.tower.enabled = False
    assert client.get("/api/tower/eval").status_code == 404
    assert client.get("/api/tower/models").status_code == 404
