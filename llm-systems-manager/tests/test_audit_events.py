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
    assert a.automated_actors == []


def test_hot_paths_never_flag_a_restart(monkeypatch):
    boot = dict(sc._BOOT_FILE_VALUES or {})
    boot["manager.audit.retention_days"] = 30
    monkeypatch.setattr(sc, "_BOOT_FILE_VALUES", boot)
    now = dict(boot)
    now["manager.audit.retention_days"] = 60
    assert sc.pending_restart_services(now) == set()


def test_tower_violation_is_a_catalogued_event_on_by_default():
    import manager_mod as M
    tower_group = next(g for g in M.AUDIT_EVENT_GROUPS if g["key"] == "tower")
    ev = next(e for e in tower_group["events"] if e["key"] == "tower.violation")
    assert ev["label"] == "Rule-bypass attempt" and ev["default_on"] is True
    assert M._AUDIT_EVENT_GROUP["tower.violation"] == "tower"
    assert M._audit_event_for("tower.violation") == "tower.violation"
    assert M._audit_group_for("tower.violation") == "tower"
    assert M._audit_label("tower.violation") == "Tower rule-bypass attempt"


def test_tower_playbook_actions_belong_to_the_tower_action_event():
    import manager_mod as M
    assert M._audit_event_for("tower.playbook.apply") == "tower.action"
    assert M._audit_event_for("tower.playbook.auto") == "tower.action"
    assert M._audit_label("tower.playbook.apply") == "Applied a Tower playbook"
    assert M._audit_label("tower.playbook.auto") == "Tower applied a safe playbook"
    tower_group = next(g for g in M.AUDIT_EVENT_GROUPS if g["key"] == "tower")
    assert next(e for e in tower_group["events"] if e["key"] == "tower.action")["label"] == "Action approved / denied / playbook applied"
