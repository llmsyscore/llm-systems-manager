"""Reads back what a perf unit wrote: sysfs echo lines and nvidia-smi settings parsed from the unit itself."""
from __future__ import annotations

import glob as _glob
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

PROFILES = ("performance", "powersave")
OUTCOME_VERIFIED = "verified"
OUTCOME_UNVERIFIABLE = "applied_unverifiable"
OUTCOME_FAILED = "failed"

_SYSTEMCTL = "/usr/bin/systemctl"
_NVIDIA_SMI = "nvidia-smi"
# sysfs targets whose readback is not the value written.
_UNREADABLE = ("pp_od_clk_voltage",)
_LABELS = {"scaling_governor": "cpu governor", "power_dpm_force_performance_level": "gpu level",
           "pp_power_profile_mode": "gpu profile", "energy_performance_preference": "cpu epp"}

_log = logging.getLogger("llm-systems-agent.power_readback")

_VALUE = r'(?:"([^"]*)"|\'([^\']*)\'|([^\s>;"\']+))'
_LOOP_RE = re.compile(r'for\s+(\w+)\s+in\s+"?(/[^\s;"]+)"?\s*;\s*do\s+echo\s+' + _VALUE + r'\s*>\s*"?\$\{?\1\}?"?')
_DIRECT_RE = re.compile(r'echo\s+' + _VALUE + r'\s*>\s*"?(/[^\s"\';]+)"?')
_NV_PL_RE = re.compile(r'nvidia-smi\b[^;|&]*?\s-pl\s+(\d+)')
_NV_PM_RE = re.compile(r'nvidia-smi\b[^;|&]*?\s-pm\s+([01])')


def _val(m: "re.Match", base: int) -> str:
    return next((g for g in m.groups()[base:base + 3] if g is not None), "").strip()


def parse_checks(text: str) -> list[dict[str, Any]]:
    """ExecStart lines → readable checks: {key, kind, path, expected, label}; unreadable targets skipped."""
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, path: Optional[str], expected: str) -> None:
        key = path if kind == "sysfs" else kind
        if key in seen:
            return
        seen.add(key)
        if path:
            label = _LABELS.get(Path(path).name, Path(path).name)
        else:
            label = {"nvidia_pl": "gpu power limit", "nvidia_pm": "gpu persistence"}[kind]
        checks.append({"key": key, "kind": kind, "path": path, "expected": expected, "label": label})

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        cmd = line.split("=", 1)[1].lstrip("-+!@ ")
        consumed: list[tuple[int, int]] = []
        for m in _LOOP_RE.finditer(cmd):
            consumed.append(m.span())
            if not m.group(2).endswith(_UNREADABLE) and "$" not in _val(m, 2):
                add("sysfs", m.group(2), _val(m, 2))
        for m in _DIRECT_RE.finditer(cmd):
            if any(a <= m.start() < b for a, b in consumed):
                continue
            path = m.group(4)
            if path.endswith(_UNREADABLE) or "$" in path or "$" in _val(m, 0):
                continue
            add("sysfs", path, _val(m, 0))
        for m in _NV_PL_RE.finditer(cmd):
            add("nvidia_pl", None, m.group(1))
        for m in _NV_PM_RE.finditer(cmd):
            add("nvidia_pm", None, m.group(1))
    return checks


def _profile_mode_index(raw: str) -> Optional[str]:
    """Index of the active row (marked *) in pp_power_profile_mode."""
    for line in raw.splitlines():
        if "*" in line:
            m = re.match(r"\s*(\d+)\s", line)
            if m:
                return m.group(1)
    return None


def unit_text(unit: str, run=subprocess.run) -> Optional[str]:
    """`systemctl cat` output (unit + drop-ins); None when the unit is unknown."""
    try:
        r = run([_SYSTEMCTL, "cat", "--no-pager", unit], capture_output=True, text=True, timeout=10)
    except Exception as e:
        _log.debug("power readback: systemctl cat %s failed: %s", unit, e)
        return None
    if r.returncode != 0:
        return None
    return r.stdout or ""


