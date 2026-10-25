"""[manager.telemetry] keys exist in the schema, the catalog and the TOML example."""
from __future__ import annotations
import tomllib
from pathlib import Path

import settings_catalog as sc
from config.unified_config import settings as live

_REPO = Path(__file__).resolve().parents[2]
PATHS = ["manager.telemetry.install_report", "manager.telemetry.crash_reports",
         "manager.telemetry.checkin", "manager.telemetry.heartbeat"]


def test_schema_has_telemetry_defaults():
    t = live.manager.telemetry
    assert isinstance(t.install_report, bool) and isinstance(t.heartbeat, bool)


def test_catalog_entries_exist_and_are_hot():
    by = {e["path"]: e for e in sc.CATALOG}
    for p in PATHS:
        assert p in by, p
        assert by[p]["group"] == "telemetry"
    for p in PATHS[:4]:
        assert by[p]["type"] == "bool" and by[p].get("hot") and by[p].get("common")
    assert "telemetry" in [k for k, _ in sc.GROUPS]


def test_toml_example_matches_model_defaults():
    doc = tomllib.loads((_REPO / "config" / "llm-systems.toml.example").read_text())
    block = doc["manager"]["telemetry"]
    assert block["install_report"] is True
    assert block["crash_reports"] is True
    assert block["checkin"] is False
    assert block["heartbeat"] is False


def test_old_toml_boots_with_defaults():
    # The plain model carries the defaults; the live TOML must not influence this.
    from config.unified_config import ManagerConfig
    t = ManagerConfig().telemetry
    assert t.install_report is True
    assert t.crash_reports is True
    assert t.checkin is False
    assert t.heartbeat is False
