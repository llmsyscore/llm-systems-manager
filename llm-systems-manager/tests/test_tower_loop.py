"""#924 Tower loop: model resolution, tool-call parsing (native + fenced), caps, refusal, store."""
from __future__ import annotations

import json
import types

import pytest

import tower
import tower_tools as tt

ENTRIES = [{"id": "qwen3-14b", "provider": "llama", "status": {"value": "loaded"}},
           {"id": "nomic-embed-text", "provider": "lms"},
           {"id": "gemma-3-12b", "provider": "lms"}]


def _cfg(**over):
    d = dict(enabled=True, model="", tool_mode="prompt", capabilities="read", off_topic="refuse",
             disabled_tools=[], max_tool_calls=3, max_tokens=256, temperature=0.2, history_days=30)
    d.update(over)
    return types.SimpleNamespace(**d)


def test_resolve_model_treats_auto_and_blank_as_no_pin():
    entries = [{"id": "nomic-embed", "provider": "lms", "status": {"value": "loaded"}},
               {"id": "gemma-4", "provider": "lms", "status": {"value": "loaded"}}]
    assert tower.resolve_model(_cfg(model="auto"), entries)["model"] == "gemma-4"
    assert tower.resolve_model(_cfg(model="AUTO"), entries)["model"] == "gemma-4"
    assert tower.resolve_model(_cfg(model=""), entries)["model"] == "gemma-4"


def test_resolve_model_prefers_pin_then_first_chat_model():
    assert tower.resolve_model(_cfg(model="gemma-3-12b"), ENTRIES)["model"] == "gemma-3-12b"
    assert tower.resolve_model(_cfg(model="missing"), ENTRIES)["model"] == "qwen3-14b"
    assert tower.resolve_model(_cfg(), [ENTRIES[1]]) is None


def test_parse_tool_call_native_and_fenced():
    native = {"tool_calls": [{"function": {"name": "alarms", "arguments": json.dumps({"count": 3})}}]}
    assert tower.parse_tool_call(native) == ("alarms", {"count": 3})
    fenced = {"content": 'Let me look.\n```tool\n{"name": "host_detail", "args": {"host": "box"}}\n```'}
    assert tower.parse_tool_call(fenced) == ("host_detail", {"host": "box"})
    assert tower.parse_tool_call({"content": "plain answer"}) is None
    assert tower.parse_tool_call({"content": "```tool\nnot json\n```"}) is None


def _registry():
    deps = {"host": lambda n: {"hostname": n, "gpu_temp_c": 91}, "hosts": lambda: [], "models": lambda h=None, p=None: [],
            "alarms": lambda s="active", c=10, w=None, h=None, r=None: [{"id": "a1"}], "alert": lambda a: None,
            "alarm_history": lambda w="30d", g="rule", t=10, h=None, r=None: {"total": 1}, "energy": lambda w="today": {},
            "flow": lambda: {}, "runs": lambda t=None, c=5: [], "speed": lambda m: [], "health": lambda: {},
            "log_tail": lambda h, p="llama", n=40: [], "config_get": lambda p: {}, "help": lambda t: ""}
    return tt.build_registry(deps)


class _TimeoutError(RuntimeError):
    err_type = "timeout"
    status = 504


def _run(script, cfg=None, user_text="why is box red?", store=None, cancelled=None, alternates=None):
    """script: list of assistant messages the fake model returns, in order, delivered as
    stream deltas — "content" char by char, "chunks" verbatim as given, or one tool_calls
    delta chunk per native reply (one fragment per entry, each keeping its own index)."""
    calls = iter(script)
    seen = {"payloads": []}
    def complete_json(body, *, label):
        raise AssertionError("run_turn must stream")
    def complete_stream(body, *, label, **kw):
        seen["payloads"].append(body)
        seen.setdefault("timeouts", []).append(kw.get("read_timeout"))
        msg = next(calls)
        if "timeout" in msg:
            raise _TimeoutError()
        if "chunks" in msg:
            for part in msg["chunks"]:
                yield {"choices": [{"delta": {"content": part}}]}
        elif "tool_calls" in msg:
            frags = []
            for i, tc in enumerate(msg["tool_calls"]):
                fn = tc.get("function") or {}
                frags.append({"index": tc.get("index", i), "id": tc.get("id", f"call_{i}"),
                              "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments", "")}})
            yield {"choices": [{"delta": {"tool_calls": frags}}]}
        else:
            for ch in msg.get("content") or "":
                yield {"choices": [{"delta": {"content": ch}}]}
    events = []
    st = store or tower.Store(":memory:")
    tid = st.create_thread("adriel", "t", {})
    out = tower.run_turn(thread_id=tid, user_text=user_text, page={"tab": "overall"}, cfg=cfg or _cfg(), role="operator",
                         registry=_registry(), complete_json=complete_json, complete_stream=complete_stream,
                         store=st, emit=events.append, model={"model": "qwen3-14b", "provider": "llama", "hosts": ["box"]},
                         cancelled=cancelled or (lambda: False), alternates=alternates)
    return out, events, seen, st, tid