def _norm(kind: str, name: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if kind == "nvidia_pl":
        try:
            return str(int(round(float(value))))
        except ValueError:
            return value
    if kind == "nvidia_pm":
        return {"enabled": "1", "disabled": "0"}.get(value.lower(), value)
    return value


class Readback:
    """Callable: profile → {outcome, checks, governor, error}; grades against the other profile's values."""

    def __init__(self, units: dict[str, str], *, run=subprocess.run, glob_fn=_glob.glob,
                 read_fn: Optional[Callable[[str], Optional[str]]] = None,
                 unit_text_fn: Optional[Callable[[str], Optional[str]]] = None,
                 governor_reader: Optional[Callable[..., Optional[str]]] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.units = dict(units)
        self._run = run
        self._glob = glob_fn
        self._read = read_fn or self._read_file
        self._unit_text = unit_text_fn or (lambda u: unit_text(u, run=run))
        self._gov = governor_reader or (lambda fresh=False: None)
        self._log = logger or _log
        self._warned: set[tuple[str, str]] = set()

    @staticmethod
    def _read_file(path: str) -> Optional[str]:
        try:
            return Path(path).read_text()
        except OSError:
            return None

    def checks(self, profile: str) -> list[dict[str, Any]]:
        unit = self.units.get(profile, profile)
        text = self._unit_text(unit)
        return parse_checks(text) if text else []

    def read(self, check: dict[str, Any]) -> Optional[str]:
        """Current value for a check; None when nothing is readable."""
        kind, name = check["kind"], Path(check["path"]).name if check["path"] else ""
        if kind == "sysfs":
            values = []
            for p in sorted(self._glob(check["path"])):
                raw = self._read(p)
                if raw is None:
                    continue
                if name == "pp_power_profile_mode":
                    v = _profile_mode_index(raw)
                else:
                    lines = raw.strip().splitlines()
                    v = lines[0].strip() if lines else ""
                if v is not None:
                    values.append(v)
            if not values:
                return None
            return values[0] if len(set(values)) == 1 else "mixed:" + ",".join(values)
        field = "power.limit" if kind == "nvidia_pl" else "persistence_mode"
        try:
            r = self._run([_NVIDIA_SMI, f"--query-gpu={field}", "--format=csv,noheader,nounits"],
                          capture_output=True, text=True, timeout=10)
        except Exception:
            return None
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        values = [_norm(kind, name, ln) for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
        return values[0] if len(set(values)) == 1 else "mixed:" + ",".join(values)

    def __call__(self, profile: str) -> dict[str, Any]:
        other = PROFILES[1] if profile == PROFILES[0] else PROFILES[0]
        other_expected = {c["key"]: c["expected"] for c in self.checks(other)}
        results, outcome, error = [], OUTCOME_UNVERIFIABLE, None
        readable = 0
        for c in self.checks(profile):
            actual = self.read(c)
            ok: Optional[bool] = None if actual is None else (actual == c["expected"])
            results.append({"label": c["label"], "expected": c["expected"], "actual": actual, "ok": ok})
            if actual is None:
                continue
            readable += 1
            if ok:
                continue
            if actual == other_expected.get(c["key"]):
                outcome = OUTCOME_FAILED
                error = error or f"{c['label']} reads {actual!r} ({other}) after {profile}"
            elif (c["label"], actual) not in self._warned:
                self._warned.add((c["label"], actual))
                self._log.warning("power: %s reads %r, neither %s nor %s value; switch stays unverifiable",
                                  c["label"], actual, PROFILES[0], PROFILES[1])
        if outcome != OUTCOME_FAILED and readable and all(r["ok"] for r in results if r["actual"] is not None):
            outcome = OUTCOME_VERIFIED
        try:
            governor = self._gov(fresh=True)
        except Exception:
            governor = None
        return {"outcome": outcome, "checks": results, "governor": governor, "error": error}
