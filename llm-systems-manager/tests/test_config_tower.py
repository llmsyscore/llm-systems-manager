"""#924 Tower config: defaults, catalog, hot reload."""
from __future__ import annotations

import pytest
import types

import settings_catalog as sc
from config.unified_config import ManagerConfig, ManagerTower


def test_defaults_are_off_read_only_in_scope():
    t = ManagerTower()
    assert (t.enabled, t.model, t.tool_mode, t.capabilities, t.off_topic) == (False, "auto", "auto", "read", "refuse")
    assert t.disabled_tools == [] and t.max_tool_calls == 16 and t.max_tokens == 1024
    assert t.history_days == 30 and t.min_severity == "warning"
    assert t.request_timeout_s == 45 and t.fallback is False and t.thinking == "medium"
    assert t.report_violations is True and t.discord is False and t.diagnose_timeout_s == 180
    assert ManagerConfig().tower.enabled is False


@pytest.mark.parametrize("path,value", [
    ("manager.tower.capabilities", "root"), ("manager.tower.off_topic", "maybe"),
    ("manager.tower.tool_mode", "yes"), ("manager.tower.max_tool_calls", 0), ("manager.tower.thinking", "max"),
    ("manager.tower.max_tokens", 64), ("manager.tower.history_days", 0), ("manager.tower.request_timeout_s", 2),
    ("manager.tower.diagnose_timeout_s", 10), ("manager.tower.diagnose_timeout_s", 901),
])
def test_catalog_rejects_out_of_range(path, value):
    clean, errors = sc.validate_and_coerce({path: value})
    assert path in errors and path not in clean


def test_catalog_group_and_hot_flags():
    keys = {e["path"] for e in sc.CATALOG if e["group"] == "tower"}
    assert keys == {f"manager.tower.{k}" for k in (
        "enabled", "model", "tool_mode", "capabilities", "off_topic", "report_violations", "disabled_tools",
        "diagnose_alarms", "diagnose_timeout_s", "playbooks_auto", "min_severity", "max_tool_calls", "max_tokens", "temperature", "thinking",
        "request_timeout_s", "fallback", "history_days", "discord", "debug")}
    assert all(e["hot"] for e in sc.CATALOG if e["group"] == "tower")
    assert dict(sc.GROUPS)["tower"] == "Tower assistant"
    common = {e["path"] for e in sc.CATALOG if e["group"] == "tower" and e.get("common")}
    assert common == {"manager.tower.enabled", "manager.tower.model", "manager.tower.capabilities", "manager.tower.diagnose_alarms"}


def test_hot_reload_copies_file_values(monkeypatch):
    import manager_mod as M
    snap = ManagerConfig()
    snap.tower.enabled = True
    snap.tower.model = "qwen3-14b"
    snap.tower.diagnose_timeout_s = 240
    fake = types.SimpleNamespace(manager=snap)
    monkeypatch.setattr(sc, "_snapshot", lambda: fake)
    M._tower_reload_config()
    assert M.settings.manager.tower.enabled is True and M.settings.manager.tower.model == "qwen3-14b"
    assert M.settings.manager.tower.diagnose_timeout_s == 240
    M.settings.manager.tower.enabled = False
    M.settings.manager.tower.diagnose_timeout_s = 180
    M.settings.manager.tower.model = ""


def test_debug_switch_defaults_off_and_is_hot_in_both_groups():
    assert ManagerTower().debug is False
    tower_paths = [e["path"] for e in sc.CATALOG if e["group"] == "tower"]
    assert tower_paths[-1] == "manager.tower.debug"
    t = sc._BY_PATH["manager.tower.debug"]
    assert t["type"] == "bool" and t["hot"] is True
    g = sc._BY_PATH["manager.gateway.debug"]
    assert g["type"] == "bool" and g["hot"] is True and g["group"] == "gateway"
    gateway_paths = [e["path"] for e in sc.CATALOG if e["group"] == "gateway"]
    assert gateway_paths[-1] == "manager.gateway.debug"


def test_tower_keys_cover_every_tower_field():
    import manager_mod as M
    assert "debug" in M._TOWER_KEYS and "report_violations" in M._TOWER_KEYS
    assert set(M._TOWER_KEYS) == set(ManagerTower.model_fields)


def test_report_violations_entry_is_hot_manager_owned_and_follows_off_topic():
    e = sc._BY_PATH["manager.tower.report_violations"]
    assert e["type"] == "bool" and e["hot"] is True and e["service"] == "manager" and e["group"] == "tower"
    paths = [x["path"] for x in sc.CATALOG if x["group"] == "tower"]
    assert paths[paths.index("manager.tower.off_topic") + 1] == "manager.tower.report_violations"


def test_gateway_hot_reload_copies_debug(monkeypatch):
    import manager_mod as M
    snap = ManagerConfig()
    snap.gateway.debug = True
    snap.gateway.enabled = True
    monkeypatch.setattr(sc, "_snapshot", lambda: types.SimpleNamespace(manager=snap))
    before = (M.settings.manager.gateway.enabled, M.settings.manager.gateway.debug)
    try:
        M._gateway_reload_config()
        assert M.settings.manager.gateway.debug is True
        assert M.settings.manager.gateway.enabled is True
    finally:
        M.settings.manager.gateway.enabled, M.settings.manager.gateway.debug = before
        M._apply_debug_loggers()
