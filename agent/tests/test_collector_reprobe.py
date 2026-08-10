# agent/tests/test_collector_reprobe.py
"""#536: hardware a collector probes and doesn't find must be re-probed on a
slow interval instead of being disabled until the agent process restarts, and
the outage must be logged once rather than passing silently."""
from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent

# Load the collector modules without collectors/__init__.py, which imports
# system -> psutil (absent in the test venv).
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


sh = _load("collectors._shared", _AGENT_ROOT / "collectors" / "_shared.py")
gpu = _load("collectors.gpu", _AGENT_ROOT / "collectors" / "gpu.py")
ups = _load("collectors.ups", _AGENT_ROOT / "collectors" / "ups.py")
lq = _load("collectors.liquidctl", _AGENT_ROOT / "collectors" / "liquidctl.py")

INTERVAL = 900.0


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _config(**over):
    base = dict(COLLECT_GPU_ENABLED=True, COLLECT_UPS_ENABLED=True,
                COLLECT_LIQUIDCTL_ENABLED=True, COLLECT_SENSORS_ENABLED=False,
                COLLECT_REPROBE_INTERVAL_S=INTERVAL, POLL_INTERVAL_S=5.0,
                LIQUIDCTL_BIN="")
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(sh, "time", c)
    return c


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Every collector caches its probe result in module globals.
    gpu._GPU_PATH = gpu._UNPROBED
    gpu._HWMON = gpu._UNPROBED
    gpu._NVIDIA_PRESENT = gpu._UNPROBED
    gpu._AMD_NAME = gpu._UNPROBED
    gpu._NV_NAME = gpu._UNPROBED
    ups._UPS_DEVICE = ups._UNPROBED
    lq._liquidctl_cache = {}
    lq._liquidctl_last_poll = 0.0
    lq._binary_missing = False
    lq._match_latches.clear()
    for mod in (gpu, ups):
        mod.set_deps(config=_config())
    lq.set_deps(config=_config())
    sh.set_deps(config=_config())
    yield


# ── AbsenceLatch ────────────────────────────────────────────────────
def test_latch_skips_until_the_interval_then_probes_again(clock):
    latch = sh.AbsenceLatch("thing")
    assert latch.should_probe() is True
    latch.record(False)

    assert latch.should_probe() is False
    clock.advance(INTERVAL - 1)
    assert latch.should_probe() is False
    clock.advance(2)
    assert latch.should_probe() is True


def test_latch_warns_once_per_outage_and_notes_recovery(clock, caplog):
    latch = sh.AbsenceLatch("PSU")
    with caplog.at_level(logging.INFO):
        latch.record(False)
        clock.advance(INTERVAL + 1)
        latch.record(False)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "PSU not detected" in warnings[0].getMessage()

        caplog.clear()
        latch.record(True)
        assert [r.getMessage() for r in caplog.records
                if r.levelno == logging.INFO] == ["PSU detected again on re-probe"]

        # A later outage is a new event and warns again.
        caplog.clear()
        latch.record(False)
        assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_latch_probe_that_raises_still_waits_out_the_interval(clock):
    latch = sh.AbsenceLatch("thing")
    latch.record(False)
    clock.advance(INTERVAL + 1)
    assert latch.should_probe() is True      # due; caller's probe now raises
    assert latch.should_probe() is False     # not retried on the very next tick


def test_latch_states_the_retry_cadence_in_readable_units(clock, caplog):
    # "%.0f min" of a 30s interval reads "every 0 min".
    sh.set_deps(config=_config(COLLECT_REPROBE_INTERVAL_S=30))
    with caplog.at_level(logging.WARNING):
        sh.AbsenceLatch("thing").record(False)
    assert "re-probing every 30s" in caplog.records[0].getMessage()

    caplog.clear()
    sh.set_deps(config=_config())
    with caplog.at_level(logging.WARNING):
        sh.AbsenceLatch("thing").record(False)
    assert "re-probing every 15 min" in caplog.records[0].getMessage()