def test_turn_reads_a_tool_then_streams_the_answer():
    out, events, seen, st, tid = _run([
        {"content": '```tool\n{"name":"host_detail","args":{"host":"box"}}\n```'},
        {"content": "box is hot: 91 °C."},
    ])
    kinds = [e["event"] for e in events]
    assert kinds[:2] == ["model", "status"] and "tool" in kinds and kinds[-1] == "done"
    tool = next(e for e in events if e["event"] == "tool")
    assert tool["name"] == "host_detail" and tool["ok"] is True and tool["summary"].startswith("read host detail · box")
    assert "".join(e["text"] for e in events if e["event"] == "delta") == "box is hot: 91 °C."
    rows = st.messages(tid)
    assert [r["role"] for r in rows] == ["user", "tool", "assistant"]
    # tool results go back to the model as a role=="tool" message (native) or a
    # role=="user" "Result of <name>" message (prompt mode, used here)
    second_payload_messages = seen["payloads"][1]["messages"]
    assert (any(m.get("role") == "tool" for m in second_payload_messages)
            or any("Result of host_detail" in (m.get("content") or "") for m in second_payload_messages))


def test_preamble_before_a_fenced_tool_call_streams_but_the_fence_never_does():
    out, events, *_ = _run([
        {"content": 'Let me look.\n```tool\n{"name":"host_detail","args":{"host":"box"}}\n```'},
        {"content": "box is hot: 91 °C."},
    ])
    kinds = [e["event"] for e in events]
    assert "tool" in kinds
    tool = next(e for e in events if e["event"] == "tool")
    assert tool["name"] == "host_detail" and tool["ok"] is True
    preamble_deltas = "".join(e["text"] for e in events[:events.index(next(e for e in events if e["event"] == "tool"))]
                              if e["event"] == "delta")
    assert preamble_deltas == "Let me look.\n"


def test_natural_finish_streams_multiple_delta_events():
    out, events, *_ = _run([
        {"content": '```tool\n{"name":"host_detail","args":{"host":"box"}}\n```'},
        {"content": "box is hot: 91 °C."},
    ])
    deltas = [e for e in events if e["event"] == "delta"]
    assert len(deltas) > 1
    assert "".join(e["text"] for e in deltas) == "box is hot: 91 °C."


def test_fenced_tool_block_is_never_emitted_as_delta():
    out, events, *_ = _run([
        {"content": '```tool\n{"name":"host_detail","args":{"host":"box"}}\n```'},
        {"content": "done"},
    ])
    seen_text = "".join(e["text"] for e in events if e["event"] == "delta")
    assert "```" not in seen_text and "host_detail" not in seen_text


def test_malformed_tool_fence_is_emitted_live_and_stored_verbatim():
    out, events, seen, st, tid = _run([{"content": '```tool\nnot json\n```'}])
    emitted = "".join(e["text"] for e in events if e["event"] == "delta")
    stored = st.messages(tid)[-1]["content"]
    assert emitted == '```tool\nnot json\n```'
    assert stored == emitted


def test_answer_ending_in_inline_code_streams_and_stores_the_closing_backtick():
    content = "Run: `systemctl restart llm-systems-manager`"
    out, events, seen, st, tid = _run([{"content": content}])
    emitted = "".join(e["text"] for e in events if e["event"] == "delta")
    stored = st.messages(tid)[-1]["content"]
    assert emitted == content
    assert stored == emitted
    assert emitted[-1] == "`"


def test_non_tool_fence_releases_buffering_and_resumes_live_streaming():
    content = "Run this:\n```bash\nls -l\n```\nThat lists files."
    out, events, seen, st, tid = _run([{"content": content}])
    deltas = [e for e in events if e["event"] == "delta"]
    emitted = "".join(e["text"] for e in deltas)
    stored = st.messages(tid)[-1]["content"]
    assert len(deltas) > 2
    assert emitted == content
    assert stored == emitted


