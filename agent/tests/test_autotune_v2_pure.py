"""#880: autotune v2 pure helpers (stdlib-only module, no agent deps)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def at():
    spec = importlib.util.spec_from_file_location(
        "llama_autotune", _AGENT_ROOT / "providers" / "llama_autotune.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llama_autotune"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── request validation ─────────────────────────────────────────────

def test_v1_body_maps_to_fit_with_only_context(at):
    req = at.validate_request({"model_ids": "org/m:Q4", "target_mb": 900, "tolerance_mb": 30,
                               "optional_params": {"ctk": "q8_0"}},
                              v1_args=lambda p: ["-ctk", p["ctk"]])
    assert req["model_ids"] == ["org/m:Q4"] and req["objective"] == "fit"
    ctx = req["dims"]["context"]
    assert ctx == {"on": True, "target_mb": 900, "tolerance_mb": 30, "custom_args": ["-ctk", "q8_0"]}
    assert all(not req["dims"][d]["on"] for d in ("kv", "moe", "threads", "slots", "spec", "sampling"))


def test_v2_defaults_fill_every_dim(at):
    req = at.validate_request({"model_ids": ["m"], "objective": "balanced"})
    assert req["budget_min"] == 120
    assert set(req["dims"]) == {"context", "kv", "moe", "threads", "slots", "spec", "sampling"}
    assert req["dims"]["kv"] == {"on": True, "candidates": ["f16", "q8_0", "q4_0"], "guard_kl_max": 0.02}
    assert req["dims"]["spec"]["draft_model"] == "auto" and req["dims"]["spec"]["n_max"] == 16
    assert req["dims"]["slots"]["min_ctx_per_slot"] == 32768


@pytest.mark.parametrize("body", [
    {"model_ids": [], "objective": "fit"},
    {"model_ids": ["m"], "objective": "fastest"},
    {"model_ids": ["m"], "objective": "fit", "budget_min": 4},
    {"model_ids": ["m"], "objective": "fit", "budget_min": 601},
    {"model_ids": ["m"], "objective": "fit", "dims": {"kv": {"candidates": ["q3_k"]}}},
    {"model_ids": ["m"], "objective": "fit", "dims": {"kv": {"candidates": list("abcdefghi")}}},
    {"model_ids": ["m"], "objective": "fit", "dims": {"threads": {"candidates": [0]}}},
    {"model_ids": ["m"], "objective": "fit", "dims": {"threads": {"candidates": [257]}}},
    {"model_ids": ["m"], "objective": "fit", "dims": {"slots": {"candidates": [65]}}},
    {"model_ids": ["m"], "objective": "fit", "dims": {"spec": {"types": ["draft-eagle3"]}}},
    {"model_ids": ["m"], "objective": "fit", "dims": {"spec": {"draft_model": "../x.gguf"}}},
    {"model_ids": ["m"], "objective": "fit", "dims": {"moe": {"min": 5, "max": 2}}},
])
def test_v2_rejects_bad_bodies(at, body):
    with pytest.raises(ValueError):
        at.validate_request(body)


def test_draft_model_path_must_live_in_cache_root(at, tmp_path):
    root = tmp_path / "hub"
    good = root / "models--o--d" / "snapshots" / "a" / "d.gguf"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"x")
    req = at.validate_request({"model_ids": ["m"], "objective": "fit",
                               "dims": {"spec": {"draft_model": str(good)}}}, cache_root=root)
    assert req["dims"]["spec"]["draft_model"] == str(good.resolve())
    outside = tmp_path / "d.gguf"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        at.validate_request({"model_ids": ["m"], "objective": "fit",
                             "dims": {"spec": {"draft_model": str(outside)}}}, cache_root=root)


# ── argv from a config section ─────────────────────────────────────

def test_section_args_merges_and_drops(at):
    sec = {"hf-repo": "o/r", "hf-file": "q4.gguf", "ctx-size": "32768", "cache-type-k": "f16",
           "ctk": "f16", "threads": "32", "flash-attn": "true", "no-mmap": "false", "alias": "x",
           "t": "8", "port": "9000"}
    out = at.section_args(sec, {"cache-type-k": "q8_0", "threads": "12"})
    assert "--ctx-size" not in out and "--hf-repo" not in out and "--port" not in out
    assert out.count("--cache-type-k") == 1 and "-ctk" not in out
    assert out[out.index("--cache-type-k") + 1] == "q8_0"
    assert out[out.index("--threads") + 1] == "12" and "-t" not in out
    assert "--flash-attn" in out and "--no-mmap" not in out
    assert "--alias" not in out


def test_section_args_short_keys_and_bool_overrides(at):
    out = at.section_args({"b": "2048"}, {"kv-unified": "true", "parallel": 4})
    assert out == ["-b", "2048", "--kv-unified", "--parallel", "4"]


def test_section_args_normalises_dash_prefixed_keys(at):
    out = at.section_args({"--hf-repo": "o/r", "--ctx-size": "4096", "--threads": "8"}, {})
    assert "--hf-repo" not in out and "----hf-repo" not in out
    assert "--ctx-size" not in out and "----ctx-size" not in out
    assert out == ["--threads", "8"]


def test_section_args_override_alias_drops_the_canonical_key(at):
    out = at.section_args({"cache-type-k": "f16", "ctv": "f16"}, {"ctk": "q8_0", "--cache-type-v": "q8_0"})
    assert "--cache-type-k" not in out                      # canonical key dropped by the ctk override
    assert out.count("--ctk") == 1 and out[out.index("--ctk") + 1] == "q8_0"
    assert "--ctv" not in out                               # alias dropped by the canonical override
    assert out.count("--cache-type-v") == 1 and out[out.index("--cache-type-v") + 1] == "q8_0"


def test_custom_args_are_capped_and_screened(at):
    def body(args):
        return {"model_ids": ["m"], "objective": "fit", "dims": {"context": {"custom_args": args}}}
    ok = at.validate_request(body(["-ub", "512"]))
    assert ok["dims"]["context"]["custom_args"] == ["-ub", "512"]
    with pytest.raises(ValueError):
        at.validate_request(body(["-x"] * 33))
    with pytest.raises(ValueError):
        at.validate_request(body(["x" * 257]))
    with pytest.raises(ValueError):
        at.validate_request(body(["-md", "/tmp/d.gguf"]))
    with pytest.raises(ValueError):
        at.validate_request(body(["--hf-repo", "o/r"]))
    assert at.validate_request(body(["--models"]))["dims"]["context"]["custom_args"] == ["--models"]



# ── --help option shapes ──────────────────────────────────

HELP = """usage: llama-server [options]

