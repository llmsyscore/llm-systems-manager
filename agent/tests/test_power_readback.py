"""#966: switch verification read back from the perf units' own sysfs / nvidia-smi lines."""
from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


PR = _load("power_readback", _AGENT_ROOT / "providers" / "power_readback.py")
PA = _load("power_arbiter_rb", _AGENT_ROOT / "providers" / "power_arbiter.py")

PERF_UNIT = """
[Service]
Type=oneshot
ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g" 2>/dev/null || true; done'
# AMD GPU power profile (auto)
ExecStart=/bin/bash -c 'for f in /sys/class/drm/card*/device/power_dpm_force_performance_level; do echo auto > "$f" 2>/dev/null || true; done'
ExecStart=/bin/bash -c 'for f in /sys/class/drm/card*/device/pp_power_profile_mode; do echo 5 > "$f" 2>/dev/null || true; done'
ExecStart=/bin/bash -c 'echo "vo -120" > /sys/class/drm/card*/device/pp_od_clk_voltage'; echo "c" > /sys/class/drm/card*/device/pp_od_clk_voltage
ExecStart=-/usr/bin/nvidia-smi -pm 1
ExecStart=/usr/bin/nvidia-smi -pl 350
ExecStart=/usr/bin/liquidctl --match Kraken set fan speed 20 30
"""
SAVE_UNIT = """
[Service]
ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo powersave > "$g" 2>/dev/null || true; done'
ExecStart=/bin/bash -c 'for f in /sys/class/drm/card*/device/power_dpm_force_performance_level; do echo low > "$f" 2>/dev/null || true; done'
ExecStart=/bin/bash -c 'for f in /sys/class/drm/card*/device/pp_power_profile_mode; do echo 2 > "$f" 2>/dev/null || true; done'
ExecStart=/usr/bin/nvidia-smi -pl 200
"""
PROFILE_TABLE = """PROFILE_INDEX(NAME) CLOCK_TYPE(NAME) FPS MinFreqType ...
 0 BOOTUP_DEFAULT :
 1 3D_FULL_SCREEN :
 2 POWER_SAVING{star}:
 5 COMPUTE{star2}:
"""


