"""Comment-preserving TOML write-back (#606)."""
from __future__ import annotations

import os
import stat

import pytest

import manager_mod  # noqa: F401  # conftest puts backend/ on sys.path
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
