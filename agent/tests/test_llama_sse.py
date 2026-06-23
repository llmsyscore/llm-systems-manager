"""Unit tests for the llama.cpp /models/sse leaf module (issue #138).

The full providers/llama.py pulls in fastapi/requests/siblings, so the SSE
logic lives in a dependency-light leaf module loaded standalone here.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_LLAMA_SSE_PY = _AGENT_ROOT / "providers" / "llama_sse.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


llama_sse = _load("llama_sse", _LLAMA_SSE_PY)


# ── router_mode_from_flags ─────────────────────────────────────────────

def test_router_mode_true_with_models_max():
    assert llama_sse.router_mode_from_flags(["--models-max", "4"]) is True


def test_router_mode_true_with_models_preset():
    assert llama_sse.router_mode_from_flags(["--models-preset", "/x"]) is True


def test_router_mode_false_when_single_model_flag_present():
    # -m wins even if a router flag is also present.
    assert llama_sse.router_mode_from_flags(["--models-max", "4", "-m", "/m.gguf"]) is False


def test_router_mode_false_with_long_model_flag():
    assert llama_sse.router_mode_from_flags(["--model", "/m.gguf"]) is False


def test_router_mode_false_with_no_relevant_flags():
    assert llama_sse.router_mode_from_flags(["--host", "0.0.0.0", "--port", "8080"]) is False


# ── parse_exec_flags ───────────────────────────────────────────────────

def test_parse_exec_flags_extracts_router_flags():
    line = "/usr/bin/llama-server --host 0.0.0.0 --port 8080 --models-max 4 --models-dir /m"
    flags = llama_sse.parse_exec_flags(line)
    assert "--models-max" in flags and "--models-dir" in flags
    assert "/m" not in flags  # values are not flags


def test_parse_exec_flags_single_model():
    flags = llama_sse.parse_exec_flags("/usr/bin/llama-server -m /models/m.gguf -ngl 99")
    assert "-m" in flags


def test_parse_exec_flags_bad_shell_returns_empty():
    assert llama_sse.parse_exec_flags('llama-server --opt "unterminated') == []


def test_router_mode_from_exec_line_roundtrip():
    line = "/usr/bin/llama-server --models-preset /presets --port 8080"
    assert llama_sse.router_mode_from_flags(llama_sse.parse_exec_flags(line)) is True


# ── sse_status_to_state ────────────────────────────────────────────────

@pytest.mark.parametrize("status,expected", [
    ("loaded", "awake"),
    ("loading", "awake"),
    ("sleeping", "sleeping"),
    ("unloaded", None),
    ("downloading", None),
    ("failed", None),
    ("totally-unknown", None),
    ("", None),
    (None, None),
])
def test_sse_status_to_state(status, expected):
    assert llama_sse.sse_status_to_state(status) == expected


def test_sse_status_to_state_is_case_insensitive():
    assert llama_sse.sse_status_to_state("LOADED") == "awake"


# ── parse_sse_stream ───────────────────────────────────────────────────

def test_parse_single_untyped_data_event():
    lines = ["data: {\"a\": 1}", ""]
    assert list(llama_sse.parse_sse_stream(lines)) == [("message", "{\"a\": 1}")]


def test_parse_typed_event():
    lines = ["event: model_status", "data: {\"status\": \"sleeping\"}", ""]
    assert list(llama_sse.parse_sse_stream(lines)) == [
        ("model_status", "{\"status\": \"sleeping\"}")
    ]


def test_parse_ignores_comment_keepalive_lines():
    lines = [": keepalive", "event: ping", "data: {}", ""]
    assert list(llama_sse.parse_sse_stream(lines)) == [("ping", "{}")]


def test_parse_dispatches_each_data_line_separately():
    # llama.cpp sends one JSON per data line; each is its own event.
    lines = ["data: line1", "data: line2", ""]
    assert list(llama_sse.parse_sse_stream(lines)) == [
        ("message", "line1"), ("message", "line2")
    ]


def test_parse_multiple_events():
    lines = [
        "event: a", "data: 1", "",
        "event: b", "data: 2", "",
    ]
    assert list(llama_sse.parse_sse_stream(lines)) == [("a", "1"), ("b", "2")]


def test_parse_dispatches_data_line_without_trailing_blank():
    # No blank-line terminator (llama.cpp style) still dispatches.
    lines = ["event: x", "data: 1"]
    assert list(llama_sse.parse_sse_stream(lines)) == [("x", "1")]


def test_parse_real_llama_stream_no_blank_lines():
    # Exact framing observed from llama.cpp /models/sse (no blank separators).
    lines = [
        'data: {"model":"m","event":"status_change","data":{"status":"unloaded","exit_code":0}}',
        'data: {"model":"m","event":"model_status","data":{"status":"loading"}}',
        'data: {"model":"m","event":"status_change","data":{"status":"loaded"}}',
        'data: {"model":"m","event":"status_change","data":{"status":"sleeping"}}',
    ]
    frames = list(llama_sse.parse_sse_stream(lines))
    assert len(frames) == 4
    assert all(f.event == "message" for f in frames)


def test_parse_strips_only_one_leading_space_after_colon():
    lines = ["data:  two-spaces", ""]
    assert list(llama_sse.parse_sse_stream(lines)) == [("message", " two-spaces")]


# ── event_kind ─────────────────────────────────────────────────────────

def test_event_kind_prefers_sse_event_field():
    assert llama_sse.event_kind("model_status", {"type": "other"}) == "model_status"


def test_event_kind_falls_back_to_data_type():
    assert llama_sse.event_kind("message", {"type": "download_progress"}) == "download_progress"


def test_event_kind_falls_back_to_data_event_key():
    assert llama_sse.event_kind("message", {"event": "models_reload"}) == "models_reload"


def test_event_kind_defaults_to_message():
    assert llama_sse.event_kind("message", {"foo": 1}) == "message"


# ── LlamaSseListener ───────────────────────────────────────────────────

def _connection(*events_lines):
    """Build one connection's worth of SSE lines from event tuples."""
    out = []
    for ev, data in events_lines:
        if ev:
            out.append(f"event: {ev}")
        out.append(f"data: {data}")
        out.append("")
    return out


