# agent/tests/test_powermetrics_latch.py
"""#544: a powermetrics reader exit must back off on the AbsenceLatch interval
and recover, instead of disabling the collector for the life of the process.
Only a missing binary stays permanently disabled."""
from __future__ import annotations

import contextlib
import importlib.util
import logging
import re
import sys
import threading
import types
import typing
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_AGENT_PY = _AGENT_ROOT / "llm-systems-agent.py"

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


sh = sys.modules.get("collectors._shared") or _load(
    "collectors._shared", _AGENT_ROOT / "collectors" / "_shared.py")

INTERVAL = 900.0


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _extract(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", _AGENT_PY.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


class FakeStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class FakePopen:
    def __init__(self, chunks: list[bytes], returncode: int = 1) -> None:
        self.stdout = FakeStdout(chunks)
        self.returncode = returncode
        self._alive = True

    def poll(self):
        return None if self._alive else self.returncode

    def wait(self, timeout=None):
        self._alive = False
        return self.returncode


class FakeThread:
    """Captures the reader target so tests can run it synchronously."""
    spawned: "list[FakeThread]" = []

    def __init__(self, target=None, args=(), name=None, daemon=None) -> None:
        self.target, self.args = target, args
        FakeThread.spawned.append(self)

    def start(self) -> None:
        pass

    def run_reader(self) -> None:
        self.target(*self.args)


@contextlib.contextmanager
def _best_effort(_what, **_kw):
    try:
        yield
    except Exception:
        pass


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(sh, "time", c)
    return c


def _harness(clock, *, popen_factory, platform="darwin"):
    """Exec the powermetrics functions with stubbed process plumbing."""
    FakeThread.spawned = []
    config = types.SimpleNamespace(
        COLLECT_POWERMETRICS_ENABLED=True, POWERMETRICS_INTERVAL_MS=5000,
        COLLECT_REPROBE_INTERVAL_S=INTERVAL, POLL_INTERVAL_S=5.0)
    sh.set_deps(config=config)
    latch = sh.AbsenceLatch("powermetrics (COLLECT_POWERMETRICS_ENABLED is on)",
                            logger=logging.getLogger("pm-test"))
    ns = {
        "sys": types.SimpleNamespace(platform=platform),
        "subprocess": types.SimpleNamespace(
            Popen=popen_factory, PIPE=-1, DEVNULL=-3),
        "threading": types.SimpleNamespace(Thread=FakeThread),
        "logger": logging.getLogger("pm-test"),
        "best_effort": _best_effort,
        "Any": typing.Any,
        "collect_enabled": sh.collect_enabled,
        "CONFIG": config,
        "_pm_lock": threading.Lock(),
        "_pm_latest": {},
        "_pm_proc": None,
        "_pm_thread": None,
        "_pm_binary_missing": False,
        "_pm_latch": latch,
        "_pm_parse_sample": lambda raw: {"soc_total_w": 12.5},
    }
    for fn in ("_pm_should_run", "_pm_reader_loop", "_pm_ensure_running",
               "collect_powermetrics"):
        exec(compile(_extract(fn), str(_AGENT_PY), "exec"), ns)
    return ns


def _run_reader(ns) -> None:
    assert FakeThread.spawned, "no reader thread was spawned"
    FakeThread.spawned.pop(0).run_reader()


def test_reader_exit_backs_off_then_respawns(clock):
    procs = []

    def popen(cmd, **kw):
        p = FakePopen([b"sample\x00"])           # one sample, then EOF
        procs.append(p)
        return p

    ns = _harness(clock, popen_factory=popen)
    assert ns["collect_powermetrics"]() == {}    # first tick spawns
    assert len(procs) == 1
    _run_reader(ns)                              # sample lands, then reader dies

    assert ns["_pm_latest"] == {}                # stale snapshot cleared
    assert ns["collect_powermetrics"]() == {}    # inside backoff: no respawn
    assert len(procs) == 1

    clock.advance(INTERVAL + 1)
    ns["collect_powermetrics"]()                 # interval over → respawn
    assert len(procs) == 2


def test_reader_exit_warns_once_and_recovery_is_logged(clock, caplog):
    def popen(cmd, **kw):
        return FakePopen([b"sample\x00"])

    ns = _harness(clock, popen_factory=popen)
    with caplog.at_level(logging.INFO, logger="pm-test"):
        ns["collect_powermetrics"]()
        _run_reader(ns)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "powermetrics" in warnings[0].getMessage()
    assert "re-probing" in warnings[0].getMessage()

    caplog.clear()
    clock.advance(INTERVAL + 1)
    with caplog.at_level(logging.INFO, logger="pm-test"):
        ns["collect_powermetrics"]()             # respawn
        _run_reader(ns)                          # sample flows again
    assert any("detected again" in r.getMessage() for r in caplog.records)


def test_missing_binary_disables_permanently(clock):
    calls = {"n": 0}

    def popen(cmd, **kw):
        calls["n"] += 1
        raise FileNotFoundError("sudo")

    ns = _harness(clock, popen_factory=popen)
    assert ns["collect_powermetrics"]() == {}
    assert ns["_pm_binary_missing"] is True
    clock.advance(INTERVAL * 10)
    assert ns["collect_powermetrics"]() == {}
    assert calls["n"] == 1                       # never retried


def test_stale_reader_does_not_clobber_a_newer_proc(clock):
    ns = _harness(clock, popen_factory=lambda cmd, **kw: FakePopen([]))
    old = FakePopen([])
    ns["_pm_proc"] = FakePopen([b"x"])           # a newer proc took over
    ns["_pm_latest"].update({"soc_total_w": 9.9})
    ns["_pm_reader_loop"](old)                   # stale reader winds down
    assert ns["_pm_latest"] == {"soc_total_w": 9.9}
    assert ns["_pm_latch"].absent is False


def test_non_darwin_never_runs(clock):
    calls = {"n": 0}

    def popen(cmd, **kw):
        calls["n"] += 1
        return FakePopen([])

    ns = _harness(clock, popen_factory=popen, platform="linux")
    assert ns["collect_powermetrics"]() == {}
    assert calls["n"] == 0
