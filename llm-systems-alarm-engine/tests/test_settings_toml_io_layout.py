"""New sub-tables written by the AE settings writer land above the next banner (#1290)."""
from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from backend import settings_toml_io as sio

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "llm-systems.toml.example"


def _example_cfg(tmp_path):
    p = tmp_path / "llm-systems.toml"
    shutil.copy(EXAMPLE, p)
    return p


def _banner(lines, word):
    return next(i for i, ln in enumerate(lines) if ln.startswith("#") and word in ln)


def test_new_subtable_lands_above_the_next_banner(tmp_path):
    p = _example_cfg(tmp_path)
    before = tomllib.loads(p.read_text())
    sio.apply_patches({"manager.timeouts.generic_http": 45.0}, config_path=p)
    lines = p.read_text().splitlines()
    hdr = lines.index("[manager.timeouts]")
    last_mgr = max(i for i, ln in enumerate(lines)
                   if ln.startswith("[manager.") and i != hdr)
    assert last_mgr < hdr < _banner(lines, "ALARM ENGINE")
    before["manager"].setdefault("timeouts", {})["generic_http"] = 45.0
    assert tomllib.loads(p.read_text()) == before


def test_new_ae_subtable_lands_above_influx_banner(tmp_path):
    p = _example_cfg(tmp_path)
    before = tomllib.loads(p.read_text())
    sio.apply_patches({"alarm_engine.api_limits.history_max_response_points": 2000},
                      config_path=p)
    lines = p.read_text().splitlines()
    hdr = lines.index("[alarm_engine.api_limits]")
    assert lines.index("[alarm_engine]") < hdr < _banner(lines, "INFLUXDB")
    before["alarm_engine"].setdefault("api_limits", {})["history_max_response_points"] = 2000
    assert tomllib.loads(p.read_text()) == before
