# agent/tests/test_log_watch_self_echo_1259.py
# #1259: a log-watch rule must not re-fire from the alarm engine's own
# "ALERT CREATED/CLOSED ... rule=<name>" lines about that rule's alert.
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"
PATTERN = r"(?i)influx.*(write.*fail|unavailable|circuit.*open)"


def _extract_func(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", AGENT_PY.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


def _ns(posted: list):
    cfg = SimpleNamespace(LOG_WATCH_MAX_BYTES_PER_TICK=1_048_576)
    ns: dict[str, Any] = {
        "CONFIG": cfg, "Path": Path, "os": os, "re": re, "time": time,
        "Optional": Optional, "Any": Any,
        "logger": logging.getLogger("test"),
        "_log_watch_state": {}, "_log_watch_compiled": {}, "_log_watch_dirty": False,
        "_log_watch_post": lambda rule, line: posted.append(line),
    }
    for fn in ("_log_watch_compile", "_log_watch_self_echo", "_log_watch_scan_rule"):
        exec(compile(_extract_func(fn), str(AGENT_PY), "exec"), ns)
    return ns


def _rule(path: Path) -> dict:
    return {"name": "ae-influx-write-fail", "path": str(path), "pattern": PATTERN,
            "cooldown_s": 1e-6, "max_per_tick": 5}


def test_self_echo_detects_engine_lines_about_this_rule():
    ns = _ns([])
    echo = ns["_log_watch_self_echo"]
    assert echo("ae-influx-write-fail",
                "ALERT CLOSED: id=abc rule=ae-influx-write-fail host=h reason=manual value=—")
    assert echo("ae-influx-write-fail",
                "ALERT CREATED: id=abc rule=ae-influx-write-fail host=h metric=x severity=critical")
    assert not echo("ae-influx-write-fail", "influxdb write failed: connection refused")
    assert not echo("ae-influx-write-fail", "ALERT CLOSED: id=abc rule=other-rule reason=manual")
    assert not echo("", "rule= anything")
    assert not echo("ae-influx", "ALERT CLOSED: id=abc rule=ae-influx-write-fail reason=manual")
    assert echo("ae-influx", "ALERT CLOSED: id=abc rule=ae-influx reason=manual")


def test_manual_close_line_does_not_refire(tmp_path):
    log = tmp_path / "ae.log"
    log.write_text("startup\n")
    posted: list[str] = []
    ns = _ns(posted)
    rule = _rule(log)
    ns["_log_watch_scan_rule"](rule)  # first observation seeks to EOF

    with log.open("a") as f:
        f.write("influxdb write failed: connection refused\n")
    ns["_log_watch_scan_rule"](rule)
    assert posted == ["influxdb write failed: connection refused"]

    with log.open("a") as f:
        f.write("ALERT CREATED: id=1 rule=ae-influx-write-fail host=h metric=m severity=critical\n")
        f.write("ALERT CLOSED: id=1 rule=ae-influx-write-fail host=h reason=manual value=—\n")
    ns["_log_watch_scan_rule"](rule)
    assert len(posted) == 1

    with log.open("a") as f:
        f.write("influxdb unavailable, retrying\n")
    ns["_log_watch_scan_rule"](rule)
    assert posted[-1] == "influxdb unavailable, retrying"
