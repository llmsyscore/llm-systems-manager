# agent/tests/test_vllm_svcconfig.py
"""#125: vLLM svcconfig — ExecStart parse keeps positionals in the command
head; POST token stream + baked-unit mismatch refusal; LoRA opt-in guard."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests._vllm_load import load_vllm

vllm = load_vllm()

UNIT = """[Service]
ExecStart="/opt/vllm/bin/vllm" serve meta-llama/Llama-3-8B --host 127.0.0.1 --port 8000 --gpu-memory-utilization 0.9 --enable-lora
"""


def test_parse_execstart_head_and_flags():
    head, args = vllm._parse_vllm_execstart(UNIT)
    assert head == "/opt/vllm/bin/vllm serve meta-llama/Llama-3-8B"
    assert {"flag": "--host", "value": "127.0.0.1", "bool": False} in args
    assert {"flag": "--gpu-memory-utilization", "value": "0.9", "bool": False} in args
    assert {"flag": "--enable-lora", "value": None, "bool": True} in args


def test_parse_execstart_missing_returns_none():
    head, args = vllm._parse_vllm_execstart("[Service]\nUser=x\n")
    assert head is None and args == []


def test_tokens_roundtrip():
    head, args = vllm._parse_vllm_execstart(UNIT)
    toks = vllm._svcconfig_tokens(head, args)
    assert toks[:3] == ["/opt/vllm/bin/vllm", "serve", "meta-llama/Llama-3-8B"]
    assert "--enable-lora" in toks and "0.9" in toks


def test_tokens_reject_newline():
    assert vllm._svcconfig_tokens(
        "vllm serve m", [{"flag": "--x\n", "value": None, "bool": True}]) is None


@pytest.fixture
def ctx():
    cfg = SimpleNamespace(
        VLLM_ENABLED=True,
        VLLM_LORA_ENABLED=False,
        VLLM_SYSTEMD_UNIT="vllm.service",
        VLLM_API_URL="http://localhost:8000",
    )
    context = SimpleNamespace(config=cfg, check_bearer=lambda *_: None)
    vllm.set_context(context)
    return context


def _wrapper_file(tmp_path, unit_path):
    wf = tmp_path / "llm-vllm-svcconfig-apply"
    wf.write_text(f"#!/usr/bin/env bash\nUNIT_PATH='{unit_path}'\n")
    return wf


BODY = {"binary": "/opt/vllm/bin/vllm serve m1",
        "args": [{"flag": "--port", "value": "8000", "bool": False}]}


def test_post_refuses_on_baked_unit_mismatch(ctx, tmp_path, monkeypatch):
    wf = _wrapper_file(tmp_path, "/etc/systemd/system/other.service")
    monkeypatch.setattr(vllm, "_VLLM_SVCCONFIG_WRAPPER", str(wf))
    monkeypatch.setattr(vllm.subprocess, "run",
                        lambda *a, **k: pytest.fail("wrapper must not run on mismatch"))
    r = vllm.vllm_svcconfig_post(dict(BODY))
    assert r["ok"] is False
    assert "baked for" in r["error"] and "vllm.service" in r["error"]


def test_post_proceeds_when_baked_unit_matches(ctx, tmp_path, monkeypatch):
    wf = _wrapper_file(tmp_path, "/etc/systemd/system/vllm.service")
    monkeypatch.setattr(vllm, "_VLLM_SVCCONFIG_WRAPPER", str(wf))
    calls = []
    monkeypatch.setattr(
        vllm.subprocess, "run",
        lambda *a, **k: calls.append((a, k)) or SimpleNamespace(returncode=0, stderr=b""))
    r = vllm.vllm_svcconfig_post(dict(BODY))
    assert r == {"ok": True}
    assert len(calls) == 1
    payload = calls[0][1]["input"].decode()
    assert payload.splitlines()[:3] == ["/opt/vllm/bin/vllm", "serve", "m1"]


def test_lora_guard_503_when_disabled(ctx):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        vllm.vllm_lora_load({"lora_name": "a", "lora_path": "/x"})
    assert ei.value.status_code == 503


def test_openai_routes_registered():
    paths = {(m, p) for m, p, _ in vllm._ROUTES}
    assert ("POST", "/vllm/openai/chat/completions") in paths
    assert ("POST", "/vllm/openai/completions") in paths
    assert ("GET", "/vllm/server/svcconfig") in paths
    assert ("POST", "/vllm/server/svcconfig") in paths
    assert ("POST", "/vllm/lora/load") in paths
    assert ("POST", "/vllm/lora/unload") in paths