class FakeHost:
    """Fake sysfs tree + nvidia-smi; `files` maps absolute paths to contents."""
    def __init__(self, files=None, nvidia=None, governor=None):
        self.files = dict(files or {})
        self.nvidia = nvidia  # {"power.limit": "350.00", "persistence_mode": "Enabled"} or None
        self.governor = governor
        self.runs = []

    def glob(self, pattern):
        import fnmatch
        return [p for p in self.files if fnmatch.fnmatch(p, pattern)]

    def read(self, path):
        return self.files.get(path)

    def run(self, argv, **kw):
        self.runs.append(list(argv))
        if argv[0] == "nvidia-smi":
            if self.nvidia is None:
                raise FileNotFoundError("nvidia-smi")
            field = argv[1].split("=", 1)[1]
            return types.SimpleNamespace(returncode=0, stdout=self.nvidia.get(field, "") + "\n", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    def read_governor(self, fresh=False):
        return self.governor


def _rb(host, units=None):
    texts = units or {"performance": PERF_UNIT, "powersave": SAVE_UNIT}
    return PR.Readback({"performance": "performance", "powersave": "powersave"}, run=host.run, glob_fn=host.glob,
                       read_fn=host.read, unit_text_fn=lambda u: texts.get(u), governor_reader=host.read_governor,
                       logger=logging.getLogger("t"))


def _amd(level, profile_idx):
    return {
        "/sys/class/drm/card1/device/power_dpm_force_performance_level": level + "\n",
        "/sys/class/drm/card1/device/pp_power_profile_mode": PROFILE_TABLE.format(
            star="*" if profile_idx == "2" else "", star2="*" if profile_idx == "5" else ""),
    }


# ── parsing ──────────────────────────────────────────────────────────
def test_parse_loop_direct_nvidia_and_skips_unreadable_targets():
    checks = PR.parse_checks(PERF_UNIT)
    by_key = {c["key"]: c for c in checks}
    assert by_key["/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"]["expected"] == "performance"
    assert by_key["/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"]["label"] == "cpu governor"
    assert by_key["/sys/class/drm/card*/device/power_dpm_force_performance_level"]["expected"] == "auto"
    assert by_key["/sys/class/drm/card*/device/pp_power_profile_mode"]["expected"] == "5"
    assert by_key["nvidia_pl"]["expected"] == "350" and by_key["nvidia_pm"]["expected"] == "1"
    assert not any("pp_od_clk_voltage" in (c["path"] or "") for c in checks)
    assert len(checks) == 5


def test_parse_direct_echo_and_dedups_drop_in_repeats():
    text = ('ExecStart=/bin/bash -c \'echo "balance_power" > /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference\'\n'
            'ExecStart=/bin/sh -c "echo low > /sys/class/drm/card1/device/power_dpm_force_performance_level"\n'
            'ExecStart=/bin/sh -c "echo auto > /sys/class/drm/card1/device/power_dpm_force_performance_level"\n')
    checks = PR.parse_checks(text)
    assert [(c["label"], c["expected"]) for c in checks] == [("cpu epp", "balance_power"), ("gpu level", "low")]


def test_parse_ignores_non_execstart_and_script_only_units():
    assert PR.parse_checks("[Service]\nExecStart=/usr/local/bin/perf.sh performance\n# echo x > /sys/foo\n") == []


# ── grading ──────────────────────────────────────────────────────────
def test_amd_only_host_verifies_each_profile_from_gpu_sysfs():
    host = FakeHost(files=_amd("auto", "5"))
    rb = _rb(host)
    r = rb("performance")
    assert r["outcome"] == "verified" and r["governor"] is None
    labels = {c["label"]: c for c in r["checks"]}
    assert labels["cpu governor"]["actual"] is None and labels["cpu governor"]["ok"] is None
    assert labels["gpu level"] == {"label": "gpu level", "expected": "auto", "actual": "auto", "ok": True}
    assert labels["gpu profile"]["actual"] == "5" and labels["gpu profile"]["ok"] is True
    assert labels["gpu power limit"]["actual"] is None
    host.files.update(_amd("low", "2"))
    assert rb("powersave")["outcome"] == "verified"


def test_gpu_still_on_the_other_profile_is_failed():
    host = FakeHost(files=_amd("low", "2"))
    r = _rb(host)("performance")
    assert r["outcome"] == "failed"
    assert "gpu level" in r["error"] and "powersave" in r["error"]


def test_unrelated_value_is_unverifiable_and_warns_once(caplog):
    host = FakeHost(files=_amd("manual", "5"))
    rb = _rb(host)
    with caplog.at_level(logging.WARNING, logger="t"):
        assert rb("performance")["outcome"] == "applied_unverifiable"
        assert rb("performance")["outcome"] == "applied_unverifiable"
    assert sum("neither" in m for m in caplog.messages) == 1


def test_nothing_readable_is_unverifiable():
    host = FakeHost()
    r = _rb(host)("performance")
    assert r["outcome"] == "applied_unverifiable" and all(c["actual"] is None for c in r["checks"])


def test_nvidia_limit_and_persistence_read_back():
    host = FakeHost(nvidia={"power.limit": "350.00", "persistence_mode": "Enabled"})
    r = _rb(host)("performance")
    assert r["outcome"] == "verified"
    got = {c["label"]: c["actual"] for c in r["checks"]}
    assert got["gpu power limit"] == "350" and got["gpu persistence"] == "1"
    host.nvidia["power.limit"] = "200.00"
    assert _rb(host)("performance")["outcome"] == "failed"


def test_nvidia_rows_that_disagree_read_mixed():
    host = FakeHost(nvidia={"power.limit": "350.00\n200.00", "persistence_mode": "Enabled\nEnabled"})
    r = _rb(host)("performance")
    got = {c["label"]: c for c in r["checks"]}
    assert got["gpu power limit"]["actual"] == "mixed:350,200" and got["gpu power limit"]["ok"] is False
    assert got["gpu persistence"]["actual"] == "1"
    assert r["outcome"] == "applied_unverifiable"


def test_unexpanded_shell_variables_are_skipped():
    text = ('ExecStart=/bin/sh -c "echo $MODE > /sys/class/drm/card1/device/power_dpm_force_performance_level"\n'
            'ExecStart=/bin/bash -c \'for f in /sys/class/drm/card*/device/pp_power_profile_mode; do echo ${P} > "$f"; done\'\n')
    assert PR.parse_checks(text) == []


def test_mixed_multi_gpu_values_do_not_verify():
    files = _amd("auto", "5")
    files["/sys/class/drm/card2/device/power_dpm_force_performance_level"] = "low\n"
    r = _rb(FakeHost(files=files))("performance")
    lvl = next(c for c in r["checks"] if c["label"] == "gpu level")
    assert lvl["actual"].startswith("mixed:") and lvl["ok"] is False
    assert r["outcome"] == "applied_unverifiable"


def test_governor_reported_even_when_the_unit_never_sets_it():
    host = FakeHost(files=_amd("auto", "5"), governor="schedutil")
    r = _rb(host)("performance")
    assert r["outcome"] == "verified" and r["governor"] == "schedutil"


def test_unit_text_uses_systemctl_cat_without_sudo():
    host = FakeHost()
    def run(argv, **kw):
        host.runs.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout=PERF_UNIT, stderr="")
    assert PR.unit_text("performance", run=run) == PERF_UNIT
    assert host.runs == [["/usr/bin/systemctl", "cat", "--no-pager", "performance"]]