def test_latch_level_is_configurable(clock, caplog):
    latch = sh.AbsenceLatch("optional device", level=logging.INFO)
    with caplog.at_level(logging.INFO):
        latch.record(False)
    assert [r.levelno for r in caplog.records] == [logging.INFO]


def test_latch_interval_follows_config_and_ignores_junk(clock):
    latch = sh.AbsenceLatch("thing")
    sh.set_deps(config=_config(COLLECT_REPROBE_INTERVAL_S=60))
    assert latch.interval_s == 60.0
    for junk in ("", None, 0, -5, "abc"):
        sh.set_deps(config=_config(COLLECT_REPROBE_INTERVAL_S=junk))
        assert latch.interval_s == sh.REPROBE_INTERVAL_S


def test_reconfigure_clears_absence_so_reload_reprobes(clock):
    latch = sh.AbsenceLatch("thing")
    latch.record(False)
    assert latch.should_probe() is False
    sh.set_deps(config=_config())            # what /config/reload triggers
    assert latch.should_probe() is True


# ── GPU ─────────────────────────────────────────────────────────────
def _stub_gpu(monkeypatch, state):
    calls = {"n": 0}

    def find_amd():
        calls["n"] += 1
        return Path("/sys/class/drm/card1/device") if state["present"] else None

    monkeypatch.setattr(gpu, "_find_amd_gpu_path", find_amd)
    monkeypatch.setattr(gpu, "_hwmon_dir",
                        lambda: Path("/hwmon0") if state["hwmon"] else None)
    monkeypatch.setattr(gpu, "_find_nvidia_gpu", lambda: False)
    monkeypatch.setattr(gpu, "_lspci_amd_name", lambda: "Radeon RX")
    return calls


def test_gpu_absent_is_not_reprobed_every_tick(clock, monkeypatch):
    calls = _stub_gpu(monkeypatch, {"present": False, "hwmon": False})
    for _ in range(20):
        assert gpu.collect_gpu() == {}
    assert calls["n"] == 1


def test_gpu_that_appears_later_is_picked_up_without_a_restart(clock, monkeypatch):
    state = {"present": False, "hwmon": False}
    _stub_gpu(monkeypatch, state)
    assert gpu.collect_gpu() == {}

    state["present"] = state["hwmon"] = True     # driver comes up after boot
    assert gpu.collect_gpu() == {}               # still inside the interval
    clock.advance(INTERVAL + 1)
    assert gpu.collect_gpu()["vendor"] == "amd"


def test_gpu_found_is_never_reprobed(clock, monkeypatch):
    calls = _stub_gpu(monkeypatch, {"present": True, "hwmon": True})
    for _ in range(5):
        assert gpu.collect_gpu()["vendor"] == "amd"
    clock.advance(INTERVAL * 10)
    gpu.collect_gpu()
    assert calls["n"] == 1


def test_gpu_card_without_hwmon_keeps_reporting_but_retries_sensors(clock, monkeypatch, caplog):
    # A card whose hwmon didn't come up has no temp/power/fan — an outage
    # worth retrying, but the vram/util fields it does have must still flow.
    state = {"present": True, "hwmon": False}
    _stub_gpu(monkeypatch, state)
    with caplog.at_level(logging.WARNING):
        assert gpu.collect_gpu()["vendor"] == "amd"
    assert "hwmon sensors missing" in caplog.records[0].getMessage()

    state["hwmon"] = True
    clock.advance(INTERVAL + 1)
    gpu.collect_gpu()
    assert gpu._HWMON == Path("/hwmon0")


def test_gpu_disabled_by_config_never_probes(clock, monkeypatch):
    calls = _stub_gpu(monkeypatch, {"present": True, "hwmon": True})
    gpu.set_deps(config=_config(COLLECT_GPU_ENABLED=False))
    assert gpu.collect_gpu() == {}
    assert calls["n"] == 0


