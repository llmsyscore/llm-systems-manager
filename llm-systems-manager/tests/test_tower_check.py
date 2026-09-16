"""#1039 model capability check: size hint, probe grading, cache and background run."""
from __future__ import annotations

import threading
import time
import types

import tower_check as tc
import tower_tools as tt


def _cfg(**over):
    d = dict(enabled=True, model="auto", tool_mode="auto", capabilities="read", off_topic="refuse",
             disabled_tools=[], max_tool_calls=3, max_tokens=256, temperature=0.2, request_timeout_s=0)
    d.update(over)
    return types.SimpleNamespace(**d)


def _deps():
    return {"host": lambda n, section="all": {"hostname": n}, "hosts": lambda *a, **k: [{"hostname": "box"}]}


def _registry():
    return tt.build_registry({**_deps(), "host_history": lambda *a, **k: {}, "models": lambda *a, **k: [],
                              "alarms": lambda *a, **k: [], "alert": lambda a: None, "alarm_search": lambda a: {},
                              "alarm_history": lambda *a, **k: {}, "energy": lambda *a, **k: {}, "flow": lambda: {},
                              "runs": lambda *a, **k: [], "speed": lambda m: [], "health": lambda: {},
                              "log_tail": lambda *a, **k: [], "config_get": lambda p: {}, "help": lambda t: "",
                              "audit": lambda *a, **k: []})


