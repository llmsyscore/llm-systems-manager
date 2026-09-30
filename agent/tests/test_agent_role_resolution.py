"""AGENT_ROLE auto-detection names every provider, vLLM included (#1151)."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"


def _role_resolver():
    src = SRC.read_text()
    m = re.search(r"\n(    providers = \[p for p in .*?cfg\.AGENT_ROLE = \"system_only\"\n)", src, re.S)
    assert m, "role resolution block not found"
    body = "\n".join(line[4:] for line in m.group(1).splitlines())

    def resolve(found):
        class Cfg:
            AGENT_ROLE = "auto"
        cfg = Cfg()
        exec(body, {"found": found, "cfg": cfg})
        return cfg.AGENT_ROLE
    return resolve


def test_single_provider_roles():
    resolve = _role_resolver()
    assert resolve({"llama": True}) == "llama_host"
    assert resolve({"lms": True}) == "lms_host"
    assert resolve({"vllm": True}) == "vllm_host"


def test_two_providers_is_mixed_and_none_is_system_only():
    resolve = _role_resolver()
    assert resolve({"llama": True, "vllm": True}) == "mixed"
    assert resolve({"lms": True, "vllm": True}) == "mixed"
    assert resolve({}) == "system_only"
    assert resolve({"openclaw": True}) == "system_only"
