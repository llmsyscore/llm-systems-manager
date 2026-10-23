"""collect_llama_for_metrics() surfaces the windowed cache/speculative blocks (#1303)."""
from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "llama_metrics_spec_mtp.txt"
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))


def _stub_module(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules.setdefault(name, mod)
    return sys.modules[name]


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


if not _module_available("requests"):
    _stub_module("requests", get=None, Session=object)
if not _module_available("fastapi"):
    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            super().__init__(detail)
            self.status_code = status_code
    _stub_module(
        "fastapi",
        Header=lambda *a, **k: None, HTTPException=_HTTPException,
        Query=lambda *a, **k: None, Request=object,
    )
    _stub_module("fastapi.responses", Response=object, StreamingResponse=object)
    _stub_module("starlette")
    _stub_module("starlette.concurrency", run_in_threadpool=lambda f, *a, **k: f(*a, **k))
if not _module_available("psutil"):
    _collectors = _stub_module("collectors")
    _collectors.__path__ = []
    _stub_module("collectors.gpu", collect_gpu=lambda: {})

_pkg = types.ModuleType("providers")
_pkg.__path__ = [str(_AGENT_ROOT / "providers")]
sys.modules.setdefault("providers", _pkg)

if "providers.llama" in sys.modules:
    llama = sys.modules["providers.llama"]
else:
    _spec = importlib.util.spec_from_file_location(
        "providers.llama", _AGENT_ROOT / "providers" / "llama.py")
    llama = importlib.util.module_from_spec(_spec)
    sys.modules["providers.llama"] = llama
    _spec.loader.exec_module(llama)

MODEL = "bartowski/Qwen3.8-27B-GGUF:Q4_K_M"


class _Resp:
    def __init__(self, payload=None, text="", ok=True):
        self._payload = payload
        self.ok = ok
        self.text = text
        self.status_code = 200 if ok else 500

    def json(self):
        return self._payload


def _bump(text: str, name: str, by: int) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith(name + " "):
            out.append(f"{name} {int(float(line.split()[-1])) + by}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def _fake_get_factory(metrics_holder: dict, calls: list):
    def _fake_get(url, **kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/v1/models"):
            return _Resp({"data": [{"id": MODEL, "status": {"value": "loaded"}}]})
        if url.endswith("/props"):
            return _Resp({"is_sleeping": False, "total_slots": 1, "build_info": "b1-339f3242"})
        if url.endswith("/metrics"):
            assert (kwargs.get("params") or {}).get("model") == MODEL
            assert (kwargs.get("params") or {}).get("autoload") == "0"
            return _Resp(text=metrics_holder["text"])
        if url.endswith("/slots"):
            return _Resp([])
        raise ConnectionError(f"unexpected fetch: {url}")
    return _fake_get


@pytest.fixture
def ctx(monkeypatch):
    cfg = SimpleNamespace(LLAMA_ENABLED=True, POLL_INTERVAL_S=2.0,
                          LLAMA_API_URL="http://127.0.0.1:9999", LLAMA_BUILD_METHOD="custom_script")
    context = SimpleNamespace(config=cfg, state={}, probe_http=lambda url, timeout=1.5: (True, "ok"),
                              runtime_lock=threading.RLock())
    llama.set_context(context)
    monkeypatch.setattr(llama, "_llama_info_last_poll", 0.0)
    monkeypatch.setattr(llama, "_llama_info_cache", {})
    monkeypatch.setattr(llama, "_llama_loaded", {"last": None})
    monkeypatch.setattr(llama, "_llama_info_last_tokens_total", None)
    monkeypatch.setattr(llama, "_llama_info_idle_logged", False)
    monkeypatch.setattr(llama, "_llama_counters", llama.llama_counters.CounterWindow())
    monkeypatch.setattr(llama, "collect_gpu", lambda: {}, raising=False)
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: None)
    monkeypatch.setattr(llama, "_llama_server_cmdline", lambda pid: None)
    return context


def _poll(monkeypatch, now):
    monkeypatch.setattr(llama, "_llama_info_last_poll", 0.0)
    monkeypatch.setattr(llama, "_llama_info_last_active_ts", now)
    monkeypatch.setattr(llama, "time", SimpleNamespace(time=lambda: now))
    return llama.collect_llama_for_metrics()


def test_first_poll_exposes_raw_counters_and_idle_blocks(ctx, monkeypatch):
    holder = {"text": _FIXTURE.read_text()}
    calls: list = []
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory(holder, calls)))
    out = _poll(monkeypatch, 1000.0)
    assert out["total_tokens_prompted"] == 262759
    assert out["prompt_tokens_cached_total"] == 238834
    assert out["spec_draft_tokens_total"] == 23627
    assert out["spec_accepted_tokens_total"] == 16486
    assert out["spec_drafts_total"] == 7877
    assert out["cache_reuse"]["state"] == "idle"
    assert out["cache_reuse"]["ratio"] is None
    assert out["spec_accept"]["state"] == "idle"
    assert "window_s" not in out["cache_reuse"]


