"""#468: readiness — load-if-present, download offer, vLLM confirm gate."""
from __future__ import annotations

import report_card as rc


def _deps(loaded=None, vllm_current="other-model", load_ok=True):
    return {"loaded_models": lambda p, a: loaded or [],
            "load": lambda p, a, src: load_ok,
            "vllm_current": lambda a: vllm_current}


def test_ready_when_reference_is_registered():
    src = rc.preset_source("small", "llama")
    out = rc.ensure_ready("llama", "a" * 32, "small",
                          _deps(loaded=[src["model_id"]]))
    assert out["status"] == "ready"
    assert out["is_reference"] is True


def test_matches_the_repo_colon_quant_id_llama_server_registers():
    # Regression: llama.cpp registers GGUFs as "<repo>:<QUANT>", never as the
    # .gguf filename. Matching on the filename made every standard run fail
    # with "model not present" even after the model was installed.
    out = rc.ensure_ready("llama", "a" * 32, "small",
                          _deps(loaded=["Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M"]))
    assert out["status"] == "ready"
    assert out["model"] == "Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M"


def test_repo_quant_match_is_case_insensitive():
    out = rc.ensure_ready("llama", "a" * 32, "small",
                          _deps(loaded=["qwen/qwen2.5-1.5b-instruct-gguf:q4_k_m"]))
    assert out["status"] == "ready"


def test_wrong_quant_of_the_right_repo_is_not_the_reference():
    out = rc.ensure_ready("llama", "a" * 32, "small",
                          _deps(loaded=["Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q8_0"]))
    assert out["status"] == "needs_download"


def test_unlabelled_repo_is_not_accepted_as_the_pinned_quant():
    # A bare repo id states no quant. Accepting it would mark an arbitrary
    # quantization as the Q4_K_M reference and ship it to the leaderboard.
    src = rc.preset_source("small", "llama")
    assert rc._model_matches("Qwen/Qwen2.5-1.5B-Instruct-GGUF", src) is False
    out = rc.ensure_ready("llama", "a" * 32, "small",
                          _deps(loaded=["Qwen/Qwen2.5-1.5B-Instruct-GGUF"]))
    assert out["status"] == "needs_download"


def test_vllm_pins_no_quant_so_a_bare_repo_still_matches():
    src = rc.preset_source("small", "vllm")
    assert rc._model_matches(src["repo"], src) is True


def test_unregistered_model_needs_download():
    # Downloaded-but-unregistered models are absent from the provider list.
    out = rc.ensure_ready("llama", "a" * 32, "small", _deps(loaded=[]))
    assert out["status"] == "needs_download"
    assert out["source"]["repo"].endswith("GGUF")
    assert out["source"]["quant"] == "Q4_K_M"


def test_registered_but_unloadable_reports_load_failure():
    out = rc.ensure_ready("llama", "a" * 32, "small",
                          _deps(loaded=["Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M"],
                                load_ok=False))
    assert out["status"] == "load_failed"


def test_lms_uses_the_same_matching():
    src = rc.preset_source("mid", "lms")
    out = rc.ensure_ready("lms", "a" * 32, "mid", _deps(loaded=[src["model_id"]]))
    assert out["status"] == "ready"


def test_vllm_requires_confirmation_and_reports_served_model():
    out = rc.ensure_ready("vllm", "a" * 32, "small",
                          _deps(vllm_current="Qwen/OtherModel"))
    assert out["status"] == "needs_confirm"
    assert out["model"] == "Qwen/OtherModel"


def test_vllm_confirm_benches_the_served_model_not_the_reference():
    # Manager never restarts vLLM; a confirmed run benches what is served.
    out = rc.ensure_ready("vllm", "a" * 32, "small",
                          _deps(vllm_current="Qwen/OtherModel"))
    assert out["model"] == "Qwen/OtherModel"
    assert out["reference"] == rc.preset_source("small", "vllm")["repo"]
    assert out["is_reference"] is False


def test_vllm_ready_when_already_on_reference():
    ref = rc.preset_source("small", "vllm")["repo"]
    out = rc.ensure_ready("vllm", "a" * 32, "small", _deps(vllm_current=ref))
    assert out["status"] == "ready"
    assert out["is_reference"] is True


def test_vllm_unavailable_when_nothing_is_served():
    out = rc.ensure_ready("vllm", "a" * 32, "small", _deps(vllm_current=None))
    assert out["status"] == "unavailable"


def test_unknown_model_key_is_unavailable():
    out = rc.ensure_ready("llama", "a" * 32, "huge", _deps())
    assert out["status"] == "unavailable"
