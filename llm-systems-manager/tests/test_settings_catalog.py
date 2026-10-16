"""Settings catalog: schema consistency + validation semantics (#606)."""
from __future__ import annotations


import settings_catalog as sc
from config.unified_config import settings as live_settings


def _exists(path):
    node = live_settings
    parts = path.split(".")
    for part in parts[:-1]:
        node = getattr(node, part, None)
        if node is None:
            return False
    return hasattr(node, parts[-1])


def test_every_catalog_path_exists_in_schema():
    missing = [e["path"] for e in sc.CATALOG if not _exists(e["path"])]
    assert missing == []


def test_groups_cover_all_entries_and_are_ordered():
    keys = [k for k, _ in sc.GROUPS]
    assert len(keys) == len(set(keys))
    assert {e["group"] for e in sc.CATALOG} <= set(keys)
    assert set(sc.GROUP_PARENTS) <= set(keys)
    assert set(sc.GROUP_PARENTS.values()) <= set(keys)
    assert not (set(sc.GROUP_PARENTS) & set(sc.GROUP_PARENTS.values()))


def test_describe_masks_secrets():
    d = sc.describe()
    secret_paths = {e["path"] for e in sc.CATALOG if e["secret"]}
    for p in secret_paths:
        assert p not in d["values"]
        assert d["secrets"][p] in ("set", "unset")
    assert "manager.port" in d["values"]


def test_validate_coerces_types_and_ranges():
    clean, errors = sc.validate_and_coerce({"manager.port": "5001"})
    assert errors == {} and clean["manager.port"] == 5001
    _, errors = sc.validate_and_coerce({"manager.history.window_minutes": 0})
    assert "manager.history.window_minutes" in errors


def test_validate_rejects_unknown_path_and_bad_choice():
    _, errors = sc.validate_and_coerce({"manager.no_such_knob": 32})
    assert "manager.no_such_knob" in errors  # not in catalog
    _, errors = sc.validate_and_coerce({"manager.branding.palette": "magenta"})
    assert "manager.branding.palette" in errors


def test_secret_semantics():
    clean, errors = sc.validate_and_coerce({"alarm_engine.ingest_token": ""})
    assert errors == {} and "alarm_engine.ingest_token" not in clean  # unchanged
    clean, _ = sc.validate_and_coerce({"alarm_engine.ingest_token": None})
    assert clean["alarm_engine.ingest_token"] == ""                   # cleared
    clean, _ = sc.validate_and_coerce({"manager.gateway.api_keys": None})
    assert clean["manager.gateway.api_keys"] == []                    # list secret clears to []


def test_list_coercion():
    clean, errors = sc.validate_and_coerce(
        {"manager.security.admin_cidrs": ["127.0.0.1", "10.0.0.0/8"]})
    assert errors == {} and clean["manager.security.admin_cidrs"] == ["127.0.0.1", "10.0.0.0/8"]
    _, errors = sc.validate_and_coerce({"manager.security.admin_cidrs": "not-a-list"})
    assert "manager.security.admin_cidrs" in errors


def test_services_for():
    assert sc.services_for(["manager.port"]) == {"manager"}
    assert sc.services_for(["alarm_engine.port"]) == {"alarm_engine"}
    assert sc.services_for(["influxdb.host"]) == {"manager", "alarm_engine"}


def test_int_rejects_bool_and_fractional_float():
    _, errors = sc.validate_and_coerce({"manager.port": True})
    assert "manager.port" in errors
    _, errors = sc.validate_and_coerce({"manager.port": 5001.9})
    assert "manager.port" in errors
    clean, errors = sc.validate_and_coerce({"manager.port": 5001.0})
    assert errors == {} and clean["manager.port"] == 5001


def test_float_rejects_nan_and_inf():
    for bad in ("nan", "inf", float("nan"), float("inf")):
        _, errors = sc.validate_and_coerce({"manager.reportcard.price_kwh": bad})
        assert "manager.reportcard.price_kwh" in errors, bad


def test_describe_reads_fresh_file_values(monkeypatch, tmp_path):
    import settings_toml_io as sio
    p = tmp_path / "llm-systems.toml"
    p.write_text("[manager.history]\nwindow_minutes = 123\n")
    monkeypatch.setattr(sio, "resolve_config_path", lambda: p)
    d = sc.describe()
    assert d["values"]["manager.history.window_minutes"] == 123


# --- nullable entries (#613) ---


def test_nullable_none_passes_validation():
    clean, errors = sc.validate_and_coerce({"manager.energy.price_kwh": None})
    assert errors == {}
    assert clean == {"manager.energy.price_kwh": None}


def test_non_nullable_none_clears_to_default():
    # #797: clearing any non-secret field removes the key so its model
    # default applies again.
    clean, errors = sc.validate_and_coerce({"manager.poll_interval": None})
    assert errors == {}
    assert clean == {"manager.poll_interval": None}


