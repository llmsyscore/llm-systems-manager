"""#1194: one failed scheduler pass is logged, the loop keeps running, and the next
attempt backs off like a failed backup instead of retrying every tick."""
from __future__ import annotations

import logging
import time

import manager_mod as M

_EV_ON = {"active": True, "reason": "on", "interval_h": 24, "keep_last": 7,
          "passphrase": "pw", "interval_s": 86400, "mirror_dir": ""}
_EV_OFF = {**_EV_ON, "active": False, "reason": "off"}


def _quiet(monkeypatch):
    monkeypatch.setattr(M, "_shutting_down", False)
    monkeypatch.setattr(M, "_last_auto_backup_ts", lambda: None)
    monkeypatch.setattr(M, "_backup_log_state", lambda ev: None)
    monkeypatch.setattr(M.time, "sleep", lambda s: None)


def test_a_failing_evaluation_does_not_end_the_loop(monkeypatch, caplog):
    _quiet(monkeypatch)
    calls = {"n": 0}

    def eval_or_fail():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("config unreadable")
        M._shutting_down = True
        return dict(_EV_OFF)

    monkeypatch.setattr(M, "_backup_sched_eval", eval_or_fail)
    with caplog.at_level(logging.WARNING):
        M._backup_scheduler_loop()
    assert calls["n"] == 2
    assert "backup scheduler pass failed" in caplog.text
    assert M._backup_sched_state["reason"] == "off"


def test_a_backup_that_raises_backs_off_instead_of_retrying_every_tick(monkeypatch, caplog):
    _quiet(monkeypatch)
    passes = {"n": 0}
    ran = []

    def eval_on():
        passes["n"] += 1
        if passes["n"] == 3:
            M._shutting_down = True
        return dict(_EV_ON)

    def boom(*a, **k):
        ran.append(time.time())
        raise RuntimeError("disk gone")

    monkeypatch.setattr(M, "_backup_sched_eval", eval_on)
    monkeypatch.setattr(M, "_backup_due_ts", lambda last_ts, last_ok, interval_s, boot_due:
                        (last_ts + min(interval_s, 3600)) if last_ts else 0)
    monkeypatch.setattr(M, "_run_scheduled_backup", boom)
    with caplog.at_level(logging.WARNING):
        M._backup_scheduler_loop()
    assert len(ran) == 1, "the backup is not retried on the very next tick"
    assert M._backup_sched_state["next_attempt"] > time.time() + 3000
    assert "disk gone" in caplog.text