----- common params -----

  -h,    --help                            print usage and exit
  -c,    --ctx-size N                      size of the prompt context
  -ngl,  --gpu-layers, --n-gpu-layers N    number of layers to store in VRAM
  -fa,   --flash-attn [on|off|auto]        set Flash Attention use
         --reasoning, --think on|off       enable reasoning
         --reasoning-preserve on|off       preserve reasoning content
         --reasoning-budget-message STRING
                                           message appended when the budget runs out
         --check-tensors                   check model tensor data for invalid values
         --kv-unified                      use single unified KV buffer
         --log-disable                     Log disable
         --no-webui                        Disable the Web UI
"""


def test_parse_help_valued_splits_flags_from_valued_options(at):
    v = at.parse_help_valued(HELP)
    assert v == {"ctx-size", "gpu-layers", "n-gpu-layers", "flash-attn", "reasoning", "think",
                 "reasoning-preserve", "reasoning-budget-message"}


def test_parse_help_valued_ignores_prose_and_empty_text(at):
    assert at.parse_help_valued("") == set()
    assert at.parse_help_valued("  (env: LLAMA_ARG_CTX_SIZE)\n  default is 4096\n") == set()


# ── on/off values against the installed help ─────────────────────

REAL_SECTION = {
    "batch-size": "2048", "cache-ram": "0", "cache-type-k": "q8_0", "cache-type-v": "q8_0",
    "check-tensors": "off", "ctx-size": "124160", "dynatemp-exp": "1", "dynatemp-range": "0.0",
    "fit": "on", "fit-ctx": "32768", "flash-attn": "on", "load-mode": "none", "min-p": "0.00",
    "n-gpu-layers": "99", "parallel": "1", "predict": "-1", "presence-penalty": "0",
    "reasoning": "on", "reasoning-budget": "-1",
    "reasoning-budget-message": '"Reasoning budget exhausted — answering now."',
    "reasoning-preserve": "on", "repeat-penalty": "1.0", "spec-draft-n-max": "3",
    "spec-type": "draft-mtp", "swa-checkpoints": "32", "temperature": "0.5", "top-k": "20",
    "top-p": "0.95", "ubatch-size": "1024",
}
REAL_VALUED = {"flash-attn", "fit", "reasoning", "reasoning-preserve", "ctx-size", "batch-size",
               "cache-type-k", "cache-type-v", "spec-type", "n-gpu-layers",
               "reasoning-budget-message", "reasoning-budget", "parallel"}


def test_section_args_valued_bools_keep_their_value(at):
    out = at.section_args(REAL_SECTION, {}, valued=REAL_VALUED)
    assert out[out.index("--flash-attn") + 1] == "on"
    assert out[out.index("--reasoning") + 1] == "on"
    assert out[out.index("--reasoning-preserve") + 1] == "on"
    assert "--check-tensors" not in out                     # off + flag-only -> omitted
    assert "--fit" not in out and "--fit-ctx" not in out and "--ctx-size" not in out
    msg = out[out.index("--reasoning-budget-message") + 1]
    assert msg == "Reasoning budget exhausted — answering now." and '"' not in msg
    assert out[out.index("--spec-type") + 1] == "draft-mtp"
    assert "off" not in out and "on" == out[out.index("--flash-attn") + 1]


def test_section_args_without_help_falls_back_to_bare_flags(at):
    out = at.section_args(REAL_SECTION, {}, valued=None)
    assert "--flash-attn" in out and out[out.index("--flash-attn") + 1].startswith("--")
    assert "--reasoning" in out and out[out.index("--reasoning") + 1].startswith("--")
    assert "--check-tensors" not in out and "on" not in out and "off" not in out


def test_section_args_bare_flag_keys_stay_bare(at):
    assert at.section_args({"check-tensors": "on"}, {}, valued=None) == ["--check-tensors"]
    assert at.section_args({"kv-unified": "on"}, {}, valued=REAL_VALUED) == ["--kv-unified"]
    assert at.section_args({"check-tensors": "off"}, {}, valued=None) == []


def test_section_args_unquotes_before_deciding_bool_shape(at):
    assert at.section_args({"check-tensors": '"off"'}, {}, valued=None) == []
    assert at.section_args({"check-tensors": '"on"'}, {}, valued=None) == ["--check-tensors"]
    assert at.section_args({"flash-attn": '"on"'}, {}, valued={"flash-attn"}) == ["--flash-attn", "on"]
    assert at.section_args({"alias-x": '""'}, {}, valued=None) == []


# ── facts ──────────────────────────────────────────────────────────

FACTS = """
print_info: arch             = qwen3moe
print_info: n_layer          = 48
print_info: n_expert         = 128
print_info: n_expert_used    = 8
print_info: n_ctx_train      = 262144
print_info: model size       = 17.28 GiB (4.86 BPW)
print_info: model params     = 30.53 B
print_info: general.name     = Qwen3 30B A3B
print_info: n_nextn_predict_layers = 1
load_tensors: offloaded 49/49 layers to GPU
"""


def test_parse_facts(at):
    f = at.parse_facts(FACTS.splitlines())
    assert f["arch"] == "qwen3moe" and f["name"] == "Qwen3 30B A3B"
    assert f["n_layer"] == 48 and f["n_expert"] == 128 and f["n_expert_used"] == 8
    assert f["n_ctx_train"] == 262144 and f["model_size_gib"] == 17.28 and f["model_params_b"] == 30.53
    assert f["mtp_layers"] == 1


def test_parse_facts_dense_model_has_no_mtp(at):
    f = at.parse_facts(["print_info: n_expert = 0", "print_info: n_layer = 32"])
    assert f["n_expert"] == 0 and f["mtp_layers"] == 0 and f["arch"] is None


PREFIXED_FACTS = """0.00.571.994 I llama_model_loader: - kv  28:                qwen35.nextn_predict_layers u32              = 1
0.00.598.117 I print_info: file size   = 16.34 GiB (5.14 BPW)
0.00.720.635 I print_info: arch                  = qwen35
0.00.720.638 I print_info: n_layer               = 64
0.00.720.641 I print_info: n_expert              = 0
0.00.720.650 I print_info: n_ctx_train           = 262144
0.00.720.665 I print_info: model params          = 27.32 B
0.01.100.000 I spec common_specu: adding speculative implementation 'draft-mtp'
"""


def test_parse_facts_reads_timestamped_lines(at):
    f = at.parse_facts(PREFIXED_FACTS.splitlines())
    assert f["arch"] == "qwen35" and f["n_layer"] == 64 and f["n_expert"] == 0
    assert f["n_ctx_train"] == 262144 and f["model_params_b"] == 27.32
    assert f["model_size_gib"] == 16.34                    # this build prints "file size"
    assert f["mtp_layers"] == 1                            # from the loader kv line


def test_parse_facts_prefixed_dense_model_has_no_mtp(at):
    dense = [ln for ln in PREFIXED_FACTS.splitlines() if "nextn" not in ln]
    assert at.parse_facts(dense)["mtp_layers"] == 0


def test_parse_kl(at):
    assert at.parse_kl("===== KL divergence statistics =====\nMean    KLD:   0.006123 ±   0.000045\n") == 0.006123
    assert at.parse_kl("nothing here") is None


def test_physical_cores(at):
    cpuinfo = "\n".join(["processor : 0", "physical id : 0", "core id : 0",
                         "processor : 1", "physical id : 0", "core id : 0",
                         "processor : 2", "physical id : 0", "core id : 1",
                         "processor : 3", "physical id : 0", "core id : 1"])
    assert at.physical_cores(cpuinfo, 4) == {"physical": 2, "logical": 4}
    assert at.physical_cores("", 16) == {"physical": 8, "logical": 16}


# ── draft discovery ────────────────────────────────────────────────

DRAFTS = [
    {"repo": "unsloth/Qwen3-0.6B-GGUF", "file": "Qwen3-0.6B-Q8_0.gguf", "path": "/h/a.gguf", "size": 700_000_000},
    {"repo": "unsloth/Qwen3-4B-GGUF", "file": "Qwen3-4B-Q4_K_M.gguf", "path": "/h/b.gguf", "size": 2_600_000_000},
    {"repo": "z/Qwen3-30B-A3B-dflash-GGUF", "file": "Qwen3-30B-A3B-DFlash-Q8_0.gguf", "path": "/h/c.gguf", "size": 900_000_000},
    {"repo": "google/gemma-3-1b-it-GGUF", "file": "gemma-3-1b-it-Q8_0.gguf", "path": "/h/d.gguf", "size": 1_000_000_000},
]


def test_family_prefix(at):
    assert at.family_prefix("Qwen3-30B-A3B-Instruct-GGUF") == "qwen3"
    assert at.family_prefix("gemma-3-27b-it") == "gemma-3"
    assert at.family_prefix("Llama-3.3-70B-Instruct") == "llama-3.3"
    assert at.family_prefix("no-size-token") == "no-size-token"


def test_find_draft_picks_smallest_same_family_within_eighth(at):
    d = at.find_draft(DRAFTS, "unsloth/Qwen3-30B-A3B-Instruct-GGUF", 18_000_000_000)
    assert d["path"] == "/h/a.gguf"
    assert at.find_draft(DRAFTS, "unsloth/Qwen3-30B-A3B-Instruct-GGUF", 4_000_000_000) is None
    assert at.find_draft(DRAFTS, "org/Mistral-7B", 8_000_000_000) is None


def test_find_dflash_matches_family_and_name(at):
    assert at.find_dflash(DRAFTS, "unsloth/Qwen3-30B-A3B-Instruct-GGUF")["path"] == "/h/c.gguf"
    assert at.find_dflash(DRAFTS, "google/gemma-3-27b-it-GGUF") is None


def test_expand_spec_types_auto(at):
    rows = at.expand_spec_types(["auto"], {"mtp_layers": 1}, DRAFTS,
                                "unsloth/Qwen3-30B-A3B-Instruct-GGUF", 18_000_000_000, "auto")
    assert [r["type"] for r in rows] == ["draft-mtp", "draft-dflash", "draft-simple", "ngram-simple"]
    assert rows[2]["draft"] == "/h/a.gguf" and rows[1]["draft"] == "/h/c.gguf" and rows[0]["draft"] is None
    rows = at.expand_spec_types(["auto"], {"mtp_layers": 0}, [], "org/m", 1, "none")
    assert [r["type"] for r in rows] == ["ngram-simple"]


def test_expand_spec_types_keeps_the_models_current_type(at):
    """A configured spec-type runs even when detection misses its heads."""
    rows = at.expand_spec_types(["auto"], {"mtp_layers": 0}, [], "org/m", 1, "none",
                                current_type="draft-mtp")
    assert [r["type"] for r in rows] == ["draft-mtp", "ngram-simple"] and rows[0]["draft"] is None
    for cur in ("none", "", None, "bogus"):
        rows = at.expand_spec_types(["auto"], {"mtp_layers": 0}, [], "org/m", 1, "none", current_type=cur)
        assert [r["type"] for r in rows] == ["ngram-simple"]
    rows = at.expand_spec_types(["auto"], {"mtp_layers": 0}, [], "org/m", 1, "none",
                                current_type="draft-simple", current_draft="/h/d.gguf")
    assert rows[0] == {"type": "draft-simple", "draft": "/h/d.gguf"}
    rows = at.expand_spec_types(["auto"], {"mtp_layers": 0}, [], "org/m", 1, "none",
                                current_type="draft-simple")
    assert [r["type"] for r in rows] == ["ngram-simple"]    # needs a draft file, has none


def test_expand_spec_types_explicit_keeps_order_and_needs_a_draft(at):
    rows = at.expand_spec_types(["draft-simple", "draft-mtp"], {"mtp_layers": 0}, DRAFTS,
                                "unsloth/Qwen3-30B-A3B-Instruct-GGUF", 18_000_000_000, "/h/b.gguf")
    assert rows == [{"type": "draft-simple", "draft": "/h/b.gguf"}]


# ── choice functions ───────────────────────────────────────────────

KV = [
    {"value": "f16", "ok": True, "ctx": 32768, "kl": None, "guard_pass": None, "lossy": False},
    {"value": "q8_0", "ok": True, "ctx": 65536, "kl": 0.006, "guard_pass": True, "lossy": False},
    {"value": "q4_0", "ok": True, "ctx": 98304, "kl": 0.031, "guard_pass": False, "lossy": True},
]


def test_choose_kv_per_objective(at):
    assert at.choose_kv("fit", KV, 32768)[0] == "q8_0"
    assert at.choose_kv("speed", KV, 32768)[0] == "f16"
    assert at.choose_kv("speed", KV, 65536)[0] == "q8_0"
    assert at.choose_kv("balanced", KV, 32768)[0] == "q8_0"
    small = [dict(KV[0]), dict(KV[1], ctx=34000)]
    assert at.choose_kv("balanced", small, 32768)[0] == "f16"
    assert at.choose_kv("serve", KV, 32768)[0] == "q8_0"


def test_choose_kv_fit_takes_smallest_passing_type(at):
    rows = [dict(KV[0]), dict(KV[1]), dict(KV[2], kl=0.01, guard_pass=True)]
    assert at.choose_kv("fit", rows, 32768)[0] == "q4_0"
    assert at.choose_kv("fit", [dict(KV[0]), dict(KV[2])], 32768)[0] == "f16"
    assert at.choose_kv("fit", [dict(KV[2])], 32768) == (None, "no candidate passed")


def test_choose_threads_ties_go_to_fewer(at):
    rows = [{"value": 8, "ok": True, "decode_tps": 100.0}, {"value": 12, "ok": True, "decode_tps": 102.5},
            {"value": 16, "ok": True, "decode_tps": 101.0}, {"value": 32, "ok": False, "decode_tps": None}]
    assert at.choose_threads(rows)[0] == 8
    rows[1]["decode_tps"] = 110.0
    assert at.choose_threads(rows)[0] == 12
    assert at.choose_threads([{"value": 8, "ok": False, "decode_tps": None}]) == (None, "no candidate loaded")


def test_choose_spec_needs_five_percent(at):
    rows = [{"value": "none", "ok": True, "decode_tps": 100.0, "accept": None},
            {"value": "draft-mtp", "ok": True, "decode_tps": 138.0, "accept": 0.74},
            {"value": "ngram-simple", "ok": True, "decode_tps": 104.0, "accept": 0.2}]
    assert at.choose_spec(rows)[0] == "draft-mtp"
    rows[1]["decode_tps"] = 104.9
    assert at.choose_spec(rows) == ("none", "no type gained 5 % over none")


def test_window_plan(at):
    assert at.window_plan(0, 16) == [(0, 4), (0, 8), (0, 12), (0, 16), (2, None)]
    assert at.window_plan(2, 8) == [(2, 4), (2, 8), (0, None)]
    assert at.window_plan(4, 4) == [(0, None), (2, None)]


SLOTS = [{"value": 1, "ok": True, "decode_tps": 100.0, "agg_tps": 100.0},
         {"value": 2, "ok": True, "decode_tps": 90.0, "agg_tps": 180.0},
         {"value": 4, "ok": True, "decode_tps": 72.0, "agg_tps": 288.0},
         {"value": 8, "ok": True, "decode_tps": 50.0, "agg_tps": 400.0}]


def test_choose_slots_per_objective(at):
    assert at.choose_slots("fit", SLOTS)[0] == 1
    assert at.choose_slots("speed", SLOTS)[0] == 1
    assert at.choose_slots("balanced", SLOTS)[0] == 4
    assert at.choose_slots("serve", SLOTS)[0] == 8
    assert at.choose_slots("serve", [dict(SLOTS[0], ok=False)]) == (None, "no candidate loaded")


def test_bisect_smallest(at):
    calls = []
    def fits(n):
        calls.append(n)
        return n >= 6
    assert at.bisect_smallest(0, 16, fits) == (6, [(8, True), (3, False), (5, False), (6, True)])
    assert at.bisect_smallest(0, 4, lambda n: False)[0] is None
    assert at.bisect_smallest(3, 3, lambda n: True) == (3, [(3, True)])


def test_budget_and_estimates(at):
    assert at.budget_ok(100, 50, 200) and not at.budget_ok(160, 50, 200)
    assert at.estimate("kv", 3, 40.0, 20.0) == 3 * (4 * 40 + 20 + 90)
    assert at.estimate("threads", 3, 40.0, 20.0) == 3 * 60
    assert at.estimate("verify", 1, 40.0, 20.0) == 40 + 60
    assert at.gain_pct(142.6, 101) == pytest.approx(41.2, abs=0.1)
    assert at.gain_pct(1, 0) is None and at.gain_pct(None, 1) is None


def test_build_changes_selection_rule(at):
    cur = {"ctx-size": "32768", "threads": "32", "cache-type-k": "f16"}
    rec = {"ctx-size": "65536", "threads": "12", "cache-type-k": "q8_0", "parallel": "4"}
    ev = {"ctx-size": {"source": "measured", "text": "free 1012 MB", "gain_pct": None},
          "threads": {"source": "measured", "text": "12 = 16 within noise", "gain_pct": 1.5},
          "cache-type-k": {"source": "measured", "text": "KL 0.006", "gain_pct": None},
          "parallel": {"source": "measured", "text": "aggregate 3.1×", "gain_pct": 210.0}}
    rows = at.build_changes(cur, rec, ev)
    assert [r["key"] for r in rows] == ["ctx-size", "cache-type-k", "threads", "parallel"]
    assert rows[0] == {"key": "ctx-size", "current": "32768", "recommended": "65536", "source": "measured",
                       "evidence": "free 1012 MB", "selected": True}
    assert rows[2]["selected"] is False and rows[3]["current"] == "" and rows[3]["selected"] is True


def test_ledger_summary(at):
    done = {"objective": "balanced", "after": {"ctx": 65536, "free_mb": 1012, "decode_tps": 142.6, "wh_per_ktok": 0.24},
            "before": {"decode_tps": 101}, "stages": [{"stage": "context", "status": "done"}, {"stage": "kv", "status": "skipped"}],
            "verify": {"ok": True}, "facts": {"n_expert": 128}}
    s = at.ledger_summary(done)
    assert s == {"objective": "balanced", "mode": "tune", "llama_build": None, "ctx_size": 65536, "free_mb": 1012,
                 "decode_tps": 142.6, "gain_pct": pytest.approx(41.2, abs=0.1), "stages_done": 1, "verify_ok": True,
                 "wh_per_ktok": 0.24, "n_expert": 128, "kl": None, "kl_pass": None, "regressed": None}


def test_kl_args_keeps_only_perplexity_safe_flags(at):
    args = ["--jinja", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--threads", "12", "--spec-type", "draft-mtp",
            "--n-gpu-layers", "99", "--n-cpu-moe", "6", "--flash-attn", "--parallel", "4", "--no-mmap", "-ngl", "40"]
    assert at.kl_args(args) == ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--threads", "12",
                                "--n-gpu-layers", "99", "--n-cpu-moe", "6", "--flash-attn", "--no-mmap", "-ngl", "40"]


def test_kl_args_keeps_safe_flag_value_pair(at):
    args = ["--flash-attn", "on", "--cache-type-k", "f16"]
    assert at.kl_args(args) == ["--flash-attn", "on", "--cache-type-k", "f16"]


def test_kl_args_bare_safe_flag_before_another_flag(at):
    args = ["--flash-attn", "--cache-type-k", "f16"]
    assert at.kl_args(args) == ["--flash-attn", "--cache-type-k", "f16"]


def test_kl_args_bare_safe_flag_at_end_of_argv(at):
    assert at.kl_args(["--threads", "8", "--mlock"]) == ["--threads", "8", "--mlock"]


def test_kl_args_no_mmap_and_mlock_value_and_bare_forms(at):
    assert at.kl_args(["--no-mmap", "on", "--mlock"]) == ["--no-mmap", "on", "--mlock"]
    assert at.kl_args(["--no-mmap", "--parallel", "4", "--mlock"]) == ["--no-mmap", "--mlock"]


def test_kl_args_keeps_load_mode_value(at):
    args = ["--load-mode", "mmap+mlock", "--parallel", "4"]
    assert at.kl_args(args) == ["--load-mode", "mmap+mlock"]


def test_spawn_cmd_fit_vs_explicit_ctx(at):
    fit = at.spawn_cmd("/o/llama-server", "o/r:Q4", 8080, 1024, None, ["--threads", "8"])
    assert fit == ["/o/llama-server", "--models-max", "1", "-lv", "4", "--host", "127.0.0.1", "--port", "8080",
                   "-fitt", "1024", "--threads", "8", "-hf", "o/r:Q4"]
    ex = at.spawn_cmd("/o/llama-server", "o/r:Q4", 8080, None, 65536, [])
    assert ex[9:13] == ["--fit", "off", "-c", "65536"] and ex[-2:] == ["-hf", "o/r:Q4"]
    # Neither flag: baseline load of the section as configured.
    plain = at.spawn_cmd("/o/llama-server", "o/r:Q4", 8080, None, None, ["--threads", "8"])
    assert plain == ["/o/llama-server", "--models-max", "1", "-lv", "4", "--host", "127.0.0.1", "--port", "8080",
                     "--threads", "8", "-hf", "o/r:Q4"]
    assert at.spawn_cmd("/o/llama-server", "o/r:Q4", 8080, 0, None, [])[9:11] == ["-fitt", "0"]


def test_mode_defaults_to_tune_and_rejects_unknown(at):
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit"})
    assert req["mode"] == "tune"
    with pytest.raises(ValueError):
        at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "poke"})


def test_verify_mode_turns_every_dimension_off_and_keeps_baseline(at):
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "balanced", "mode": "verify",
                               "baseline_tps": 41.5, "dims": {"kv": {"on": True}}})
    assert req["mode"] == "verify"
    assert all(not d["on"] for name, d in req["dims"].items() if name != "context")
    assert req["baseline_tps"] == 41.5


def test_verify_mode_drops_operator_custom_args(at):
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "balanced", "mode": "verify",
                               "dims": {"context": {"custom_args": ["-ub", "512"]}}})
    assert req["dims"]["context"]["custom_args"] == []
    tune = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "balanced",
                                "dims": {"context": {"custom_args": ["-ub", "512"]}}})
    assert tune["dims"]["context"]["custom_args"] == ["-ub", "512"]


def test_quality_overrides_canonicalise_short_aliases(at):
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                               "overrides": {"ctv": "q4_0", "-ctk": "q8_0", "t": "8", "ncmoe": None}})
    assert req["overrides"] == {"cache-type-v": "q4_0", "cache-type-k": "q8_0",
                                "threads": "8", "n-cpu-moe": None}


def test_quality_mode_validates_overrides(at):
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                               "overrides": {"cache-type-k": "q4_0", "--ctv": "q4_0", "threads": None},
                               "kl_max": 0.05})
    assert req["overrides"] == {"cache-type-k": "q4_0", "cache-type-v": "q4_0", "threads": None}
    assert req["kl_max"] == 0.05
    with pytest.raises(ValueError):
        at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                             "overrides": {"model": "/etc/passwd"}})
    with pytest.raises(ValueError):
        at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                             "overrides": {"threads": "8; rm -rf"}})
    with pytest.raises(ValueError):
        at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality", "overrides": {}})


def test_quality_overrides_accept_load_mode_and_reject_deprecated_keys(at):
    req = at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                               "overrides": {"lm": "mmap+mlock"}})
    assert req["overrides"] == {"load-mode": "mmap+mlock"}
    with pytest.raises(ValueError):
        at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                             "overrides": {"no-mmap": "true"}})
    with pytest.raises(ValueError):
        at.validate_request({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                             "overrides": {"mlock": "true"}})


def test_ledger_summary_carries_mode_build_and_kl(at):
    done = {"objective": "fit", "mode": "quality", "llama_build": "b10809-5266f24",
            "guard": {"kl": 0.011, "pass": True}, "after": {}, "before": {}, "stages": []}
    s = at.ledger_summary(done)
    assert s["mode"] == "quality" and s["llama_build"] == "b10809-5266f24"
    assert s["kl"] == 0.011 and s["kl_pass"] is True
