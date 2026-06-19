# agent/tests/test_liquidctl_gating.py
"""The liquidctl collector must probe absent hardware at most once per process
lifetime (probe-once, off until restart) so it doesn't spawn `sudo liquidctl
status` every tick on hosts without the supporting devices."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent

# Load collectors.liquidctl without triggering collectors/__init__.py (which
# imports system -> psutil, absent in the test venv).
if "collectors" not in sys.modules:
    _pkg = types.ModuleType("collectors")
    _pkg.__path__ = [str(_AGENT_ROOT / "collectors")]
    sys.modules["collectors"] = _pkg


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("collectors._shared", _AGENT_ROOT / "collectors" / "_shared.py")
lq = _load("collectors.liquidctl", _AGENT_ROOT / "collectors" / "liquidctl.py")

_KRAKEN_OUTPUT = (
    "NZXT Kraken X\n"
    "Liquid temperature    30.5  °C\n"
    "Pump speed            1800  rpm\n"
)


def _config(enabled=True):
    return types.SimpleNamespace(
        COLLECT_LIQUIDCTL_ENABLED=enabled,
        COLLECT_SENSORS_ENABLED=False,
        POLL_INTERVAL_S=5.0,
        LIQUIDCTL_BIN="",
    )


@pytest.fixture(autouse=True)
def _reset_state():
    # Fresh module state per test — these globals carry absence memory.
    lq._liquidctl_cache = {}
    lq._liquidctl_last_poll = 0.0
    lq._binary_missing = False
    lq._absent_matches = set()
    yield


def _match_of(cmd):
    return cmd[cmd.index("--match") + 1]


def test_missing_binary_probes_once_then_stops(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        raise FileNotFoundError("liquidctl")

    monkeypatch.setattr(lq.subprocess, "check_output", fake)
    lq.set_deps(config=_config())

    r1 = lq.collect_liquidctl()
    after_first = len(calls)
    r2 = lq.collect_liquidctl()
    after_second = len(calls)

    assert r1 == {}
    assert r2 == {}
    assert after_first >= 1                 # probed at least once
    assert after_second == after_first      # no new subprocess on the 2nd pass


def test_absent_devices_probe_once_each_then_stop(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        raise lq.subprocess.CalledProcessError(1, cmd)   # no matching device

    monkeypatch.setattr(lq.subprocess, "check_output", fake)
    lq.set_deps(config=_config())

    r1 = lq.collect_liquidctl()
    first = len(calls)
    lq.collect_liquidctl()
    second = len(calls)

    assert r1 == {}
    assert first == 3                       # one probe per hardcoded pattern
    assert second == first                  # absent patterns skipped next pass


def test_exit_zero_empty_output_caches_absent(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return ""    # liquidctl ran but matched/printed no device

    monkeypatch.setattr(lq.subprocess, "check_output", fake)
    lq.set_deps(config=_config())

    r1 = lq.collect_liquidctl()
    first = len(calls)
    lq.collect_liquidctl()

    assert r1 == {}
    assert first == 3                       # one probe per pattern
    assert len(calls) == first              # no re-probe after exit-0-empty


def test_disabled_flag_spawns_no_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(lq.subprocess, "check_output",
                        lambda *a, **k: calls.append(a) or "")
    lq.set_deps(config=_config(enabled=False))

    assert lq.collect_liquidctl() == {}
    assert calls == []


def test_transient_error_does_not_permanently_disable(monkeypatch):
    state = {"fail": True}
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        m = _match_of(cmd)
        if m == "Kraken" and state["fail"]:
            raise lq.subprocess.TimeoutExpired(cmd, 5)   # transient, not absence
        if m == "Kraken":
            return _KRAKEN_OUTPUT
        raise lq.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(lq.subprocess, "check_output", fake)
    lq.set_deps(config=_config())

    assert lq.collect_liquidctl() == {}     # Kraken timed out, others absent
    state["fail"] = False
    r2 = lq.collect_liquidctl()             # Kraken must be re-probed, not skipped

    assert r2["aio"]["Liquid temperature"]["value"] == 30.5


def test_present_device_keeps_being_probed(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if _match_of(cmd) == "Kraken":
            return _KRAKEN_OUTPUT
        raise lq.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(lq.subprocess, "check_output", fake)
    lq.set_deps(config=_config())

    r1 = lq.collect_liquidctl()
    assert r1["aio"]["Liquid temperature"]["value"] == 30.5

    calls.clear()
    lq.collect_liquidctl()
    probed = [_match_of(c) for c in calls]
    assert "Kraken" in probed               # present device still polled
    assert "HX1000i" not in probed          # absent ones stay skipped
    assert "Smart Device" not in probed
