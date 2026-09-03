"""Audit settings (#794): catalog entries, model defaults, hot-apply flag."""
from __future__ import annotations

import settings_catalog as sc


def test_audit_catalog_entries_are_hot_and_manager_owned():
    for p in ("manager.audit.retention_days", "manager.audit.page_size",
              "manager.audit.save_automated", "manager.audit.automated_actors",
              "manager.audit.disabled_events"):
        e = sc.entry_for(p)
        assert e and e["group"] == "audit" and e["service"] == "manager" and e.get("hot") is True
    assert sc.services_for(["manager.audit.retention_days"]) == set()
    assert sc.is_hot("manager.audit.page_size") and not sc.is_hot("manager.port")


def test_audit_defaults_from_model():
    a = sc._FileOnlySettings().manager.audit
    assert (a.retention_days, a.page_size, a.save_automated, a.disabled_events) == (60, 25, False, [])
    assert a.automated_actors == ["smoketestuser"]


def test_hot_paths_never_flag_a_restart(monkeypatch):
    boot = dict(sc._BOOT_FILE_VALUES or {})
    boot["manager.audit.retention_days"] = 30
    monkeypatch.setattr(sc, "_BOOT_FILE_VALUES", boot)
    now = dict(boot)
    now["manager.audit.retention_days"] = 60
    assert sc.pending_restart_services(now) == set()
