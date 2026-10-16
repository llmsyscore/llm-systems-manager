"""Comment-preserving TOML write-back (#606)."""
from __future__ import annotations

import os
import shutil
import stat
import tomllib
from pathlib import Path

import pytest
import tomlkit

import settings_toml_io as sio

SAMPLE = '''# top comment survives
[manager]
port = 5000   # inline comment survives
tls_port = 5443

[manager.history]
window_minutes = 60

[unknown_section]   # unknown keys survive round-trip
mystery = "kept"
'''


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text(SAMPLE)
    os.chmod(p, 0o600)
    return p


def test_patch_preserves_comments_and_unknown_keys(cfg):
    sio.apply_patches({"manager.port": 5001}, config_path=cfg)
    text = cfg.read_text()
    assert "# top comment survives" in text
    assert "inline comment survives" in text
    assert 'mystery = "kept"' in text
    assert "port = 5001" in text


def test_patch_creates_missing_tables(cfg):
    sio.apply_patches({"manager.energy.price_kwh": 0.12}, config_path=cfg)
    assert sio.read_sections(("manager",), config_path=cfg)["manager"]["energy"]["price_kwh"] == 0.12


def test_atomic_write_keeps_0600_and_backs_up(cfg):
    sio.apply_patches({"manager.tls_port": 0}, config_path=cfg)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    backups = list((cfg.parent / "backups").glob("llm-systems.toml.*"))
    assert len(backups) == 1
    assert "port = 5000" in backups[0].read_text()  # backup is the pre-write state


def test_backup_pruned_to_keep_last(cfg):
    for i in range(13):
        sio.apply_patches({"manager.port": 5000 + i}, config_path=cfg)
    assert len(list((cfg.parent / "backups").glob("llm-systems.toml.*"))) <= sio._BACKUP_KEEP


def test_schema_validation_rejects_and_leaves_file_untouched(cfg):
    before = cfg.read_text()
    with pytest.raises(sio.SettingsValidationError):
        sio.apply_patches({"manager.port": "not-a-port"}, config_path=cfg)
    assert cfg.read_text() == before


def test_missing_file_starts_from_example_or_empty(tmp_path):
    p = tmp_path / "llm-systems.toml"
    sio.apply_patches({"manager.port": 5002}, config_path=p)
    assert sio.read_sections(("manager",), config_path=p)["manager"]["port"] == 5002
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_read_sections_filters_by_prefix(cfg):
    out = sio.read_sections(("manager",), config_path=cfg)
    assert "unknown_section" not in out


def test_removals_delete_key_and_survive_absent_targets(cfg):
    sio.apply_patches({}, config_path=cfg,
                      removals=["manager.tls_port",
                                "manager.energy.price_kwh",   # absent leaf
                                "no_such.section.key"])       # absent table
    text = cfg.read_text()
    assert "tls_port" not in text
    assert "port = 5000" in text
    assert "# top comment survives" in text


def test_removal_through_non_table_intermediate_fails_cleanly(tmp_path):
    """A string where a table is expected must surface as a validation error
    (from the schema check), never an unhandled TypeError from the removal."""
    p = tmp_path / "llm-systems.toml"
    p.write_text('[manager]\nenergy = "not-a-table"\n')
    with pytest.raises(sio.SettingsValidationError):
        sio.apply_patches({}, config_path=p,
                          removals=["manager.energy.price_kwh"])
    assert 'energy = "not-a-table"' in p.read_text()


def test_backup_prune_ignores_foreign_files(cfg):
    bdir = cfg.parent / "backups"
    bdir.mkdir(exist_ok=True)
    foreign = bdir / "llm-systems.toml.example"
    foreign.write_text("keep me")
    for i in range(13):
        sio.apply_patches({"manager.port": 5100 + i}, config_path=cfg)
    assert foreign.read_text() == "keep me"


EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "llm-systems.toml.example"


def _example_cfg(tmp_path):
    p = tmp_path / "llm-systems.toml"
    shutil.copy(EXAMPLE, p)
    return p


def _line_of(lines, pred):
    return next(i for i, ln in enumerate(lines) if pred(ln))


def test_new_subtable_lands_above_the_next_banner(tmp_path):
    p = _example_cfg(tmp_path)
    before = tomllib.loads(p.read_text())
    sio.apply_patches({"manager.timeouts.generic_http": 45.0}, config_path=p)
    lines = p.read_text().splitlines()
    hdr = lines.index("[manager.timeouts]")
    banner = _line_of(lines, lambda ln: ln.startswith("#") and "ALARM ENGINE" in ln)
    last_mgr = max(i for i, ln in enumerate(lines)
                   if ln.startswith("[manager.") and i != hdr)
    assert last_mgr < hdr < banner
    before["manager"].setdefault("timeouts", {})["generic_http"] = 45.0
    assert tomllib.loads(p.read_text()) == before


def test_new_ae_subtable_lands_above_influx_banner(tmp_path):
    p = _example_cfg(tmp_path)
    before = tomllib.loads(p.read_text())
    sio.apply_patches({"alarm_engine.api_limits.history_max_response_points": 2000},
                      config_path=p)
    lines = p.read_text().splitlines()
    hdr = lines.index("[alarm_engine.api_limits]")
    banner = _line_of(lines, lambda ln: ln.startswith("#") and "INFLUXDB" in ln)
    assert lines.index("[alarm_engine]") < hdr < banner
    before["alarm_engine"].setdefault("api_limits", {})["history_max_response_points"] = 2000
    assert tomllib.loads(p.read_text()) == before


def test_existing_table_write_is_unchanged_by_lift(tmp_path):
    p = _example_cfg(tmp_path)
    old = p.read_text().splitlines()
    sio.apply_patches({"manager.port": 5099}, config_path=p)
    new = p.read_text().splitlines()
    assert len(old) == len(new)
    diff = [(a, b) for a, b in zip(old, new) if a != b]
    assert len(diff) == 1 and "5099" in diff[0][1]


def test_lift_keeps_data_identical():
    text = ('[a]\nx = 1\n\n[a.b]\ny = 2\n\n\n# ──── NEXT ────\n# about next\n\n'
            '[a.new]\nz = 3\n[c]\nw = 4\n')
    lifted = sio._lift_new_sections(text, {"a.new"})
    assert lifted != text
    assert tomllib.loads(lifted) == tomllib.loads(text)
    lines = lifted.splitlines()
    assert lines.index("[a.new]") < lines.index("# ──── NEXT ────") < lines.index("[c]")


def test_new_subtable_without_banner_is_left_in_place(tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text(SAMPLE)
    sio.apply_patches({"manager.energy.price_kwh": 0.12}, config_path=p)
    text = p.read_text()
    doc = tomlkit.parse(SAMPLE)
    doc["manager"]["energy"] = tomlkit.table()
    doc["manager"]["energy"]["price_kwh"] = 0.12
    assert text == tomlkit.dumps(doc)
