# agent/tests/test_install_liquidctl_gate.py
"""The installer must enable liquidctl only when a SUPPORTED device is actually
present (probe `liquidctl list`), not merely when the binary is on PATH; and the
example config must not self-enable it by default."""
from __future__ import annotations

import subprocess
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_INSTALL_SH = _AGENT_ROOT / "install" / "install.sh"
_EXAMPLE = _AGENT_ROOT / "agent_config.yaml.example"

# The exact classification predicate the installer uses on `liquidctl list`.
_PRED = "grep -qiE 'kraken|hx1000i|smart device'"


def test_installer_probes_hardware_not_just_binary():
    text = _INSTALL_SH.read_text()
    # detection must run an actual device enumeration, not only _offer_apt_install
    assert "liquidctl list" in text
    assert _PRED in text


def test_example_config_does_not_self_enable_liquidctl():
    text = _EXAMPLE.read_text()
    assert "COLLECT_LIQUIDCTL_ENABLED: false" in text
    assert "COLLECT_LIQUIDCTL_ENABLED: true" not in text


def _classifies(list_output: str) -> bool:
    r = subprocess.run(
        ["bash", "-c", f"printf '%s' \"$1\" | {_PRED}", "_", list_output],
        capture_output=True,
    )
    return r.returncode == 0


def test_predicate_matches_supported_devices():
    assert _classifies("Device #0: NZXT Kraken X53\n")
    assert _classifies("Device #0: Corsair HX1000i\n")
    assert _classifies("Device #0: NZXT Smart Device V2\n")


def test_predicate_rejects_absent_or_unrelated_hardware():
    assert not _classifies("")                                  # no devices
    assert not _classifies("Device #0: Some Other Cooler\n")    # unsupported