def test_malformed_fence_then_valid_tool_block_runs_the_valid_one():
    content = '```tool\nnot json\n```\n```tool\n{"name":"alarms","args":{}}\n```'
    out, events, seen, st, tid = _run([
        {"content": content},
        {"content": "there is 1 alert"},
    ])
    tool_idx = next(i for i, e in enumerate(events) if e["event"] == "tool")
    assert events[tool_idx]["name"] == "alarms" and events[tool_idx]["ok"] is True
    round1_deltas = "".join(e["text"] for e in events[:tool_idx] if e["event"] == "delta")
    assert round1_deltas == '```tool\nnot json\n```\n'


def _pre_tool_deltas(events):
    """Delta text emitted before the first tool event."""
    stop = next(i for i, e in enumerate(events) if e["event"] == "tool")
    return "".join(e["text"] for e in events[:stop] if e["event"] == "delta")


def test_coalesced_chunks_hold_the_tool_fence_and_stream_the_code_block():
    out, events, seen, st, tid = _run([
        {"chunks": ["A\n", "```", "py\nx=1\n```\n", "```", "tool\n",
                    '{"name":"alarms","args":{}}', "\n```"]},
        {"content": "1 alert"},
    ])
    tool = next(e for e in events if e["event"] == "tool")
    assert tool["name"] == "alarms" and tool["ok"] is True
    shown = _pre_tool_deltas(events)
    assert shown == "A\n```py\nx=1\n```\n"
    assert "```tool" not in shown and "alarms" not in shown
    assert st.messages(tid)[1]["content"] == shown


def test_empty_fence_pair_streams_as_text():
    content = "Note:\n```\n```\nend."
    out, events, seen, st, tid = _run([{"content": content}])
    emitted = "".join(e["text"] for e in events if e["event"] == "delta")
    assert emitted == content and st.messages(tid)[-1]["content"] == content
    assert not any(e["event"] == "tool" for e in events)


def test_tool_call_with_trailing_prose_stores_exactly_what_was_emitted():
    out, events, seen, st, tid = _run([
        {"chunks": ['Still checking.\n```tool\n{"name":"alarms","args":{}}\n```\nmore text after.']},
        {"content": "1 alert"},
    ])
    assert next(e for e in events if e["event"] == "tool")["ok"] is True
    shown = _pre_tool_deltas(events)
    assert shown == "Still checking.\n"
    assert st.messages(tid)[1]["content"] == shown
    assert not any("more text after." in (e.get("text") or "") for e in events)


def test_code_block_then_tool_call_in_one_chunk_streams_only_the_code_block():
    out, events, seen, st, tid = _run([
        {"chunks": ['Checking.\n```bash\nls\n```\n```tool\n{"name":"alarms","args":{}}\n```']},
        {"content": "1 alert"},
    ])
    assert next(e for e in events if e["event"] == "tool")["name"] == "alarms"
    shown = _pre_tool_deltas(events)
    assert shown == "Checking.\n```bash\nls\n```\n"
    assert st.messages(tid)[1]["content"] == shown


def test_tool_fence_split_across_chunk_boundaries_is_never_emitted():
    out, events, seen, st, tid = _run([
        {"chunks": ["Look", ":\n", "``", "`to", "ol\n", '{"name":"alar', 'ms","args":{}}\n', "``", "`"]},
        {"content": "1 alert"},
    ])
    tool = next(e for e in events if e["event"] == "tool")
    assert tool["name"] == "alarms" and tool["ok"] is True
    assert _pre_tool_deltas(events) == "Look:\n"


def test_inline_backticks_stream_untouched():
    content = "the `--ctx-size` flag"
    out, events, seen, st, tid = _run([{"content": content}])
    deltas = [e for e in events if e["event"] == "delta"]
    assert "".join(e["text"] for e in deltas) == content
    assert len(deltas) > 5 and st.messages(tid)[-1]["content"] == content


def test_tool_fence_not_at_line_start_is_treated_as_text():
    content = 'See ```tool\n{"name":"alarms","args":{}}\n``` for the syntax.'
    out, events, seen, st, tid = _run([{"content": content}])
    emitted = "".join(e["text"] for e in events if e["event"] == "delta")
    assert emitted == content and st.messages(tid)[-1]["content"] == content
    assert not any(e["event"] == "tool" for e in events)