def test_secret_none_still_blanks_rather_than_removing():
    clean, errors = sc.validate_and_coerce({"manager.backup.passphrase": None})
    assert errors == {} and clean == {"manager.backup.passphrase": ""}


def test_nullable_flag_exposed_in_describe():
    entries = {e["path"]: e for e in sc.describe()["entries"]}
    assert entries["manager.energy.price_kwh"].get("nullable") is True
    assert "nullable" not in entries["manager.poll_interval"]


# --- derived pending restart (#611) ---


def test_pending_restart_derives_from_file_drift(tmp_path, monkeypatch):
    import settings_toml_io as sio
    cfg = tmp_path / "llm-systems.toml"
    cfg.write_text("[manager]\npoll_interval = 30\n")
    monkeypatch.setattr(sio, "resolve_config_path", lambda: cfg)
    monkeypatch.setattr(sc, "_BOOT_FILE_VALUES", sc.file_catalog_values())
    assert sc.pending_restart_services() == set()
    cfg.write_text("[manager]\npoll_interval = 60\n")
    assert sc.pending_restart_services() == {"manager"}
    cfg.write_text("[manager]\npoll_interval = 60\n"
                   "[influxdb]\nhost = \"elsewhere\"\n")
    assert sc.pending_restart_services() == {"manager", "alarm_engine"}


def test_pending_restart_empty_when_file_unreadable(monkeypatch):
    monkeypatch.setattr(sc, "file_catalog_values", lambda: None)
    assert sc.pending_restart_services() == set()


# --- model defaults (#797) ---


def test_defaults_come_from_the_pydantic_models():
    d = sc.defaults()
    assert d["manager.port"] == 5000
    assert d["manager.gateway.enabled"] is True
    assert d["manager.backup.keep_last"] == 7
    assert d["manager.security.admin_cidrs"] == ["127.0.0.1", "::1"]


def test_defaults_omit_secrets():
    d = sc.defaults()
    secret_paths = [e["path"] for e in sc.CATALOG if e["secret"]]
    assert secret_paths
    assert not (set(secret_paths) & set(d))


def test_describe_exposes_defaults():
    desc = sc.describe()
    assert desc["defaults"]["manager.port"] == 5000
    # Defaults are a separate map from the live values.
    assert set(desc) >= {"groups", "entries", "values", "secrets", "defaults"}


# --- "Most used" flag (#801) ---


def test_common_flags_the_expected_paths():
    expected = {
        "manager.port", "manager.alarm_engine_url", "manager.auth.mode",
        "manager.auth.session_lifetime_days", "manager.poll_interval",
        "manager.backup.enabled", "manager.backup.interval_hours",
        "manager.gateway.enabled", "manager.companion.release_check",
        "manager.energy.price_kwh", "manager.audit.retention_days",
        "alarm_engine.evaluation_interval", "logging.level",
    }
    common = {e["path"] for e in sc.CATALOG if e.get("common")}
    assert expected <= common


def test_common_flag_survives_describe():
    entries = {e["path"]: e for e in sc.describe()["entries"]}
    assert entries["manager.port"].get("common") is True
    assert "common" not in entries["manager.tls_port"]


def test_pending_restart_paths_skips_hot(monkeypatch):
    boot = {"manager.port": 5000, "manager.audit.page_size": 25,
            "manager.history.window_minutes": 60}
    monkeypatch.setattr(sc, "_BOOT_FILE_VALUES", boot)
    now = {"manager.port": 5001, "manager.audit.page_size": 50,
           "manager.history.window_minutes": 60}
    assert sc.pending_restart_paths(now) == ["manager.port"]
    assert sc.pending_restart_services(now) == {"manager"}


def test_chips_entry_lists_every_tower_tool_and_rejects_unknown_names():
    import tower_tools
    e = sc._BY_PATH["manager.tower.disabled_tools"]
    assert e["type"] == "chips" and e["exclude"] is True and e["label"] == "Available tools"
    assert e["choices"] == list(tower_tools.TOOL_NAMES)
    clean, errors = sc.validate_and_coerce({"manager.tower.disabled_tools": ["log_tail", " help "]})
    assert errors == {} and clean["manager.tower.disabled_tools"] == ["log_tail", "help"]
    _, errors = sc.validate_and_coerce({"manager.tower.disabled_tools": ["rm_rf"]})
    assert "rm_rf" in errors["manager.tower.disabled_tools"]


# --- choice labels + tool groups (round 2, #924) ---


def test_capabilities_entry_has_display_labels():
    e = sc._BY_PATH["manager.tower.capabilities"]
    assert e["labels"] == {"read": "Answer only", "operate": "Answer and act",
                            "admin": "Answer and act, incl. admin actions"}


def test_disabled_tools_entry_groups_cover_every_tool():
    import tower_tools
    e = sc._BY_PATH["manager.tower.disabled_tools"]
    assert list(e["groups"]) == ["Read tools", "Questions", "Timers", "Actions"]
    concatenated = [n for names in e["groups"].values() for n in names]
    assert concatenated == list(tower_tools.TOOL_NAMES)