# ── UPS ─────────────────────────────────────────────────────────────
def test_ups_absent_is_not_reprobed_every_tick_but_recovers(clock, monkeypatch):
    state = {"dev": None}
    calls = {"n": 0}

    def find():
        calls["n"] += 1
        return state["dev"]

    monkeypatch.setattr(ups, "_find_ups_device", find)
    monkeypatch.setattr(ups.subprocess, "check_output",
                        lambda *a, **k: "  percentage:  91%\n  state: fully-charged\n")

    for _ in range(10):
        assert ups.collect_ups()["percent"] is None
    assert calls["n"] == 1

    state["dev"] = "/org/freedesktop/UPower/devices/ups_hiddev0"
    clock.advance(INTERVAL + 1)
    assert ups.collect_ups()["percent"] == 91.0
    assert calls["n"] == 2


# ── liquidctl ───────────────────────────────────────────────────────
_PSU_OUTPUT = (
    "Corsair HX1000i\n"
    "Estimated input power    145.0  W\n"
)


def test_liquidctl_absent_device_is_retried_after_the_interval(clock, monkeypatch):
    state = {"psu": False}
    seen = []

    def fake(cmd, **kw):
        seen.append(cmd[cmd.index("--match") + 1])
        if cmd[cmd.index("--match") + 1] == "HX1000i" and state["psu"]:
            return _PSU_OUTPUT
        raise lq.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(lq.subprocess, "check_output", fake)

    assert lq.collect_liquidctl() == {}
    seen.clear()
    assert lq.collect_liquidctl() == {}
    assert seen == []                            # all three latched absent

    state["psu"] = True
    clock.advance(INTERVAL + 1)
    out = lq.collect_liquidctl()
    assert out["psu"]["Estimated input power"]["value"] == 145.0


def test_liquidctl_missing_binary_is_retried_after_the_interval(clock, monkeypatch):
    state = {"installed": False}
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if not state["installed"]:
            raise FileNotFoundError("liquidctl")
        if cmd[cmd.index("--match") + 1] == "HX1000i":
            return _PSU_OUTPUT
        raise lq.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(lq.subprocess, "check_output", fake)

    assert lq.collect_liquidctl() == {}
    assert len(calls) == 1                       # one match tried, then short-circuit
    assert lq.collect_liquidctl() == {}
    assert len(calls) == 1

    state["installed"] = True
    clock.advance(INTERVAL + 1)
    assert lq.collect_liquidctl()["psu"]["Estimated input power"]["value"] == 145.0


def test_liquidctl_warns_once_when_enabled_but_nothing_is_found(clock, monkeypatch, caplog):
    monkeypatch.setattr(
        lq.subprocess, "check_output",
        lambda cmd, **kw: (_ for _ in ()).throw(lq.subprocess.CalledProcessError(1, cmd)))

    with caplog.at_level(logging.INFO):
        for _ in range(5):
            lq.collect_liquidctl()
        clock.advance(INTERVAL + 1)
        lq.collect_liquidctl()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "liquidctl devices" in warnings[0].getMessage()
    # Per-device absence is expected on a partly-populated host: info, not warn.
    assert any("liquidctl device 'Kraken'" in r.getMessage()
               for r in caplog.records if r.levelno == logging.INFO)


def test_liquidctl_transient_failure_is_not_reported_as_an_outage(clock, monkeypatch, caplog):
    def fake(cmd, **kw):
        raise lq.subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(lq.subprocess, "check_output", fake)
    with caplog.at_level(logging.WARNING):
        lq.collect_liquidctl()
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_liquidctl_present_device_never_stops_being_polled(clock, monkeypatch):
    def fake(cmd, **kw):
        if cmd[cmd.index("--match") + 1] == "HX1000i":
            return _PSU_OUTPUT
        raise lq.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(lq.subprocess, "check_output", fake)
    for _ in range(5):
        assert lq.collect_liquidctl()["psu"]["Estimated input power"]["value"] == 145.0
    clock.advance(INTERVAL * 5)
    assert lq.collect_liquidctl()["psu"]["Estimated input power"]["value"] == 145.0