def test_second_poll_derives_windowed_ratios(ctx, monkeypatch):
    holder = {"text": _FIXTURE.read_text()}
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory(holder, [])))
    _poll(monkeypatch, 1000.0)
    t = _bump(holder["text"], "llamacpp:prompt_tokens_total", 10)
    t = _bump(t, "llamacpp:prompt_tokens_cached_total", 8200)
    t = _bump(t, "llamacpp:spec_decode_num_draft_tokens_total", 93)
    t = _bump(t, "llamacpp:spec_decode_num_accepted_tokens_total", 68)
    t = _bump(t, "llamacpp:spec_decode_num_drafts_total", 30)
    holder["text"] = t
    out = _poll(monkeypatch, 1005.0)
    cr, sa = out["cache_reuse"], out["spec_accept"]
    assert (cr["state"], cr["processed"], cr["cached"]) == ("ok", 10, 8200)
    assert cr["ratio"] == pytest.approx(8200 / 8210, abs=1e-4)
    assert (sa["state"], sa["drafted"], sa["accepted"], sa["drafts"]) == ("ok", 93, 68, 30)
    assert sa["ratio"] == pytest.approx(68 / 93, abs=1e-4)
    assert out["prompt_tokens_cached_total"] == 238834 + 8200


def test_build_without_counters_reports_unsupported(ctx, monkeypatch):
    legacy = "\n".join(l for l in _FIXTURE.read_text().splitlines()
                       if "cached" not in l and "spec_decode" not in l) + "\n"
    holder = {"text": legacy}
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory(holder, [])))
    _poll(monkeypatch, 1000.0)
    out = _poll(monkeypatch, 1005.0)
    assert out["total_tokens_prompted"] == 262759
    assert out["prompt_tokens_cached_total"] is None
    assert out["spec_draft_tokens_total"] is None
    assert out["cache_reuse"]["state"] == "unsupported"
    assert out["spec_accept"]["state"] == "unsupported"


def test_idle_pause_keeps_raw_counters_and_marks_paused(ctx, monkeypatch):
    holder = {"text": _FIXTURE.read_text()}
    calls: list = []
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory(holder, calls)))
    _poll(monkeypatch, 1000.0)
    holder["text"] = _bump(_bump(holder["text"], "llamacpp:prompt_tokens_total", 100),
                           "llamacpp:prompt_tokens_cached_total", 300)
    _poll(monkeypatch, 1005.0)
    calls.clear()
    # Token-idle for longer than the threshold: /metrics is skipped, no wake-up.
    monkeypatch.setattr(llama, "_llama_info_last_poll", 0.0)
    monkeypatch.setattr(llama, "_llama_info_last_active_ts", 1005.0)
    monkeypatch.setattr(llama, "time", SimpleNamespace(
        time=lambda: 1005.0 + llama._LLAMA_METRICS_IDLE_THRESHOLD_S + 1))
    out = llama.collect_llama_for_metrics()
    assert "metrics" not in calls
    assert out["prompt_tokens_cached_total"] == 238834 + 300
    assert out["total_tokens_prompted"] == 262759 + 100
    cr = out["cache_reuse"]
    # The 60 s window aged out while polling was paused: no deltas, no made-up ratio.
    assert (cr["state"], cr["processed"], cr["cached"], cr["ratio"]) == ("paused", 0, 0, None)
    assert out["spec_accept"]["state"] == "paused"