# ── arbiter integration ──────────────────────────────────────────────
class _Sys:
    def __init__(self):
        self.t = 0.0
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[1:2] == ["show"]:
            return types.SimpleNamespace(returncode=0, stdout="Result=success\nExecMainStatus=0\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _arb(readback, tmp_path=None):
    sysd = _Sys()
    a = PA.PowerArbiter({"performance": "performance", "powersave": "powersave"}, run=sysd.run, readback=readback,
                        record_path=str(tmp_path / "power.json") if tmp_path else None,
                        clock=sysd.clock, sleep=sysd.sleep)
    a.start()
    return a


def test_arbiter_uses_readback_outcome_and_exposes_checks():
    host = FakeHost(files=_amd("low", "2"))
    calls = []

    def readback(profile):
        calls.append(profile)
        host.files.update(_amd("auto", "5") if profile == "performance" else _amd("low", "2"))
        return _rb(host)(profile)

    a = _arb(readback)
    try:
        r = a.request("performance", "manual", timeout=2)
        assert r["outcome"] == "verified" and calls == ["performance"]
        snap = a.snapshot()
        assert snap["outcome"] == "verified" and snap["governor"] is None
        assert {c["label"]: c["ok"] for c in snap["readback"]}["gpu level"] is True
        assert {c["label"]: c["ok"] for c in r["readback"]}["gpu profile"] is True
    finally:
        a.stop()


def test_arbiter_marks_failed_when_readback_reads_the_other_profile():
    host = FakeHost(files=_amd("low", "2"))
    a = _arb(lambda profile: _rb(host)(profile))
    try:
        r = a.request("performance", "manual", timeout=2)
        assert r["outcome"] == "failed" and "gpu level" in r["error"]
        assert a.snapshot()["applied"] is None
    finally:
        a.stop()


def test_recover_rejects_a_record_the_gpu_disagrees_with(tmp_path):
    rec = tmp_path / "power.json"
    rec.write_text('{"applied_profile": "performance", "owner": "policy", "outcome": "verified"}')
    host = FakeHost(files=_amd("low", "2"))
    a = PA.PowerArbiter({"performance": "performance", "powersave": "powersave"}, run=_Sys().run,
                        readback=lambda p: _rb(host)(p), record_path=str(rec))
    a.recover()
    assert a.snapshot()["applied"] is None
    host.files.update(_amd("auto", "5"))
    a.recover()
    assert a.snapshot()["applied"] == "performance"
    assert a.snapshot()["outcome"] == "verified"
    assert a.snapshot()["readback"][1]["ok"] is True


def test_readback_exception_degrades_to_unverifiable():
    def boom(profile):
        raise RuntimeError("no sysfs")
    a = _arb(boom)
    try:
        r = a.request("powersave", "manual", timeout=2)
        assert r["outcome"] == "applied_unverifiable" and a.snapshot()["readback"] == []
    finally:
        a.stop()
