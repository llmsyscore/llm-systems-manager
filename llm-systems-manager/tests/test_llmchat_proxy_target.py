"""#466: llm_chat auto proxy target follows the default/primary llama agent,
not the first llama-pool member."""
from __future__ import annotations

from types import SimpleNamespace

import agent_registry
import proxies

A1 = {"agent_id": "a" * 32, "status": "approved",
      "registered_from": "10.0.0.1", "capabilities": {"llama": True}}
A2 = {"agent_id": "b" * 32, "status": "approved",
      "registered_from": "10.0.0.2", "capabilities": {"llama": True}}


def _patch(monkeypatch, glob, llm_chat="auto"):
    agents = {a["agent_id"]: a for a in (A1, A2)}
    monkeypatch.setattr(agent_registry, "load_agents",
                        lambda: {"agents": agents, "global": glob})
    monkeypatch.setattr(proxies, "settings", SimpleNamespace(
        manager=SimpleNamespace(proxies=SimpleNamespace(
            llm_chat=llm_chat, openclaw=False, image_gen=False))))


def test_llmchat_follows_default_over_pool_order(monkeypatch):
    # Pool lists A1 first, but the operator's default is A2.
    _patch(monkeypatch, {"llama_pool": [A1["agent_id"], A2["agent_id"]],
                         "default_llama_id": A2["agent_id"],
                         "primary_llama_id": A2["agent_id"]})
    assert proxies.resolve_proxy_target("llm_chat") == "http://10.0.0.2:8080"


def test_llmchat_no_default_falls_back_to_first_capable(monkeypatch):
    _patch(monkeypatch, {"llama_pool": [A1["agent_id"], A2["agent_id"]]})
    assert proxies.resolve_proxy_target("llm_chat") == "http://10.0.0.1:8080"


def test_llmchat_legacy_primary_only_still_honored(monkeypatch):
    # Manual TOML/JSON edits may set only the legacy key.
    _patch(monkeypatch, {"llama_pool": [A1["agent_id"], A2["agent_id"]],
                         "primary_llama_id": A2["agent_id"]})
    assert proxies.resolve_proxy_target("llm_chat") == "http://10.0.0.2:8080"


def test_llmchat_explicit_url_untouched(monkeypatch):
    _patch(monkeypatch, {"default_llama_id": A2["agent_id"]},
           llm_chat="http://192.0.2.9:9999/")
    assert proxies.resolve_proxy_target("llm_chat") == "http://192.0.2.9:9999"


def test_llmchat_disabled_returns_none(monkeypatch):
    _patch(monkeypatch, {"default_llama_id": A2["agent_id"]}, llm_chat=False)
    assert proxies.resolve_proxy_target("llm_chat") is None
