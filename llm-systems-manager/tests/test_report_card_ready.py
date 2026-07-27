"""#468: readiness — load-if-present, download offer, vLLM confirm gate."""
from __future__ import annotations

import report_card as rc


def _deps(loaded=None, vllm_current="other-model", load_ok=True):
    return {"loaded_models": lambda p, a: loaded or [],
            "load": lambda p, a, src: load_ok,
            "vllm_current": lambda a: vllm_current}


def test_ready_when_reference_already_loaded():
    src = rc.preset_source("small", "llama")
    out = rc.ensure_ready("llama", "a" * 32, "small", _deps(loaded=[src["file"]]))
    assert out["status"] == "ready"
    assert out["is_reference"] is True


def test_llama_loads_when_not_loaded():
    out = rc.ensure_ready("llama", "a" * 32, "small", _deps(loaded=[]))
    assert out["status"] == "ready"     # load callable succeeded


def test_llama_needs_download_when_load_fails():
    out = rc.ensure_ready("llama", "a" * 32, "small",
                          _deps(loaded=[], load_ok=False))
    assert out["status"] == "needs_download"
    assert out["source"]["repo"].endswith("GGUF")


def test_llama_matches_registered_id_containing_the_gguf_stem():
    # llama-server ids come from config.ini and need not equal the filename.
    out = rc.ensure_ready("llama", "a" * 32, "small",
                          _deps(loaded=["Qwen2.5-1.5B-Instruct-Q4_K_M"],
                                load_ok=False))
    assert out["status"] == "ready"


def test_lms_uses_the_same_load_path():
    out = rc.ensure_ready("lms", "a" * 32, "mid", _deps(loaded=[]))
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
