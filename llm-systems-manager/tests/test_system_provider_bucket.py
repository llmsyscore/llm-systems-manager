"""#1041: a system-only agent's `system` bucket feeds the live views but not energy accounting."""
from __future__ import annotations

import pytest

import discord_bot as db
import energy
import provider_state

SYS = "e" * 32
SYS_SAMPLE = {"host": "sysbox", "cpu_total": 12.5, "ram": {"percent": 61.0}, "gpu": {}, "liquidctl": {}}


@pytest.fixture
def deps(monkeypatch):
    import agent_registry
    agents = {SYS: {"status": "approved", "hostname": "sysbox", "role": "system_only",
                    "capabilities": {"sysperf": True}, "token": "tok", "bind_url": "https://sysbox:8082"}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: {"agents": {k: dict(v) for k, v in agents.items()}})
    monkeypatch.setattr(agent_registry, "default_agent_id_for", lambda prov: None)
    provider_state.STORE.put("system", SYS, dict(SYS_SAMPLE))
    ctx = type("Ctx", (), {})()
    ctx.alarm_engine_url = lambda: "https://ae:8081"
    ctx.ae_session = None
    yield db.prod_deps(ctx)
    provider_state.STORE.evict(SYS)


def test_store_view_carries_system_buckets_only_for_live_readers(deps):
    live = energy.store_view_from_provider_state()
    assert "system" in live[SYS] and live[SYS]["system"][0]["cpu_total"] == 12.5
    assert SYS not in energy.store_view_from_provider_state(energy.PROVIDERS)


def test_host_and_fleet_read_cpu_and_ram_from_the_system_bucket(deps):
    row = deps["host"]("sysbox")
    assert row["online"] is True and row["cpu_pct"] == 12.5 and row["ram_pct"] == 61.0 and row["provider_states"] == {}
    (f,) = deps["fleet"]()
    assert f["hostname"] == "sysbox" and f["online"] is True and f["providers"] == [] and f["watts"] is None