class _Driver:
    """Records on_event/sleep, drives connect() through canned connections.

    should_stop() trips once connect() has been called max_connects times, so
    each canned connection is processed in full before shutdown.
    """

    def __init__(self, connections, max_connects):
        self._connections = list(connections)
        self._max_connects = max_connects
        self.connect_calls = 0
        self.events = []
        self.sleeps = []

    def connect(self):
        idx = self.connect_calls
        self.connect_calls += 1
        if idx < len(self._connections):
            conn = self._connections[idx]
            if isinstance(conn, Exception):
                raise conn
            return iter(conn)
        return iter([])

    def should_stop(self):
        return self.connect_calls >= self._max_connects

    def on_event(self, kind, data):
        self.events.append((kind, data))

    def sleep(self, secs):
        self.sleeps.append(secs)


def _listener(driver, **kw):
    params = {"initial_backoff": 1.0, "max_backoff": 30.0}
    params.update(kw)
    return llama_sse.LlamaSseListener(
        connect=driver.connect,
        on_event=driver.on_event,
        should_stop=driver.should_stop,
        sleep=driver.sleep,
        **params,
    )


def test_listener_dispatches_parsed_json_events():
    conn = _connection(("model_status", '{"model": "m1", "status": "sleeping"}'))
    drv = _Driver([conn], max_connects=1)
    _listener(drv).run()
    assert drv.events == [("model_status", {"model": "m1", "status": "sleeping"})]


def test_listener_skips_non_json_data():
    conn = _connection(("model_status", "not-json"),
                       ("model_status", '{"status": "loaded"}'))
    drv = _Driver([conn], max_connects=1)
    _listener(drv).run()
    assert drv.events == [("model_status", {"status": "loaded"})]


def test_listener_reconnects_after_clean_stream_end():
    c1 = _connection(("model_status", '{"status": "loaded"}'))
    c2 = _connection(("model_status", '{"status": "sleeping"}'))
    drv = _Driver([c1, c2], max_connects=2)
    _listener(drv).run()
    assert drv.connect_calls == 2
    assert [e[1]["status"] for e in drv.events] == ["loaded", "sleeping"]


# ── select_wake_target (issue #140) ────────────────────────────────────

def test_wake_target_prefers_sleeping_over_first_listed():
    # Sleeping model wins over the first-listed entry.
    models = [
        {"id": "alpha", "status": {"value": "unloaded"}},
        {"id": "beta", "status": {"value": "sleeping"}},
    ]
    assert llama_sse.select_wake_target(models) == "beta"


def test_wake_target_prefers_sleeping_over_loaded():
    models = [
        {"id": "loaded-one", "status": {"value": "loaded"}},
        {"id": "sleeping-one", "status": {"value": "sleeping"}},
    ]
    assert llama_sse.select_wake_target(models) == "sleeping-one"


def test_wake_target_falls_back_to_loaded_when_none_sleeping():
    models = [
        {"id": "x", "status": {"value": "unloaded"}},
        {"id": "y", "status": {"value": "loaded"}},
    ]
    assert llama_sse.select_wake_target(models) == "y"


def test_wake_target_falls_back_to_first_when_no_status():
    # Older llama.cpp / single-model: no per-model status → first listed.
    assert llama_sse.select_wake_target([{"id": "one"}, {"id": "two"}]) == "one"