def _stream(replies):
    """A fake complete_stream: each call yields the next scripted reply (content string or native call)."""
    it = iter(replies)
    seen = []
    def cs(body, *, label, **kw):
        seen.append({"label": label, "body": body})
        r = next(it)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, dict):
            yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c0", "function": r}]}}]}
        else:
            yield {"choices": [{"delta": {"content": r}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    cs.seen = seen
    return cs


FENCED_OK = '```tool\n{"name":"hosts_overview","args":{}}\n```'
NATIVE_OK = {"name": "hosts_overview", "arguments": "{}"}


def test_size_b_reads_the_largest_b_suffix():
    assert tc.size_b("gemma-3-12b-it") == 12
    assert tc.size_b("Qwen3.8-27B-GGUF") == 27
    assert tc.size_b("nemotron-nano-4b-q4_k_m") == 4
    assert tc.size_b("mixtral-8x7b") == 7
    assert tc.size_b("Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf") == 8
    assert tc.size_b("phi-4") is None and tc.size_b("") is None
    assert tc.size_b("qwen2.5-0.5b") == 0.5
    assert tc.size_b("model-q1b-x") is None
    assert tc.size_b("7b") == 7


def test_probe_passes_when_the_reply_calls_hosts_overview():
    cs = _stream([FENCED_OK])
    ok, detail = tc.probe(cs, _cfg(), tt.catalog(_registry(), _cfg(), "operator"), {"model": "m", "provider": "llama"}, native=False)
    assert ok and detail == "called hosts_overview"
    body = cs.seen[0]["body"]
    assert cs.seen[0]["label"] == "tower-check" and body["temperature"] == 0 and body["max_tokens"] == 512
    assert "tools" not in body and body["messages"][-1] == {"role": "user", "content": "Which hosts are online? Call hosts_overview."}


def test_probe_native_sends_the_schema_and_accepts_a_native_call():
    cs = _stream([NATIVE_OK])
    ok, detail = tc.probe(cs, _cfg(), tt.catalog(_registry(), _cfg(), "operator"), {"model": "m", "provider": "lms"}, native=True)
    assert ok and "tools" in cs.seen[0]["body"]


def test_probe_fails_on_prose_other_tool_or_error():
    tools = tt.catalog(_registry(), _cfg(), "operator")
    assert tc.probe(_stream(["hosts_overview: box is online"]), _cfg(), tools, {"model": "m", "provider": "llama"}, native=False) == (False, "no call")
    assert tc.probe(_stream(['```tool\n{"name":"alarms","args":{}}\n```']), _cfg(), tools, {"model": "m", "provider": "llama"}, native=False) == (False, "called alarms")
    assert tc.probe(_stream([RuntimeError("boom")]), _cfg(), tools, {"model": "m", "provider": "llama"}, native=False) == (False, "error: RuntimeError")


def test_run_grades_native_then_fenced_then_failed():
    cfg = _cfg()
    model = {"model": "gemma-3-12b", "provider": "llama", "hosts": ["box"]}
    now = [1000.0]
    c = tc.Checks(complete_stream=_stream([NATIVE_OK]), cfg=lambda: cfg, registry_factory=_registry,
                  server_args_of=lambda m: ["--jinja"], now=lambda: now[0])
    r = c.run(model)
    assert r == {"model": "gemma-3-12b", "grade": "native", "mode": "native", "size_b": 12, "small": False,
                 "at": 1000.0, "detail": "called hosts_overview"}
    c2 = tc.Checks(complete_stream=_stream(["nope", FENCED_OK]), cfg=lambda: cfg, registry_factory=_registry,
                   server_args_of=lambda m: ["--jinja"], now=lambda: now[0])
    r2 = c2.run({"model": "tiny-4b", "provider": "llama", "hosts": ["box"]})
    assert r2["grade"] == "fenced" and r2["mode"] == "fenced" and r2["small"] is True and r2["size_b"] == 4
    cs3 = _stream(["nope"])
    c3 = tc.Checks(complete_stream=cs3, cfg=lambda: cfg, registry_factory=_registry,
                   server_args_of=lambda m: [None], now=lambda: now[0])
    r3 = c3.run({"model": "x", "provider": "llama", "hosts": ["box"]})
    assert r3["grade"] == "failed" and r3["mode"] == "fenced" and r3["size_b"] is None and r3["small"] is False
    assert len(cs3.seen) == 1  # no --jinja: native never tried


def test_ensure_caches_by_model_and_tool_mode_and_runs_once_in_the_background():
    cfg = _cfg()
    started = threading.Event()
    release = threading.Event()
    def slow(body, *, label, **kw):
        started.set(); release.wait(2)
        yield {"choices": [{"delta": {"content": FENCED_OK}}]}
    c = tc.Checks(complete_stream=slow, cfg=lambda: cfg, registry_factory=_registry, server_args_of=lambda m: [None])
    model = {"model": "m", "provider": "llama", "hosts": ["box"]}
    assert c.ensure(model) == {"model": "m", "grade": "pending"}
    assert started.wait(1)
    assert c.ensure(model) == {"model": "m", "grade": "pending"}   # second call does not start another run
    release.set()
    deadline = time.time() + 2
    while time.time() < deadline and c.get("m") is None:
        time.sleep(0.01)
    assert c.get("m")["grade"] == "fenced"
    assert c.ensure(model) is c.get("m")
    cfg.tool_mode = "prompt"
    assert c.get("m") is None                       # a different tool mode is a different check
    c.forget()
    cfg.tool_mode = "auto"
    assert c.get("m") is None


def test_a_raising_probe_body_is_graded_failed_and_cached():
    cfg = _cfg()
    def boom():
        raise RuntimeError("no registry")
    c = tc.Checks(complete_stream=_stream([FENCED_OK]), cfg=lambda: cfg, registry_factory=boom,
                  server_args_of=lambda m: [None], now=lambda: 1000.0)
    r = c.run({"model": "tiny-4b", "provider": "llama", "hosts": ["box"]})
    assert r == {"model": "tiny-4b", "grade": "failed", "mode": "fenced", "size_b": 4, "small": True,
                 "at": 1000.0, "detail": "error: RuntimeError"}
    assert c.get("tiny-4b") == r


def test_run_joins_a_background_run_instead_of_probing_twice():
    cfg = _cfg()
    started = threading.Event()
    release = threading.Event()
    seen = []
    def slow(body, *, label, **kw):
        seen.append(label); started.set(); release.wait(3)
        yield {"choices": [{"delta": {"content": FENCED_OK}}]}
    c = tc.Checks(complete_stream=slow, cfg=lambda: cfg, registry_factory=_registry, server_args_of=lambda m: [None])
    model = {"model": "m", "provider": "llama", "hosts": ["box"]}
    assert c.ensure(model)["grade"] == "pending"
    assert started.wait(2)
    got = []
    t = threading.Thread(target=lambda: got.append(c.run(model)), daemon=True)
    t.start()
    time.sleep(0.1)
    release.set()
    t.join(5)
    assert not t.is_alive() and len(seen) == 1
    assert got[0] is c.get("m") and got[0]["grade"] == "fenced"