def test_model_switch_while_paused_drops_previous_model_blocks(ctx, monkeypatch):
    holder = {"text": _FIXTURE.read_text()}
    fake = _fake_get_factory(holder, [])
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=fake))
    _poll(monkeypatch, 1000.0)
    holder["text"] = _bump(_bump(holder["text"], "llamacpp:prompt_tokens_total", 100),
                           "llamacpp:prompt_tokens_cached_total", 300)
    assert _poll(monkeypatch, 1005.0)["cache_reuse"]["state"] == "ok"

    # Router swaps to another model while /metrics polling is paused.
    def _get_b(url, **kwargs):
        if url.endswith("/v1/models"):
            return _Resp({"data": [{"id": "other/model:Q8", "status": {"value": "loaded"}}]})
        if url.endswith("/props"):
            return _Resp({"is_sleeping": False, "total_slots": 1})
        return fake(url, **kwargs)
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_get_b))
    monkeypatch.setattr(llama, "_llama_info_last_poll", 0.0)
    monkeypatch.setattr(llama, "_llama_info_last_active_ts", 1005.0)
    monkeypatch.setattr(llama, "time", SimpleNamespace(
        time=lambda: 1005.0 + llama._LLAMA_METRICS_IDLE_THRESHOLD_S + 1))
    out = llama.collect_llama_for_metrics()
    assert out["model"] == "other/model:Q8"
    assert out["cache_reuse"] is None and out["spec_accept"] is None
    assert out["prompt_tokens_cached_total"] is None
    assert out["spec_draft_tokens_total"] is None


def test_sleeping_model_skips_metrics_and_leaves_blocks_none(ctx, monkeypatch):
    holder = {"text": _FIXTURE.read_text()}
    calls: list = []
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory(holder, calls)))
    _poll(monkeypatch, 1000.0)
    _poll(monkeypatch, 1005.0)
    calls.clear()

    def _get_sleeping(url, **kwargs):
        if url.endswith("/props"):
            calls.append("props")
            return _Resp({"is_sleeping": True, "total_slots": 1})
        return _fake_get_factory(holder, calls)(url, **kwargs)
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_get_sleeping))
    out = _poll(monkeypatch, 1010.0)
    assert out["sleeping"] is True
    assert "metrics" not in calls and "slots" not in calls
    assert out["cache_reuse"] is None and out["spec_accept"] is None
    assert out["prompt_tokens_cached_total"] is None


def test_mainpid_change_resets_window(ctx, monkeypatch):
    holder = {"text": _FIXTURE.read_text()}
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory(holder, [])))
    monkeypatch.setattr(llama, "_llama_main_pid", {"last": None})
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: 4242)
    _poll(monkeypatch, 1000.0)
    holder["text"] = _bump(holder["text"], "llamacpp:prompt_tokens_cached_total", 300)
    assert _poll(monkeypatch, 1005.0)["cache_reuse"]["state"] == "ok"
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: 4343)
    holder["text"] = _bump(holder["text"], "llamacpp:prompt_tokens_cached_total", 5000)
    out = _poll(monkeypatch, 1010.0)
    assert out["cache_reuse"]["state"] == "reset"
    assert out["cache_reuse"]["cached"] == 0