def test_describe_entries_carry_labels_and_groups_through_unchanged():
    entries = {e["path"]: e for e in sc.describe()["entries"]}
    assert entries["manager.tower.capabilities"]["labels"] == sc._BY_PATH["manager.tower.capabilities"]["labels"]
    assert entries["manager.tower.disabled_tools"]["groups"] == sc._BY_PATH["manager.tower.disabled_tools"]["groups"]


def test_capabilities_still_validates_the_raw_choice_value():
    clean, errors = sc.validate_and_coerce({"manager.tower.capabilities": "operate"})
    assert errors == {} and clean["manager.tower.capabilities"] == "operate"


NEW_1290 = {  # path: (type, group, service, hot)
    "manager.agents.stale_after_s": ("int", "agents", "manager", True),
    "manager.agents.down_after_s": ("int", "agents", "manager", True),
    "manager.agents.liveness_watch_interval_s": ("int", "agents", "manager", True),
    "manager.public_hosts": ("list", "network", "manager", False),
    "manager.http_threads": ("int", "network", "manager", False),
    "manager.listen_backlog": ("int", "network", "manager", False),
    "manager.stream_keepalive_s": ("float", "network", "manager", False),
    "manager.stream_max_lifetime_s": ("float", "network", "manager", False),
    "manager.stream_reserve_threads": ("int", "network", "manager", False),
    "manager.uploads.max_content_length": ("int", "timeouts", "manager", False),
    "manager.timeouts.generic_http": ("float", "timeouts", "manager", False),
    "notifications.timeouts.http": ("float", "notifications", "alarm_engine", False),
    "notifications.timeouts.smtp": ("float", "notifications", "alarm_engine", False),
    "alarm_engine.history.downsampling.rollup_enabled": ("bool", "history", "alarm_engine", False),
    "alarm_engine.history.downsampling.rollup_every": ("choice", "history", "alarm_engine", False),
    "alarm_engine.api_limits.history_since_minutes_max": ("int", "history", "alarm_engine", False),
    "alarm_engine.api_limits.history_max_response_points": ("int", "history", "alarm_engine", False),
    "alarm_engine.api_limits.export_since_minutes_max": ("int", "history", "alarm_engine", False),
    "alarm_engine.caches.max_hosts_per_metric": ("int", "alarm_engine", "alarm_engine", False),
    "alarm_engine.caches.metric_catalog_warmup_days": ("int", "alarm_engine", "alarm_engine", False),
    "openclaw.metrics_push_interval_s": ("float", "openclaw", "manager", False),
    "paths.log_max_bytes": ("int", "logging", "both", False),
    "paths.log_backup_count": ("int", "logging", "both", False),
}


def test_1290_entries_present_with_bounds():
    for p, (typ, group, service, hot) in NEW_1290.items():
        e = sc.entry_for(p)
        assert e and e["type"] == typ and e["group"] == group and e["service"] == service, p
        assert bool(e.get("hot")) == hot, p
        if typ in ("int", "float"):
            assert "min" in e and "max" in e and e["min"] <= sc._current(p) <= e["max"], p


def test_1290_rollup_every_rejects_unschedulable():
    _, errors = sc.validate_and_coerce({"alarm_engine.history.downsampling.rollup_every": "soon"})
    assert errors
    coerced, errors = sc.validate_and_coerce({"alarm_engine.history.downsampling.rollup_every": "10m"})
    assert errors == {} and coerced["alarm_engine.history.downsampling.rollup_every"] == "10m"


def test_1290_rollup_every_choices_match_the_read_grains():
    assert sc.entry_for("alarm_engine.history.downsampling.rollup_every")["choices"] == ["1m", "5m", "10m", "30m", "1h"]


def test_1290_agents_liveness_save_updates_the_live_settings(monkeypatch):
    import types

    import manager_mod as M
    import settings_toml_io

    from config.unified_config import ManagerConfig
    live = M.settings.manager.agents
    for k in ("stale_after_s", "down_after_s", "liveness_watch_interval_s"):
        monkeypatch.setattr(live, k, getattr(live, k))
    snap = ManagerConfig()
    snap.agents.stale_after_s = 42
    snap.agents.down_after_s = 420
    snap.agents.liveness_watch_interval_s = 7
    snap.agents.enrollment_mode = live.enrollment_mode
    snap.agents.enrollment_window_min = live.enrollment_window_min
    snap.agents.enrollment_secret = live.enrollment_secret
    monkeypatch.setattr(settings_toml_io, "apply_patches", lambda *a, **k: None)
    monkeypatch.setattr(sc, "_snapshot", lambda: types.SimpleNamespace(manager=snap))
    M._tower_write_setting("manager.agents.stale_after_s", 42)
    assert (live.stale_after_s, live.down_after_s, live.liveness_watch_interval_s) == (42, 420, 7)


def test_1290_public_hosts_is_a_list_row():
    assert sc.entry_for("manager.public_hosts")["type"] == "list"
