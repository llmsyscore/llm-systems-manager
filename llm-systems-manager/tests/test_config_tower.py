"""#924 Tower config: defaults, catalog, hot reload."""
from __future__ import annotations

import pytest
import types

import settings_catalog as sc
from config.unified_config import ManagerConfig, ManagerTower


def test_defaults_are_off_read_only_in_scope():
    t = ManagerTower()
    assert (t.enabled, t.model, t.tool_mode, t.capabilities, t.off_topic) == (False, "auto", "auto", "read", "refuse")
    assert t.disabled_tools == [] and t.max_tool_calls == 8 and t.max_tokens == 1024
    assert t.history_days == 30 and t.min_severity == "warning"
    assert t.request_timeout_s == 45 and t.fallback is False
    assert ManagerConfig().tower.enabled is False


@pytest.mark.parametrize("path,value", [
    ("manager.tower.capabilities", "root"), ("manager.tower.off_topic", "maybe"),
    ("manager.tower.tool_mode", "yes"), ("manager.tower.max_tool_calls", 0),
    ("manager.tower.max_tokens", 64), ("manager.tower.history_days", 0), ("manager.tower.request_timeout_s", 2),
])
def test_catalog_rejects_out_of_range(path, value):
    clean, errors = sc.validate_and_coerce({path: value})
    assert path in errors and path not in clean


def test_catalog_group_and_hot_flags():
    keys = {e["path"] for e in sc.CATALOG if e["group"] == "tower"}
    assert keys == {f"manager.tower.{k}" for k in (
        "enabled", "model", "tool_mode", "capabilities", "off_topic", "disabled_tools",
        "diagnose_alarms", "playbooks_auto", "min_severity", "max_tool_calls", "max_tokens", "temperature",
        "request_timeout_s", "fallback", "history_days")}
    assert all(e["hot"] for e in sc.CATALOG if e["group"] == "tower")
    assert dict(sc.GROUPS)["tower"] == "Tower assistant"
    common = {e["path"] for e in sc.CATALOG if e["group"] == "tower" and e.get("common")}
    assert common == {"manager.tower.enabled", "manager.tower.model", "manager.tower.capabilities", "manager.tower.diagnose_alarms"}


def test_hot_reload_copies_file_values(monkeypatch):
    import manager_mod as M
    snap = ManagerConfig()
    snap.tower.enabled = True
    snap.tower.model = "qwen3-14b"
    fake = types.SimpleNamespace(manager=snap)
    monkeypatch.setattr(sc, "_snapshot", lambda: fake)
    M._tower_reload_config()
    assert M.settings.manager.tower.enabled is True and M.settings.manager.tower.model == "qwen3-14b"
    M.settings.manager.tower.enabled = False
    M.settings.manager.tower.model = ""
