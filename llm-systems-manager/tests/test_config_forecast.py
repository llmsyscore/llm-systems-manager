"""#1031 Forecast config: defaults, catalog."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

import settings_catalog as sc
from config.unified_config import ManagerConfig, ManagerForecast

_BAD_VALUES = [("every", "5m"), ("at", "24:00"), ("at", "3"), ("every_hours", 0), ("every_hours", 721), ("run_day", "monday"), ("window_days", 6), ("window_days", 31),
               ("alert_min_severity", "fatal"), ("digest_day", "monday"), ("digest_at", "8am"),
               ("tower_effort", "sometimes")]


def test_forecast_defaults():
    f = ManagerForecast()
    assert (f.enabled, f.every, f.every_hours, f.run_day, f.at, f.checks_disabled, f.window_days) == \
        (False, "daily", 8, "mon", "03:00", [], 14)
    assert (f.alerts, f.alert_min_severity, f.tower_effort, f.tower_history) == (False, "warning", "auto", False)
    assert (f.digest_day, f.digest_at) == ("mon", "08:00")
    assert ManagerConfig().forecast.enabled is False


def test_an_unknown_key_in_the_section_is_ignored():
    """The never-released tower_mode / tower_analyse keys load as any other stray key does."""
    f = ManagerForecast(tower_mode="explain", tower_analyse=False, tower_effort="light")
    assert f.tower_effort == "light"
    assert not hasattr(f, "tower_mode") and not hasattr(f, "tower_analyse")


@pytest.mark.parametrize("key,bad", _BAD_VALUES)
def test_forecast_rejects_bad_values_pydantic(key, bad):
    with pytest.raises(ValidationError):
        ManagerForecast(**{key: bad})


@pytest.mark.parametrize("key,bad", _BAD_VALUES)
def test_forecast_rejects_bad_values_catalog(key, bad):
    clean, errors = sc.validate_and_coerce({f"manager.forecast.{key}": bad})
    assert f"manager.forecast.{key}" in errors and f"manager.forecast.{key}" not in clean


def test_catalog_has_forecast_group_and_keys():
    paths = {e["path"] for e in sc.CATALOG}
    want = {"enabled", "every", "every_hours", "run_day", "at", "checks_disabled", "window_days", "alerts", "alert_min_severity",
            "tower_effort", "tower_history", "digest_day", "digest_at"}
    assert {f"manager.forecast.{k}" for k in want} <= paths
    history = next(e for e in sc.CATALOG if e["path"] == "manager.forecast.tower_history")
    assert history["help"] == ("Full effort only: keep each run's Tower investigation threads. "
                               "They are not yet viewable in the drawer.")
    entry = next(e for e in sc.CATALOG if e["path"] == "manager.forecast.tower_effort")
    assert entry["type"] == "choice" and entry["label"] == "Tower effort" and entry["hot"] is True
    assert entry["common"] is True
    assert entry["choices"] == ["auto", "off", "light", "standard", "full"]
    assert entry["labels"] == {"auto": "Auto", "off": "Off", "light": "Light", "standard": "Standard", "full": "Full"}
    assert entry["help"] == (
        "How much of a forecast Tower does. Auto picks a level from the Tower model's evaluation score, size, "
        "tool check and speed — the better the model, the more it does. Light = one short digest tying the "
        "findings together. Standard = the digest plus a likely cause per finding. Full = Tower also investigates "
        "each flagged check with its read tools. The code's figures and advice are never replaced. Used only "
        "while Tower is on and a model is awake.")


def test_forecast_group_follows_tower():
    keys = [k for k, _ in sc.GROUPS]
    assert keys.index("forecast") == keys.index("tower") + 1
    assert dict(sc.GROUPS)["forecast"] == "Forecast"
