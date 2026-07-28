"""#468: preset pinning — exact revisions, provider coverage, corpus size."""
from __future__ import annotations

import re

import report_card as rc


def test_preset_has_small_and_mid():
    assert [m["key"] for m in rc.REFERENCE_MODELS] == ["small", "mid"]


def test_every_model_pins_all_three_providers():
    for m in rc.REFERENCE_MODELS:
        for p in ("llama", "lms", "vllm"):
            src = rc.preset_source(m["key"], p)
            assert src and src["repo"] and src["revision"], (m["key"], p)


def test_revisions_are_real_commit_hashes():
    for m in rc.REFERENCE_MODELS:
        for p in ("llama", "lms", "vllm"):
            rev = rc.preset_source(m["key"], p)["revision"]
            assert re.fullmatch(r"[0-9a-f]{40}", rev), (m["key"], p, rev)


def test_gguf_for_llama_and_lms_awq_for_vllm():
    for m in rc.REFERENCE_MODELS:
        assert rc.preset_source(m["key"], "llama")["file"].endswith(".gguf")
        assert rc.preset_source(m["key"], "lms")["file"].endswith(".gguf")
        assert "AWQ" in rc.preset_source(m["key"], "vllm")["repo"]


def test_preset_source_unknown_returns_none():
    assert rc.preset_source("huge", "llama") is None
    assert rc.preset_source("small", "nope") is None


def test_corpus_is_roughly_512_tokens():
    # ~4 chars/token heuristic; corpus must be stable, not "roughly" per run.
    assert 1600 <= len(rc.PROMPT_CORPUS) <= 2600
    assert rc.PRESET_VERSION == "preset_v1"
    assert rc.GEN_TOKENS == 128 and rc.REPS == 3


def test_gguf_providers_pin_a_repo_colon_quant_model_id():
    # This is the id llama.cpp/LM Studio actually register.
    for m in rc.REFERENCE_MODELS:
        for p in ("llama", "lms"):
            src = rc.preset_source(m["key"], p)
            assert src["model_id"] == f"{src['repo']}:{src['quant']}"
        v = rc.preset_source(m["key"], "vllm")
        assert v["model_id"] == v["repo"]


def test_every_model_advertises_a_download_size():
    for m in rc.REFERENCE_MODELS:
        assert isinstance(m["approx_gb"], (int, float)) and m["approx_gb"] > 0


def test_gguf_sources_have_download_patterns_matching_their_file():
    import fnmatch
    for m in rc.REFERENCE_MODELS:
        for prov in ("llama", "lms"):
            src = rc.preset_source(m["key"], prov)
            assert src["patterns"], (m["key"], prov)
            assert any(fnmatch.fnmatch(src["file"], pat)
                       for pat in src["patterns"]), (m["key"], prov)
            # Lowercase-only globs; uppercase quant tags match nothing.
            assert all(pat == pat.lower() for pat in src["patterns"])