def test_wake_target_honors_explicit_request():
    models = [
        {"id": "alpha", "status": {"value": "sleeping"}},
        {"id": "beta", "status": {"value": "unloaded"}},
    ]
    assert llama_sse.select_wake_target(models, requested="beta") == "beta"


def test_wake_target_falls_back_when_requested_not_listed():
    # Stale/unknown id → fall back to the sleeping model, don't warm a ghost.
    models = [{"id": "alpha", "status": {"value": "sleeping"}}]
    assert llama_sse.select_wake_target(models, requested="ghost") == "alpha"
    assert llama_sse.select_wake_target(models, requested="  ") == "alpha"


def test_wake_target_empty_list_returns_none():
    assert llama_sse.select_wake_target([]) is None
    assert llama_sse.select_wake_target([{"status": {"value": "sleeping"}}]) is None


def test_wake_target_ignores_malformed_entries():
    models = ["bad", {"id": "good", "status": {"value": "sleeping"}}, None]
    assert llama_sse.select_wake_target(models) == "good"


def test_listener_stops_immediately_without_connecting():
    drv = _Driver([], max_connects=0)
    _listener(drv).run()
    assert drv.connect_calls == 0
    assert drv.events == []


def test_listener_backs_off_on_connect_error():
    # max_connects=2 lets the failed attempt sleep once, then the empty
    # follow-up connection trips should_stop before a second sleep.
    drv = _Driver([RuntimeError("boom")], max_connects=2)
    _listener(drv).run()
    assert drv.sleeps == [1.0]


def test_listener_no_backoff_sleep_when_events_received():
    c1 = _connection(("model_status", '{"status": "loaded"}'))
    drv = _Driver([c1], max_connects=1)
    _listener(drv).run()
    assert drv.sleeps == []


def test_listener_backoff_escalates_then_caps():
    errs = [RuntimeError("x")] * 8
    drv = _Driver(errs, max_connects=9)
    _listener(drv, max_backoff=10.0).run()
    assert drv.sleeps == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0, 10.0]


def test_listener_connected_flag_false_after_run():
    conn = _connection(("model_status", '{"status": "loaded"}'))
    drv = _Driver([conn], max_connects=1)
    lis = _listener(drv)
    lis.run()
    assert lis.connected is False


def test_listener_backoff_resets_after_established_connection():
    # Connect failures escalate (1,2,4); an established connection resets backoff
    # and reconnects after a brief fixed delay, so the next failure restarts at 1.
    err = RuntimeError("x")
    good = _connection(("model_status", '{"status": "loaded"}'))
    drv = _Driver([err, err, err, good, err], max_connects=6)
    _listener(drv, max_backoff=30.0).run()
    assert drv.sleeps == [1.0, 2.0, 4.0, 1.0, 1.0]


def test_listener_keepalive_only_stream_backs_off():
    drv = _Driver([[": keepalive", ""]], max_connects=2)
    _listener(drv).run()
    assert drv.events == []
    assert drv.sleeps == [1.0]


class _RecordingLogger:
    def __init__(self):
        self.infos = []
        self.warnings = []
        self.debugs = []

    def info(self, msg, *args):
        self.infos.append(msg % args if args else msg)

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def debug(self, msg, *args):
        self.debugs.append(msg % args if args else msg)


def test_listener_warns_after_consecutive_failures():
    log = _RecordingLogger()
    drv = _Driver([RuntimeError("x")] * 3, max_connects=4)
    _listener(drv, logger=log, fail_warn_threshold=3).run()
    assert len(log.warnings) == 1
    assert "3 consecutive" in log.warnings[0]


def test_listener_logs_connected_on_first_connect():
    log = _RecordingLogger()
    conn = _connection(("model_status", '{"status": "loaded"}'))
    drv = _Driver([conn], max_connects=1)
    _listener(drv, logger=log).run()
    assert sum("connected" in i for i in log.infos) == 1


def test_listener_logs_connected_once_across_clean_reconnects():
    log = _RecordingLogger()
    c1 = _connection(("model_status", '{"status": "loaded"}'))
    c2 = _connection(("model_status", '{"status": "sleeping"}'))
    drv = _Driver([c1, c2], max_connects=2)
    _listener(drv, logger=log).run()
    assert sum("connected" in i for i in log.infos) == 1


def test_listener_relogs_connected_after_sustained_down():
    log = _RecordingLogger()
    good = _connection(("model_status", '{"status": "loaded"}'))
    drv = _Driver([RuntimeError("x")] * 3 + [good], max_connects=5)
    _listener(drv, logger=log, fail_warn_threshold=3).run()
    assert len(log.warnings) == 1
    assert sum("connected" in i for i in log.infos) == 1


def test_listener_calls_on_disconnect_after_connection():
    calls = []
    conn = _connection(("model_status", '{"status": "loaded"}'))
    drv = _Driver([conn], max_connects=1)
    _listener(drv, on_disconnect=lambda: calls.append(1)).run()
    assert calls == [1]


