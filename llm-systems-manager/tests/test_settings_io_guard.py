"""#614 interim guard: the duplicated settings_toml_io copies must not drift."""
from pathlib import Path


def test_settings_toml_io_copies_identical():
    root = Path(__file__).resolve().parents[2]
    a = root / "llm-systems-manager" / "backend" / "settings_toml_io.py"
    b = root / "llm-systems-alarm-engine" / "backend" / "settings_toml_io.py"
    assert a.read_bytes() == b.read_bytes(), (
        "settings_toml_io.py drifted between the manager and the alarm "
        "engine — apply the same change to both copies (#614)")
