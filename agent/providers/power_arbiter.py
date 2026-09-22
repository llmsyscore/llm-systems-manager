"""Host power arbiter: the only writer of the performance/powersave units. Stdlib only."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

OUTCOME_VERIFIED = "verified"
OUTCOME_UNVERIFIABLE = "applied_unverifiable"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_DEFERRED = "deferred"

PROFILES = ("performance", "powersave")
OWNERS = ("job", "manual", "policy")
HOLD = "hold"
_RETRY_S = (5.0, 15.0, 60.0)
_SIM_STEP = 0.2
_SIM_REAL_WAIT = 0.05
_SYSTEMCTL = "/usr/bin/systemctl"
_STOP_JOIN_S = 5.0

_log = logging.getLogger("llm-systems-agent.power_arbiter")


def select_mode(*, enabled: bool, is_linux: bool, sudo_list: Optional[str],
                unit_files_present: bool, units: Optional[dict[str, str]] = None) -> tuple[str, str]:
    """(mode, reason): full | observe | disabled."""
    units = dict(units or {p: p for p in PROFILES})
    if not enabled:
        return "disabled", "PERF_CONTROLLER_ENABLED is false"
    if not is_linux:
        return "observe", "perf units are Linux-only"
    if not unit_files_present:
        return "observe", "perf unit files missing: %s / %s" % (units["performance"], units["powersave"])
    text = sudo_list or ""
    missing = [u for u in units.values() if f"reload-or-restart {u}" not in text]
    if missing:
        return "observe", "sudoers lacks: " + ", ".join(f"{_SYSTEMCTL} reload-or-restart {u}" for u in missing)
    return "full", ""


class PowerArbiter:
    """Owns the perf units: one worker thread applies the highest-priority desired profile."""

    def __init__(self, units: dict[str, str], *, mode: str = "full", run=None,
                 governor_reader: Optional[Callable[..., Optional[str]]] = None,
                 readback: Optional[Callable[[str], dict[str, Any]]] = None,
                 record_path: Optional[str] = None, clock=time.monotonic, sleep=time.sleep,
                 dwell_ticks: int = 2, max_switches_per_min: int = 6,
                 logger: Optional[logging.Logger] = None) -> None:
        self.units = dict(units)
        self.mode = mode
        self._run = run
        self._gov = governor_reader or (lambda fresh=False: None)
        self._readback = readback or self._governor_readback
        self._readback_last: list[dict[str, Any]] = []
        self._applied_state: tuple[Optional[str], Optional[str], list[dict[str, Any]]] = (None, None, [])
        self._record_path = record_path
        self._clock = clock
        self._sleep = sleep
        self._simulated = clock is not time.monotonic
        self._dwell = max(1, int(dwell_ticks))
        self._cap = max(1, int(max_switches_per_min))
        self._log = logger or _log
        self._cv = threading.Condition()
        self._holds: dict[str, Optional[str]] = {"job": None, "manual": None}
        self._holders: dict[str, dict[str, str]] = {"job": {}, "manual": {}}
        self._policy: Optional[str] = None
        self._pending: tuple[Optional[str], int] = (None, 0)
        self._applied: Optional[str] = None
        self._outcome: Optional[str] = None
        self._error: Optional[str] = None
        self._governor: Optional[str] = None
        self._last_switch: Optional[dict[str, Any]] = None
        self._last_defer: Optional[dict[str, Any]] = None
        self._last_aggregate: Optional[str] = None
        self._counters = {"switches": 0, "failures": 0, "unverifiable": 0, "deferred": 0,
                          "reconcile_corrections": 0, "unknown_streak_max": 0}
        self._attempts = 0
        self._switch_times: deque = deque()
        self._fail_streak = 0
        self._retry_at: Optional[float] = None
        self._warned_governors: set[str] = set()
        self._warned_switch_error: Optional[tuple[str, str]] = None
        self._busy = False
        self._stop = False
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self, after: Optional[threading.Thread] = None) -> None:
        """No-op while a worker thread is still alive; `after` is a predecessor worker to wait out first."""
        if self._thread is not None and self._thread.is_alive():
            return
        with self._cv:
            self._stop = False

        def _worker() -> None:
            if after is not None and after.is_alive():
                after.join()
            self._loop()

        self._thread = threading.Thread(target=_worker, name="power-arbiter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Clear the worker slot only when the thread actually ended."""
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=_STOP_JOIN_S)
        if thread.is_alive():
            self._log.warning("power: worker thread did not exit within %ss; keeping the slot (no new worker)",
                              _STOP_JOIN_S)
            return
        self._thread = None

    def adopt_holds(self, other: "PowerArbiter") -> None:
        """Carry the job/manual holds (and their holders) over from a predecessor arbiter."""
        with other._cv:
            holds, holders = dict(other._holds), {k: dict(v) for k, v in other._holders.items()}
            last_aggregate = other._last_aggregate
        with self._cv:
            self._holds, self._holders = holds, holders
            self._last_aggregate = last_aggregate
            self._cv.notify_all()

    def recover(self) -> None:
        """Adopt the durable record when the governor agrees; holds are never restored."""
        if not self._record_path:
            return
        try:
            with open(self._record_path) as f:
                rec = json.load(f) or {}
        except (OSError, ValueError):
            return
        applied = rec.get("applied_profile")
        rb = self._safe_readback(applied) if applied in PROFILES else {"governor": self._read_governor()}
        with self._cv:
            self._governor = rb.get("governor")
            self._readback_last = list(rb.get("checks") or [])
            if applied in PROFILES:
                if rb.get("outcome") == OUTCOME_FAILED:
                    self._log.info("power: record says %s but the host reads otherwise (%s); re-applying on the next tick",
                                   applied, rb.get("error"))
                else:
                    self._applied = applied
                    self._outcome = rb.get("outcome") or rec.get("outcome")
                    self._applied_state = (self._outcome, None, list(rb.get("checks") or []))
            if rec.get("owner") in ("job", "manual"):
                self._log.info("power: dropping the %s hold recorded by the previous run", rec["owner"])
            self._cv.notify_all()

    # ── requests ─────────────────────────────────────────────────────
    def request(self, profile: str, owner: str, timeout: float = 45.0,
                holder: Optional[str] = None) -> dict[str, Any]:
        """Set an owner hold (per named holder) and block until an attempt made after this call settles."""
        if profile not in PROFILES or owner not in ("job", "manual"):
            raise ValueError(f"bad request {owner}:{profile}")
        with self._cv:
            seq0 = self._attempts
            self._holds[owner] = profile
            self._holders[owner][holder or ""] = profile
            self._cv.notify_all()
        return self._wait_settled(profile, owner, timeout, seq0)

    def release(self, owner: str, holder: Optional[str] = None) -> None:
        """Drop one named holder; the owner hold stays while other holders remain. No holder drops all."""
        if owner not in ("job", "manual"):
            raise ValueError(f"bad release {owner}")
        with self._cv:
            names = self._holders[owner]
            if holder is None:
                names.clear()
            else:
                names.pop(holder, None)
            self._holds[owner] = next(reversed(names.values()), None) if names else None
            self._cv.notify_all()

    def set_policy(self, profile_or_hold: str) -> None:
        with self._cv:
            if profile_or_hold == HOLD:
                return
            prof, n = self._pending
            n = n + 1 if prof == profile_or_hold else 1
            self._pending = (profile_or_hold, n)
            if n >= self._dwell:
                self._policy = profile_or_hold
                self._cv.notify_all()

    def on_aggregate(self, aggregate: str) -> None:
        with self._cv:
            if self._last_aggregate is not None and aggregate != self._last_aggregate and self._holds["manual"]:
                self._holds["manual"] = None
                self._holders["manual"].clear()
                self._log.info("power: manual hold released (aggregate %s -> %s)", self._last_aggregate, aggregate)
                self._cv.notify_all()
            self._last_aggregate = aggregate

    def wait_idle(self, timeout: float) -> None:
        """Test helper: block until desired == applied and the worker is idle."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                desired, _ = self._effective()
                settled = desired is None or desired == self._applied or self.mode != "full"
                if settled and not self._busy:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._cv.wait(min(remaining, 0.05))

    # ── snapshot ─────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        with self._cv:
            desired, owner = self._effective()
            return {"mode": self.mode, "desired": desired, "applied": self._applied, "owner": owner,
                    "outcome": self._outcome, "error": self._error, "governor": self._governor,
                    "readback": [dict(c) for c in self._readback_last],
                    "last_switch": dict(self._last_switch) if self._last_switch else None,
                    "counters": dict(self._counters), "enabled": self.mode != "disabled",
                    "awake": self.units["performance"], "sleep": self.units["powersave"]}

    # ── internals ────────────────────────────────────────────────────
    def _effective(self) -> tuple[Optional[str], Optional[str]]:
        for owner in ("job", "manual"):
            if self._holds[owner]:
                return self._holds[owner], owner
        return self._policy, ("policy" if self._policy else None)

    def _latest_attempt(self, profile: str, seq0: int) -> Optional[dict[str, Any]]:
        """The newest switch or cap-defer for this profile made after seq0."""
        seen = [a for a in (self._last_switch, self._last_defer)
                if a and a.get("to") == profile and a.get("seq", 0) > seq0]
        return max(seen, key=lambda a: a["seq"]) if seen else None

    def _wait_settled(self, profile: str, owner: str, timeout: float, seq0: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                if self.mode != "full":
                    return self._result(OUTCOME_SKIPPED, f"arbiter mode is {self.mode}")
                if not self._busy:
                    done = self._latest_attempt(profile, seq0)
                    if done:
                        return self._result(done["outcome"], done.get("error"))
                    if self._holds.get(owner) != profile:
                        return self._result(OUTCOME_SKIPPED, f"{owner} hold released")
                    held_by = self._effective()[1]
                    if held_by != owner:
                        return self._result(OUTCOME_SKIPPED, f"outranked by {held_by}")
                    if self._applied == profile:
                        settled = (self._outcome if self._outcome in (OUTCOME_VERIFIED, OUTCOME_UNVERIFIABLE)
                                   else OUTCOME_VERIFIED)
                        return self._result(settled, None)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._result(OUTCOME_FAILED, "timeout waiting for the arbiter")
                self._cv.wait(min(remaining, 0.05))

    def _result(self, outcome: str, error: Optional[str]) -> dict[str, Any]:
        return {"outcome": outcome, "applied": self._applied, "governor": self._governor,
                "readback": [dict(c) for c in self._readback_last],
                "error": error, "owner": self._effective()[1], "desired": self._effective()[0],
                "mode": self.mode}

    def _loop(self) -> None:
        while True:
            with self._cv:
                desired = owner = None
                while not self._stop:
                    desired, owner = self._effective()
                    if not desired or desired == self._applied or self.mode != "full":
                        if not desired or desired == self._applied:
                            self._retry_at = None          # converged: stop the 0.5s idle wakeups
                            if self._outcome == OUTCOME_FAILED and self._applied is not None:
                                self._outcome, self._error, self._readback_last = self._applied_state
                                self._cv.notify_all()
                        self._cv.wait(0.5 if self._retry_at is not None else None)
                        continue
                    if self._retry_at is not None and self._clock() < self._retry_at:
                        self._wait_backoff()
                        continue
                    break
                if self._stop:
                    return
                self._busy = True
            try:
                self._apply(desired, owner)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _wait_backoff(self) -> None:
        """Wait out a retry/cap deadline; an injected clock is advanced by the injected sleep."""
        remaining = max(0.0, (self._retry_at or 0.0) - self._clock())
        if self._simulated:
            self._sleep(min(_SIM_STEP, remaining))
            self._cv.wait(_SIM_REAL_WAIT)
        else:
            self._cv.wait(min(remaining, 0.5))

    def _apply(self, profile: str, owner: Optional[str]) -> None:
        now = self._clock()
        while self._switch_times and now - self._switch_times[0] > 60.0:
            self._switch_times.popleft()
        if len(self._switch_times) >= self._cap:
            with self._cv:
                self._attempts += 1
                self._counters["deferred"] += 1
                self._retry_at = self._switch_times[0] + 60.0
                self._last_defer = {"to": profile, "seq": self._attempts, "outcome": OUTCOME_DEFERRED,
                                    "error": f"switch rate cap reached ({self._cap}/min)"}
            self._log.warning("power: switch cap reached (%d/min); deferring %s", self._cap, profile)
            return
        unit = self.units[profile]
        outcome, error, rb = self._switch(unit, profile)
        governor = rb.get("governor")
        with self._cv:
            self._switch_times.append(self._clock())
            self._governor = governor
            self._readback_last = list(rb.get("checks") or [])
            self._attempts += 1
            if outcome == OUTCOME_FAILED:
                self._counters["failures"] += 1
                self._fail_streak += 1
                self._retry_at = self._clock() + _RETRY_S[min(self._fail_streak, len(_RETRY_S)) - 1]
            else:
                self._counters["switches"] += 1
                if outcome == OUTCOME_UNVERIFIABLE:
                    self._counters["unverifiable"] += 1
                self._applied = profile
                self._applied_state = (outcome, error, list(rb.get("checks") or []))
                self._fail_streak = 0
                self._retry_at = None
            self._outcome, self._error = outcome, error
            self._last_switch = {"to": profile, "unit": unit, "ts": time.time(), "owner": owner,
                                 "outcome": outcome, "error": error, "seq": self._attempts}
            record = {"applied_profile": self._applied, "owner": owner, "requested_at": time.time(),
                      "outcome": outcome, "host_has_cpufreq": governor is not None}
        self._write_record(record)
        if outcome == OUTCOME_FAILED:
            key = (profile, error or "")
            if key != self._warned_switch_error:
                self._warned_switch_error = key
                self._log.warning("power: switch to %s (%s) failed: %s", profile, unit, error)
            else:
                self._log.debug("power: switch to %s (%s) failed again: %s", profile, unit, error)
        else:
            self._warned_switch_error = None
            self._log.info("power: %s -> %s (%s, owner=%s)", unit, profile, outcome, owner)

    def _switch(self, unit: str, profile: str) -> tuple[str, Optional[str], dict[str, Any]]:
        run = self._run or subprocess.run
        try:
            r = run(["sudo", "-n", _SYSTEMCTL, "reload-or-restart", unit],
                    capture_output=True, text=True, timeout=30)
        except Exception as e:
            return OUTCOME_FAILED, str(e)[:240], self._safe_readback(profile)
        if r.returncode != 0:
            return OUTCOME_FAILED, (r.stderr or r.stdout or f"rc={r.returncode}").strip()[:240], self._safe_readback(profile)
        try:
            s = run([_SYSTEMCTL, "show", unit, "-p", "Result,ExecMainStatus"],
                    capture_output=True, text=True, timeout=10)
            if "Result=success" not in (s.stdout or ""):
                return OUTCOME_FAILED, f"unit result: {(s.stdout or '').strip()[:120]}", self._safe_readback(profile)
        except Exception as e:
            self._log.debug("power: systemctl show failed: %s", e)
        rb = self._safe_readback(profile)
        return rb.get("outcome") or OUTCOME_UNVERIFIABLE, rb.get("error"), rb

    def _safe_readback(self, profile: str) -> dict[str, Any]:
        try:
            rb = self._readback(profile)
        except Exception as e:
            self._log.warning("power: readback failed: %s", e)
            rb = {"outcome": OUTCOME_UNVERIFIABLE, "checks": [], "governor": None, "error": None}
        rb.setdefault("checks", [])
        rb.setdefault("governor", None)
        return rb

    def _governor_readback(self, profile: str) -> dict[str, Any]:
        """Default readback: grade the cpufreq governor alone."""
        gov = self._read_governor()
        if gov is None:
            return {"outcome": OUTCOME_UNVERIFIABLE, "checks": [], "governor": None, "error": None}
        check = {"label": "cpu governor", "expected": profile, "actual": gov, "ok": gov == profile}
        if gov == profile:
            return {"outcome": OUTCOME_VERIFIED, "checks": [check], "governor": gov, "error": None}
        other = PROFILES[1] if profile == PROFILES[0] else PROFILES[0]
        if gov == other:
            return {"outcome": OUTCOME_FAILED, "checks": [check], "governor": gov,
                    "error": f"governor readback {gov!r} != {profile!r}"}
        # Third-party governor (schedutil, ondemand, a hybrid list): applied, not provable.
        if gov not in self._warned_governors:
            self._warned_governors.add(gov)
            self._log.warning("power: governor reads %r, neither %s nor %s; switches stay unverifiable",
                              gov, PROFILES[0], PROFILES[1])
        return {"outcome": OUTCOME_UNVERIFIABLE, "checks": [check], "governor": gov, "error": None}

    def _read_governor(self) -> Optional[str]:
        try:
            return self._gov(fresh=True)
        except Exception:
            return None

    def _write_record(self, rec: dict[str, Any]) -> None:
        if not self._record_path:
            return
        tmp = f"{self._record_path}.tmp.{os.getpid()}"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(rec, f)
            os.replace(tmp, self._record_path)
        except OSError as e:
            self._log.warning("power: record write failed (%s); continuing in memory", e)
            try:
                os.unlink(tmp)
            except OSError:
                self._log.debug("power: temp record %s already gone", tmp)


# ── module singleton ─────────────────────────────────────────────────
_ARBITER: Optional[PowerArbiter] = None
_LOCK = threading.Lock()


def get() -> PowerArbiter:
    global _ARBITER
    with _LOCK:
        if _ARBITER is None:
            _ARBITER = PowerArbiter({"performance": "performance", "powersave": "powersave"}, mode="disabled")
        return _ARBITER


def configure(*, enabled: bool, is_linux: bool, awake_unit: str, sleep_unit: str,
              record_path: Optional[str], sudo_list_fn: Callable[[], Optional[str]],
              unit_exists_fn: Callable[[str], bool], governor_reader, logger=None,
              dwell_ticks: int = 2, readback_factory=None) -> PowerArbiter:
    """Build (or rebuild) the singleton from config; runs validation and recover()."""
    global _ARBITER
    units = {"performance": awake_unit, "powersave": sleep_unit}
    sudo_list = sudo_list_fn() if enabled and is_linux else None
    present = enabled and is_linux and all(unit_exists_fn(u) for u in units.values())
    mode, reason = select_mode(enabled=enabled, is_linux=is_linux, sudo_list=sudo_list,
                               unit_files_present=present, units=units)
    lg = logger or _log
    if mode == "observe":
        lg.error("power arbiter in OBSERVE mode (state reported, never switched): %s", reason)
    else:
        lg.info("power arbiter mode=%s", mode)
    with _LOCK:
        old = _ARBITER
        old_thread = None
        if old is not None:
            old.stop()
            old_thread = old._thread
        readback = readback_factory(units) if readback_factory else None
        arb = PowerArbiter(units, mode=mode, governor_reader=governor_reader, readback=readback,
                           record_path=record_path, dwell_ticks=dwell_ticks, logger=lg)
        arb.recover()
        if old is not None:
            arb.adopt_holds(old)
        arb.start(after=old_thread)
        _ARBITER = arb
        return arb


def read_sudo_list() -> Optional[str]:
    try:
        r = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True, timeout=10)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return None


def unit_file_exists(unit: str) -> bool:
    name = unit if unit.endswith(".service") else f"{unit}.service"
    return any(os.path.exists(os.path.join(d, name))
               for d in ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system"))
