# agent/tests/test_vllm_provider.py
"""#125: vLLM provider — spec, route table, config defaults, wiring."""
from __future__ import annotations

import re
from pathlib import Path

from tests._vllm_load import load_vllm

_AGENT_ROOT = Path(__file__).resolve().parent.parent
vllm = load_vllm()


def test_provider_spec_shape():
    assert vllm.PROVIDER_SPEC == {
        "name": "vllm",
        "capability_key": "vllm",
        "push_endpoint": "/api/remote/provider-state",
    }


def test_vllm_in_modules_tuple():
    src = (_AGENT_ROOT / "providers" / "__init__.py").read_text()
    assert re.search(r"from \. import .*\bvllm\b", src)
    assert re.search(r"_MODULES = \(.*\bvllm\b.*\)", src)


def test_route_table_covers_mvp_surface():
    paths = {(m, p) for m, p, _ in vllm._ROUTES}
    for expected in [
        ("GET", "/vllm/server/status"),
        ("POST", "/vllm/server/start"),
        ("POST", "/vllm/server/stop"),
        ("POST", "/vllm/server/restart"),
        ("GET", "/vllm/models"),
    ]:
        assert expected in paths


def test_config_defaults_declared():
    src = (_AGENT_ROOT / "llm-systems-agent.py").read_text()
    assert re.search(r"^\s+VLLM_ENABLED: bool = False", src, re.M)
    assert re.search(r'^\s+VLLM_API_URL: str = "http://localhost:8000"', src, re.M)
    assert re.search(r'^\s+VLLM_SYSTEMD_UNIT: str = "vllm\.service"', src, re.M)
    assert re.search(r"^\s+VLLM_LORA_ENABLED: bool = False", src, re.M)
    assert '"vllm": CONFIG.VLLM_ENABLED' in src


def test_yaml_example_has_vllm_block():
    text = (_AGENT_ROOT / "agent_config.yaml.example").read_text()
    assert "VLLM_ENABLED: false" in text
    assert "VLLM_SYSTEMD_UNIT" in text