def test_forced_final_stores_only_the_emitted_preamble_not_the_withheld_fence():
    script = [{"content": '```tool\n{"name":"alarms","args":{}}\n```'}] * 2 + [
        {"content": 'Still checking things.\n```tool\n{"name":"alarms","args":{}}\n```'},
    ]
    out, events, seen, st, tid = _run(script, cfg=_cfg(max_tool_calls=1))
    emitted = "".join(e["text"] for e in events if e["event"] == "delta")
    stored = st.messages(tid)[-1]["content"]
    assert stored == "Still checking things.\n"
    assert stored == emitted


def test_natural_finish_empty_answer_gets_a_fixed_fallback_line():
    out, events, seen, st, tid = _run([{"content": ""}])
    fallback = "I could not produce an answer; try rephrasing."
    stored = st.messages(tid)[-1]["content"]
    assert stored == fallback
    assert any(e["event"] == "delta" and e["text"] == fallback for e in events)


def test_native_tool_call_via_deltas_is_parsed_and_run():
    out, events, seen, st, tid = _run([
        {"tool_calls": [{"id": "call_0", "function": {"name": "alarms", "arguments": json.dumps({"count": 3})}}]},
        {"content": "there is 1 alert"},
    ], cfg=_cfg(tool_mode="native"))
    tool = next(e for e in events if e["event"] == "tool")
    assert tool["name"] == "alarms" and tool["ok"] is True
    rows = st.messages(tid)
    assert [r["role"] for r in rows] == ["user", "tool", "assistant"]
    assert any(m.get("role") == "tool" for m in seen["payloads"][1]["messages"])


def test_multiple_native_tool_calls_only_first_is_kept_in_the_assistant_message():
    out, events, seen, st, tid = _run([
        {"tool_calls": [{"id": "call_0", "function": {"name": "alarms", "arguments": "{}"}},
                         {"id": "call_1", "function": {"name": "hosts_overview", "arguments": "{}"}}]},
        {"content": "done"},
    ], cfg=_cfg(tool_mode="native"))
    assert sum(1 for e in events if e["event"] == "tool") == 1
    second_payload = seen["payloads"][1]["messages"]
    assistant_msg = next(m for m in second_payload if m.get("role") == "assistant")
    assert len(assistant_msg.get("tool_calls") or []) == 1
    assert assistant_msg["tool_calls"][0]["id"] == "call_0"


def test_turn_stops_at_max_tool_calls():
    script = [{"content": '```tool\n{"name":"alarms","args":{}}\n```'}] * 3 + [{"content": "gave up"}]
    out, events, *_ = _run(script, cfg=_cfg(max_tool_calls=2))
    assert sum(1 for e in events if e["event"] == "tool") == 2
    assert out["calls"] == 2 and "stopped after 2" in out.get("note", "")


def test_forced_final_empty_answer_gets_a_fixed_fallback_line():
    script = [{"content": '```tool\n{"name":"alarms","args":{}}\n```'}] * 2 + [{"content": ""}]
    out, events, seen, st, tid = _run(script, cfg=_cfg(max_tool_calls=1))
    fallback = "I stopped after 1 tool calls without a final answer; ask again with a narrower question."
    assert out["ok"] is True
    rows = st.messages(tid)
    assert rows[-1]["role"] == "assistant" and rows[-1]["content"] == fallback
    assert any(e["event"] == "delta" and e["text"] == fallback for e in events)


def test_unknown_or_invalid_tool_becomes_a_tool_error_not_a_crash():
    out, events, *_ = _run([
        {"content": '```tool\n{"name":"rm_rf","args":{}}\n```'},
        {"content": '```tool\n{"name":"log_tail","args":{"host":"box","lines":9999}}\n```'},
        {"content": "ok"},
    ])
    tools = [e for e in events if e["event"] == "tool"]
    assert tools[0]["ok"] is False and "not available" in tools[0]["summary"]
    assert tools[1]["ok"] is False and "lines" in tools[1]["summary"]


def test_off_topic_refuse_is_in_the_prompt_and_native_mode_sends_tools():
    _, _, seen, *_ = _run([{"content": "hi"}], cfg=_cfg(tool_mode="native"))
    body = seen["payloads"][0]
    assert tower.REFUSAL in body["messages"][0]["content"]
    assert body["tools"][0]["function"]["name"] and "```tool" in body["messages"][0]["content"]
    _, _, seen2, *_ = _run([{"content": "hi"}], cfg=_cfg(off_topic="allow"))
    assert tower.REFUSAL not in seen2["payloads"][0]["messages"][0]["content"]