def test_listener_idle_read_timeout_does_not_escalate():
    # An established stream that errors mid-iteration (idle read-timeout) must
    # reconnect promptly without fail-streak escalation or a 'down' WARNING.
    log = _RecordingLogger()
    calls = {"n": 0}
    sleeps = []

    def connect():
        calls["n"] += 1
        if calls["n"] <= 3:
            def gen():
                raise RuntimeError("Read timed out")
                yield  # unreachable; makes this a generator (opens, then raises)
            return gen()
        return iter([])

    lis = llama_sse.LlamaSseListener(
        connect=connect,
        on_event=lambda k, d: None,
        should_stop=lambda: calls["n"] >= 4,
        sleep=lambda s: sleeps.append(s),
        logger=log,
        fail_warn_threshold=2,
        initial_backoff=1.0,
        max_backoff=30.0,
    )
    lis.run()
    assert log.warnings == []
    assert sleeps == [1.0, 1.0, 1.0]


# ── status_value / progress_value ──────────────────────────────────────

def test_status_value_plain_string():
    assert llama_sse.status_value({"status": "sleeping"}) == "sleeping"


def test_status_value_nested_value_dict():
    assert llama_sse.status_value({"status": {"value": "loaded"}}) == "loaded"


def test_status_value_falls_back_to_state_key():
    assert llama_sse.status_value({"state": "loading"}) == "loading"


def test_status_value_none_when_absent():
    assert llama_sse.status_value({"model": "m"}) is None


def test_status_value_real_envelope_nested_data():
    # Real llama.cpp shape: {"model","event","data":{"status":...}}.
    env = {"model": "m", "event": "status_change", "data": {"status": "sleeping", "exit_code": 0}}
    assert llama_sse.status_value(env) == "sleeping"


@pytest.mark.parametrize("data,expected", [
    ({"progress": 42}, 42),
    ({"percent": 7}, 7),
    ({"pct": 3}, 3),
    ({"progress": 0}, 0),
    ({"data": {"progress": 88}}, 88),
    ({"other": 1}, None),
    ({}, None),
])
def test_progress_value(data, expected):
    assert llama_sse.progress_value(data) == expected


def test_event_kind_reads_real_envelope_event_key():
    env = {"model": "m", "event": "status_change", "data": {"status": "loaded"}}
    assert llama_sse.event_kind("message", env) == "status_change"


# ── parse_sse_stream extra framing ─────────────────────────────────────

def test_parse_handles_crlf_framing():
    lines = ["event: model_status\r", "data: {\"status\": \"sleeping\"}\r", "\r"]
    assert list(llama_sse.parse_sse_stream(lines)) == [
        ("model_status", "{\"status\": \"sleeping\"}")
    ]


def test_parse_event_field_resets_after_dispatch():
    # The second (untyped) event must fall back to "message", not reuse "a".
    lines = ["event: a", "data: 1", "", "data: 2", ""]
    assert list(llama_sse.parse_sse_stream(lines)) == [("a", "1"), ("message", "2")]


# ── requests_sse_lines (fake session, no real requests import) ──────────

class _FakeResp:
    def __init__(self, lines, status_error=None):
        self._lines = lines
        self._status_error = status_error
        self.closed = False

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.get_kwargs = None

    def get(self, url, **kw):
        self.get_kwargs = kw
        return self._resp


def test_requests_sse_lines_yields_and_maps_none_to_empty():
    resp = _FakeResp(["data: x", None, ""])
    out = list(llama_sse.requests_sse_lines("http://x/models/sse",
                                            session=_FakeSession(resp)))
    assert out == ["data: x", "", ""]
    assert resp.closed is True


def test_requests_sse_lines_closes_on_early_break():
    resp = _FakeResp(["data: a", "data: b", "data: c"])
    gen = llama_sse.requests_sse_lines("http://x/models/sse",
                                       session=_FakeSession(resp))
    next(gen)
    gen.close()
    assert resp.closed is True


def test_requests_sse_lines_propagates_status_error_and_closes():
    # Eager connect: raise_for_status fires at call time, not first next().
    err = RuntimeError("404")
    resp = _FakeResp([], status_error=err)
    with pytest.raises(RuntimeError):
        llama_sse.requests_sse_lines("http://x/models/sse",
                                     session=_FakeSession(resp))
    assert resp.closed is True


def test_requests_sse_lines_passes_stream_and_timeout():
    resp = _FakeResp([])
    sess = _FakeSession(resp)
    list(llama_sse.requests_sse_lines("http://x/models/sse", session=sess))
    assert sess.get_kwargs["stream"] is True
    assert sess.get_kwargs["timeout"] == (5.0, 300.0)
