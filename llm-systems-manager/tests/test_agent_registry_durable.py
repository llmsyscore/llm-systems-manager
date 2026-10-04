# llm-systems-manager/tests/test_agent_registry_durable.py
"""#1182: the agent registry survives a host crash — saves are flushed to
disk, a previous copy is kept, and a damaged file is set aside, not replaced."""
from __future__ import annotations

import json
import os

import pytest

import agent_registry
import durable_io


def _registry(*ids):
    return {
        "agents": {i: {"agent_id": i, "hostname": f"host-{i}", "status": "approved",
                       "token": i * 8, "capabilities": {}} for i in ids},
        "global": {"auth_disabled": False},
        "schema_version": 2,
    }


def _reset_cache():
    with agent_registry._agents_cache_lock:
        agent_registry._agents_cache.update(
            {"mtime": 0.0, "data": None, "by_token": {}})


@pytest.fixture
def agents_file(tmp_path, monkeypatch):
    f = tmp_path / "agents.json"
    monkeypatch.setattr(agent_registry, "AGENTS_FILE", f)
    _reset_cache()
    yield f
    _reset_cache()


def test_write_durable_flushes_file_then_directory(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(durable_io.os, "fsync",
                        lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(durable_io.os, "replace",
                        lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    target = tmp_path / "state.json"
    durable_io.write_durable(target, '{"a": 1}')
    assert events == ["fsync", "replace", "fsync"]
    assert json.loads(target.read_text()) == {"a": 1}
    assert (target.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_save_agents_is_flushed_before_rename(agents_file, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(durable_io.os, "fsync",
                        lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(durable_io.os, "replace",
                        lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    agent_registry.save_agents(_registry("a"))
    assert events[:2] == ["fsync", "replace"]
    assert (agents_file.stat().st_mode & 0o777) == 0o600


def test_save_agents_keeps_previous_copy(agents_file):
    agent_registry.save_agents(_registry("a"))
    agent_registry.save_agents(_registry("a", "b"))
    bak = agents_file.with_name("agents.json.bak")
    assert set(json.loads(bak.read_text())["agents"]) == {"a"}
    assert set(json.loads(agents_file.read_text())["agents"]) == {"a", "b"}
    assert (bak.stat().st_mode & 0o777) == 0o600


def test_empty_registry_file_falls_back_to_previous_copy(agents_file, caplog):
    agent_registry.save_agents(_registry("a"))
    agent_registry.save_agents(_registry("a", "b"))
    agents_file.write_text("")
    _reset_cache()
    with caplog.at_level("ERROR"):
        data = agent_registry.load_agents()
    assert set(data["agents"]) == {"a"}
    assert set(json.loads(agents_file.read_text())["agents"]) == {"a"}
    aside = list(agents_file.parent.glob("agents.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == ""
    assert "restored the previous copy" in caplog.text
    assert agent_registry.agent_by_token("a" * 8)["agent_id"] == "a"


def test_unreadable_registry_without_previous_copy_is_kept_aside(agents_file, caplog):
    agents_file.write_text('{"agents": {"a": ')
    with caplog.at_level("ERROR"):
        data = agent_registry.load_agents()
    assert data["agents"] == {}
    aside = list(agents_file.parent.glob("agents.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == '{"agents": {"a": '
    assert not agents_file.exists()
    assert "no usable previous copy" in caplog.text


def test_damaged_previous_copy_is_not_used(agents_file):
    agents_file.write_text("")
    agents_file.with_name("agents.json.bak").write_text("[]")
    assert agent_registry.load_agents()["agents"] == {}
    assert not agents_file.exists()


def test_damaged_registry_file_never_replaces_previous_copy(agents_file):
    agent_registry.save_agents(_registry("a"))
    agent_registry.save_agents(_registry("a", "b"))
    agents_file.write_text("")
    agent_registry.save_agents(_registry("c"))
    bak = agents_file.with_name("agents.json.bak")
    assert set(json.loads(bak.read_text())["agents"]) == {"a"}


def test_restored_registry_is_back_on_disk_without_a_rewrite(agents_file, monkeypatch):
    agent_registry.save_agents(_registry("a"))
    agent_registry.save_agents(_registry("a", "b"))
    agents_file.write_text("")
    _reset_cache()
    with monkeypatch.context() as m:
        m.setattr(agent_registry, "write_durable",
                  lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        assert set(agent_registry.load_agents()["agents"]) == {"a"}
        _reset_cache()
        assert set(agent_registry.load_agents()["agents"]) == {"a"}
    agent_registry.save_agents(_registry("a", "d"))
    bak = agents_file.with_name("agents.json.bak")
    assert set(json.loads(bak.read_text())["agents"]) == {"a"}
    assert set(json.loads(agents_file.read_text())["agents"]) == {"a", "d"}


def test_missing_registry_file_is_an_empty_registry(agents_file):
    assert agent_registry.load_agents()["agents"] == {}
    assert list(agents_file.parent.iterdir()) == []