def test_model_error_emits_error_event():
    def boom(body, *, label):
        import gateway
        raise gateway.GatewayError("upstream 502", 502, "upstream")
    st = tower.Store(":memory:")
    tid = st.create_thread("u", "t", {})
    events = []
    out = tower.run_turn(thread_id=tid, user_text="x", page={}, cfg=_cfg(), role="operator", registry=_registry(),
                         complete_json=boom, complete_stream=boom, store=st, emit=events.append,
                         model={"model": "m", "provider": "llama", "hosts": ["box"]}, cancelled=lambda: False)
    assert events[-1]["event"] == "error" and out["ok"] is False
    assert "upstream" not in events[-1]["message"] and "502" not in events[-1]["message"]


def test_cancel_mid_stream_stops_without_storing_a_partial_answer():
    def complete_stream(body, *, label):
        yield {"choices": [{"delta": {"content": "he"}}]}
        yield {"choices": [{"delta": {"content": "llo"}}]}
    seen_calls = {"n": 0}
    def cancelled():
        seen_calls["n"] += 1
        return seen_calls["n"] > 2
    st = tower.Store(":memory:")
    tid = st.create_thread("u", "t", {})
    events = []
    out = tower.run_turn(thread_id=tid, user_text="x", page={}, cfg=_cfg(), role="operator", registry=_registry(),
                         complete_stream=complete_stream, store=st, emit=events.append,
                         model={"model": "m", "provider": "llama", "hosts": ["box"]}, cancelled=cancelled)
    assert out["ok"] is False
    assert events[-1]["event"] == "error" and events[-1]["message"] == "Stopped."
    assert [r["role"] for r in st.messages(tid)] == ["user"]


def test_store_threads_are_per_user_and_sweep_keeps_live_threads_messages():
    st = tower.Store(":memory:")
    a = st.create_thread("alice", "first question", {"tab": "overall"})
    st.create_thread("bob", "other", {})
    assert [t["id"] for t in st.list_threads("alice")] == [a]
    assert st.get_thread("bob", a) is None
    live = st.create_thread("alice", "still active", {})
    st.add_message(a, "user", "hi")
    st.add_message(live, "user", "hi")
    st._conn().execute("UPDATE tower_threads SET updated = updated - 40*86400 WHERE id=?", (a,))
    # a live thread can still carry an old message row; sweep must not touch it
    st._conn().execute("UPDATE tower_messages SET ts = ts - 40*86400 WHERE thread_id=?", (live,))
    st._conn().commit()
    assert st.sweep(30) >= 1
    assert [t["id"] for t in st.list_threads("alice")] == [live]
    assert st.messages(a) == []
    assert len(st.messages(live)) == 1


