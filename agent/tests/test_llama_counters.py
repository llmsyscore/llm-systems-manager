"""Windowed prompt-cache reuse and speculative acceptance from llama-server counters (#1303)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "llama_metrics_spec_mtp.txt"

_spec = importlib.util.spec_from_file_location(
    "llama_counters_t", _AGENT_ROOT / "providers" / "llama_counters.py")
lc = importlib.util.module_from_spec(_spec)
sys.modules["llama_counters_t"] = lc
_spec.loader.exec_module(lc)


def _text(processed, cached, drafted=None, accepted=None, drafts=None):
    lines = [
        "# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed, excluding cached tokens",
        "# TYPE llamacpp:prompt_tokens_total counter",
        f"llamacpp:prompt_tokens_total {processed}",
        f"llamacpp:prompt_tokens_cached_total {cached}",
        "llamacpp:tokens_predicted_total 500",
    ]
    if drafted is not None:
        lines += [f"llamacpp:spec_decode_num_draft_tokens_total {drafted}",
                  f"llamacpp:spec_decode_num_accepted_tokens_total {accepted}",
                  'llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 7']
    if drafts is not None:
        lines.append(f"llamacpp:spec_decode_num_drafts_total {drafts}")
    return "\n".join(lines) + "\n"


# --- parse_counters -------------------------------------------------------

def test_parse_captured_fixture():
    counters = lc.parse_counters(_FIXTURE.read_text())
    assert counters == {
        "prompt_processed": 262759,
        "prompt_cached": 238834,
        "spec_drafted": 23627,
        "spec_accepted": 16486,
        "spec_drafts": 7877,
    }


def test_parse_skips_labelled_comment_and_unsupported_lines():
    counters = lc.parse_counters(_text(10, 5))
    assert counters == {"prompt_processed": 10, "prompt_cached": 5}
    assert lc.parse_counters("# only comments\n") == {}
    assert lc.parse_counters("llamacpp:prompt_tokens_total NaN\n") == {}


# --- CounterWindow ---------------------------------------------------------

def _window(window_s=60.0):
    return lc.CounterWindow(window_s=window_s)


def test_issue_runs_cache_reuse_and_acceptance():
    """Run 1 cold (8,200 processed, 78 drafted/52 accepted); run 2 hits the cache."""
    w = _window()
    w.observe("m", 100.0, lc.parse_counters(_text(0, 0, 0, 0, 0)))
    w.observe("m", 105.0, lc.parse_counters(_text(8200, 0, 78, 52, 20)))
    cr, sa = w.cache_reuse(105.0), w.spec_accept(105.0)
    assert (cr["state"], cr["processed"], cr["cached"], cr["ratio"]) == ("ok", 8200, 0, 0.0)
    assert (sa["state"], sa["drafted"], sa["accepted"], sa["ratio"]) == ("ok", 78, 52, 0.6667)
    assert sa["accepted_per_draft"] == 2.6

    w2 = _window()
    w2.observe("m", 200.0, lc.parse_counters(_text(8200, 0, 78, 52, 20)))
    w2.observe("m", 205.0, lc.parse_counters(_text(8210, 8200, 171, 120, 45)))
    cr = w2.cache_reuse(205.0)
    assert cr["ratio"] == pytest.approx(8200 / 8210, abs=1e-4)
    assert cr["state"] == "ok"
    assert w2.spec_accept(205.0)["ratio"] == pytest.approx(68 / 93, abs=1e-4)


def test_unsupported_build_distinct_from_zero():
    w = _window()
    w.observe("m", 1.0, lc.parse_counters("llamacpp:tokens_predicted_total 3\n"))
    w.observe("m", 6.0, lc.parse_counters("llamacpp:tokens_predicted_total 9\n"))
    assert w.cache_reuse(6.0) == {"state": "unsupported", "processed": None, "cached": None, "ratio": None}
    assert w.spec_accept(6.0)["state"] == "unsupported"
    # Cache counters present but no speculative decoding configured.
    w.observe("m", 11.0, lc.parse_counters(_text(10, 5)))
    assert w.cache_reuse(11.0)["state"] == "idle"  # first sample after the names appeared: no deltas yet
    w.observe("m", 16.0, lc.parse_counters(_text(20, 5)))
    assert w.cache_reuse(16.0)["state"] == "ok"
    assert w.spec_accept(16.0)["state"] == "unsupported"


def test_idle_window_is_not_zero_percent():
    w = _window()
    w.observe("m", 0.0, lc.parse_counters(_text(100, 50, 10, 5)))
    w.observe("m", 5.0, lc.parse_counters(_text(100, 50, 10, 5)))
    cr = w.cache_reuse(5.0)
    assert (cr["state"], cr["ratio"], cr["processed"], cr["cached"]) == ("idle", None, 0, 0)
    assert w.spec_accept(5.0)["state"] == "idle"


def test_window_drops_old_samples():
    w = _window(window_s=10.0)
    w.observe("m", 0.0, lc.parse_counters(_text(0, 0)))
    w.observe("m", 4.0, lc.parse_counters(_text(100, 100)))
    assert w.cache_reuse(4.0)["ratio"] == 0.5
    w.observe("m", 20.0, lc.parse_counters(_text(100, 100)))
    # Both earlier samples aged out: nothing left to diff inside the window.
    assert w.cache_reuse(20.0)["state"] == "idle"
    w.observe("m", 25.0, lc.parse_counters(_text(100, 300)))
    assert w.cache_reuse(25.0)["ratio"] == 1.0


def test_counter_reset_clears_window_and_labels_state():
    w = _window()
    w.observe("m", 0.0, lc.parse_counters(_text(1000, 500, 80, 40)))
    w.observe("m", 5.0, lc.parse_counters(_text(1200, 600, 90, 45)))
    assert w.cache_reuse(5.0)["state"] == "ok"
    w.observe("m", 10.0, lc.parse_counters(_text(30, 0, 4, 2)))     # server restarted
    cr = w.cache_reuse(10.0)
    assert (cr["state"], cr["ratio"], cr["processed"]) == ("reset", None, 0)
    assert w.spec_accept(10.0)["state"] == "reset"
    w.observe("m", 15.0, lc.parse_counters(_text(60, 30, 8, 4)))
    assert w.cache_reuse(15.0)["state"] == "reset"           # still inside the window
    assert w.cache_reuse(15.0)["ratio"] == pytest.approx(0.5)
    w.observe("m", 71.0, lc.parse_counters(_text(90, 60, 12, 6)))
    assert w.cache_reuse(71.0)["state"] == "ok"          # reset label expires with the window


def test_model_switch_never_mixes_counters():
    w = _window()
    w.observe("a", 0.0, lc.parse_counters(_text(1000, 900, 50, 25)))
    w.observe("a", 5.0, lc.parse_counters(_text(1100, 1000, 60, 30)))
    assert w.cache_reuse(5.0)["ratio"] == 0.5
    w.observe("b", 10.0, lc.parse_counters(_text(10, 0, 0, 0)))
    cr = w.cache_reuse(10.0)
    assert (cr["state"], cr["processed"], cr["cached"]) == ("idle", 0, 0)
    assert w.model == "b"
    assert w.latest() == {"prompt_processed": 10, "prompt_cached": 0,
                          "spec_drafted": 0, "spec_accepted": 0}


def test_paused_poll_marks_state_and_keeps_window_deltas():
    w = _window()
    w.observe("m", 0.0, lc.parse_counters(_text(0, 0, 0, 0)))
    w.observe("m", 5.0, lc.parse_counters(_text(100, 300, 10, 8)))
    cr = w.cache_reuse(30.0, polled=False)
    assert (cr["state"], cr["ratio"]) == ("paused", 0.75)
    assert w.spec_accept(30.0, polled=False)["state"] == "paused"
    # Once the window has aged out, the paused read carries no deltas.
    assert w.cache_reuse(120.0, polled=False)["processed"] == 0


def test_spec_counters_at_zero_mean_speculation_off_not_idle():
    """Upstream emits the spec_decode counters at 0 when no draft model / MTP is configured."""
    w = _window()
    w.observe("m", 0.0, lc.parse_counters(_text(100, 0, 0, 0, 0)))
    w.observe("m", 5.0, lc.parse_counters(_text(900, 100, 0, 0, 0)))
    sa = w.spec_accept(5.0)
    assert (sa["state"], sa["drafted"], sa["accepted"], sa["ratio"]) == ("off", 0, 0, None)
    assert w.cache_reuse(5.0)["state"] == "ok"
    w.observe("m", 10.0, lc.parse_counters(_text(900, 100, 12, 9, 4)))
    assert w.spec_accept(10.0)["state"] == "ok"
    assert w.spec_accept(10.0)["ratio"] == 0.75


def test_external_reset_clears_ring_and_labels_next_window():
    w = _window()
    w.observe("m", 0.0, lc.parse_counters(_text(1000, 500, 80, 40)))
    w.observe("m", 5.0, lc.parse_counters(_text(1200, 600, 90, 45)))
    w.reset(8.0)
    assert w.latest() == {}
    # Counters climbed past the old values before the next poll: no inflated delta.
    w.observe("m", 10.0, lc.parse_counters(_text(1500, 100, 95, 50)))
    cr = w.cache_reuse(10.0)
    assert (cr["state"], cr["processed"], cr["cached"], cr["ratio"]) == ("reset", 0, 0, None)
    fresh = _window()
    fresh.reset(1.0)   # nothing recorded yet: no reset label to carry
    fresh.observe("m", 2.0, lc.parse_counters(_text(10, 0)))
    assert fresh.cache_reuse(2.0)["state"] == "idle"