def test_history_merges_same_role_rows_so_the_next_turn_alternates():
    st = tower.Store(":memory:")
    tid = st.create_thread("adriel", "t", {})
    # a prior turn with a preamble: assistant, tool, assistant
    st.add_message(tid, "user", "why is box red?")
    st.add_message(tid, "assistant", "Let me check.")
    st.add_message(tid, "tool", '{"gpu_temp_c": 91}', tool_name="host_detail", tool_ok=True, tool_ms=1)
    st.add_message(tid, "assistant", "box is hot: 91 \u00b0C.")
    seen = {"payloads": []}
    def complete_stream(body, *, label):
        seen["payloads"].append(body)
        yield {"choices": [{"delta": {"content": "still hot."}}]}
    tower.run_turn(thread_id=tid, user_text="and now?", page={}, cfg=_cfg(), role="operator",
                   registry=_registry(), complete_stream=complete_stream, store=st, emit=lambda e: None,
                   model={"model": "m", "provider": "llama", "hosts": ["box"]}, cancelled=lambda: False)
    msgs = seen["payloads"][0]["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "user"]
    assert all(a != b for a, b in zip(roles[1:], roles[2:])), roles
    assert msgs[2]["content"] == "Let me check.\nbox is hot: 91 \u00b0C."
    assert msgs[3]["content"] == "and now?"


def _held_stream(parts):
    """complete_stream fake that keeps a strong reference to the generator it hands out."""
    held = {}
    def complete_stream(body, *, label):
        def gen():
            try:
                for part in parts:
                    yield {"choices": [{"delta": {"content": part}}]}
            finally:
                held["closed"] = held.get("closed", 0) + 1
        g = gen()
        held["gen"] = g
        return g
    return complete_stream, held


def test_early_stop_closes_the_completion_generator():
    cs, held = _held_stream(['```tool\n{"name":"alarms","args":{}}\n```\n', "never read"])
    msg, shown = tower._stream_reply(cs, {"messages": []}, lambda e: None, lambda: False)
    assert tower.parse_tool_call(msg) == ("alarms", {})
    assert held["gen"].gi_frame is None and held["closed"] == 1


def test_cancel_closes_the_completion_generator():
    cs, held = _held_stream(["he", "llo"])
    n = {"i": 0}
    def cancelled():
        n["i"] += 1
        return n["i"] > 1
    with pytest.raises(tower._Cancelled):
        tower._stream_reply(cs, {"messages": []}, lambda e: None, cancelled)
    assert held["gen"].gi_frame is None and held["closed"] == 1


def test_parse_tool_call_accepts_model_native_tag_forms():
    hermes = {"content": '<tool_call> <function=tool> {"name": "energy_summary", "args": {"window": "today"}} </tool_call>'}
    assert tower.parse_tool_call(hermes) == ("energy_summary", {"window": "today"})
    qwen = {"content": '<tool_call>\n{"name": "alarms", "arguments": {"count": 2}}\n</tool_call>'}
    assert tower.parse_tool_call(qwen) == ("alarms", {"count": 2})
    bare = {"content": '<tool_call> tool {"name": "models", "args": {"provider": "llama"}} </tool_call>'}
    assert tower.parse_tool_call(bare) == ("models", {"provider": "llama"})
    llama = {"content": '<function=host_detail>{"host": "box"}</function>'}
    assert tower.parse_tool_call(llama) == ("host_detail", {"host": "box"})
    assert tower.parse_tool_call({"content": "<tool_call> not json </tool_call>"}) is None
    assert tower.parse_tool_call({"content": "<b>bold</b> answer"}) is None


def test_native_tag_call_is_held_back_run_and_never_streamed():
    out, events, *_ = _run([
        {"content": 'Checking.\n<tool_call> <function=tool> {"name": "alarms", "args": {}} </tool_call>\n'},
        {"content": "One alert."},
    ])
    assert out["ok"] is True and out["calls"] == 1
    deltas = "".join(e["text"] for e in events if e["event"] == "delta")
    assert "<tool_call>" not in deltas and "alarms" not in deltas
    assert deltas == "Checking.\nOne alert."
    assert [e["name"] for e in events if e["event"] == "tool"] == ["alarms"]


def test_bare_word_tag_call_is_held_back_and_run():
    out, events, *_ = _run([
        {"content": 'What used the most power today?\n<tool_call> tool {"name": "alarms", "args": {}} </tool_call>\n'},
        {"content": "Box."},
    ])
    assert out["calls"] == 1
    assert "".join(e["text"] for e in events if e["event"] == "delta") == "What used the most power today?\nBox."


def test_native_tag_call_without_trailing_newline_still_runs():
    out, events, *_ = _run([
        {"content": '<function=alarms>{"count": 2}</function>'},
        {"content": "Two."},
    ])
    assert out["calls"] == 1
    assert "".join(e["text"] for e in events if e["event"] == "delta") == "Two."


def test_lines_starting_with_an_angle_bracket_still_stream():
    out, events, *_ = _run([{"content": "<5 ms latency\n<b>not a call</b>\n"}])
    assert out["calls"] == 0
    assert "".join(e["text"] for e in events if e["event"] == "delta") == "<5 ms latency\n<b>not a call</b>\n"


def test_request_timeout_reaches_the_gateway_and_times_out_with_a_clear_error():
    out, events, seen, st, tid = _run([{"timeout": True}], cfg=_cfg(request_timeout_s=30))
    assert seen["timeouts"] == [30]
    assert out["ok"] is False
    err = [e for e in events if e["event"] == "error"][0]
    assert err["message"] == "qwen3-14b did not start answering within 30 s."
    assert [r["role"] for r in st.messages(tid)] == ["user"]


def test_no_timeout_configured_means_no_read_timeout_kwarg():
    _, _, seen, *_ = _run([{"content": "hi"}])
    assert seen["timeouts"] == [None]


def test_fallback_hands_this_question_to_the_next_model_and_discloses_it():
    alt = {"model": "gemma-4", "provider": "lms", "hosts": ["mac"]}
    out, events, seen, st, tid = _run(
        [{"timeout": True}, {"content": '```tool\n{"name":"alarms","args":{}}\n```'}, {"content": "One alert."}],
        cfg=_cfg(request_timeout_s=20, fallback=True), alternates=lambda cur: alt if cur["model"] == "qwen3-14b" else None)
    assert out["ok"] is True and out["calls"] == 1 and out["note"] == "fallback from qwen3-14b"
    models = [e for e in events if e["event"] == "model"]
    assert models[0]["model"] == "qwen3-14b" and "fallback" not in models[0]
    assert models[1] == {"event": "model", "model": "gemma-4", "provider": "lms", "hosts": ["mac"], "fallback": True, "from": "qwen3-14b"}
    assert [b["model"] for b in seen["payloads"]] == ["qwen3-14b", "gemma-4", "gemma-4"]
    text = "".join(e["text"] for e in events if e["event"] == "delta")
    preface = "Fallback: asking gemma-4 because qwen3-14b did not respond within 20 s.\n\n"
    assert text == preface + "One alert."
    rows = st.messages(tid)
    assert rows[-1]["role"] == "assistant" and rows[-1]["content"] == preface + "One alert."


def test_fallback_is_off_by_default_and_never_chains():
    alt = {"model": "gemma-4", "provider": "lms", "hosts": ["mac"]}
    out, events, seen, *_ = _run([{"timeout": True}], cfg=_cfg(request_timeout_s=20), alternates=lambda cur: alt)
    assert out["ok"] is False and len(seen["payloads"]) == 1
    out, events, seen, *_ = _run([{"timeout": True}, {"timeout": True}], cfg=_cfg(request_timeout_s=20, fallback=True),
                                 alternates=lambda cur: alt)
    assert out["ok"] is False and len(seen["payloads"]) == 2
    assert [e for e in events if e["event"] == "error"][0]["message"] == "gemma-4 did not start answering within 20 s."


def test_alternate_model_skips_the_current_and_non_chat_models_and_prefers_another_host():
    entries = [{"id": "qwen3-14b", "provider": "llama", "status": {"value": "loaded"}, "hosts": ["box"]},
               {"id": "nomic-embed", "provider": "lms", "status": {"value": "loaded"}, "hosts": ["mac"]},
               {"id": "bonsai", "provider": "lms", "status": {"value": "loaded"}, "hosts": ["mac"]},
               {"id": "gemma-4", "provider": "lms", "status": {"value": "loaded"}, "hosts": ["mac"]},
               {"id": "cold", "provider": "lms", "status": {"value": "unloaded"}, "hosts": ["mac"]}]
    assert tower.alternate_model({"model": "qwen3-14b", "hosts": ["box"]}, entries)["model"] == "bonsai"
    assert tower.alternate_model({"model": "bonsai", "hosts": ["mac"]}, entries)["model"] == "qwen3-14b"
    assert tower.alternate_model({"model": "bonsai", "hosts": ["mac"]}, entries[1:])["model"] == "gemma-4"
    assert tower.alternate_model({"model": "qwen3-14b"}, entries[:2]) is None


def test_history_drops_earlier_refusals_once_off_topic_is_allowed():
    st = tower.Store(":memory:")
    tid = st.create_thread("adriel", "t", {})
    st.add_message(tid, "user", "capital of France?")
    st.add_message(tid, "assistant", tower.REFUSAL)
    st.add_message(tid, "user", "why is box red?")
    st.add_message(tid, "assistant", "GPU is hot.")
    st.add_message(tid, "user", "capital of France?")
    kept = tower._history(st, tid)
    assert [m["content"] for m in kept] == ["capital of France?", tower.REFUSAL, "why is box red?", "GPU is hot.", "capital of France?"]
    allowed = tower._history(st, tid, drop_refusals=True)
    assert [m["content"] for m in allowed] == ["why is box red?", "GPU is hot.", "capital of France?"]
    _, _, seen, *_ = _run([{"content": "Paris."}], cfg=_cfg(off_topic="allow"), store=st, user_text="capital of France?")
    sent = [m["content"] for m in seen["payloads"][0]["messages"][1:]]
    assert tower.REFUSAL not in sent and sent[-1] == "capital of France?"
