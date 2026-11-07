"""#1419: the server log falls back to the unit's journal when no log file is configured."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_llama_unit_info as base  # noqa: E402

llama = base.llama
env = base.env


@pytest.fixture
def logenv(env, monkeypatch):
    env.LLAMA_SYSTEMD_UNIT = "llama_server.service"
    env.LLAMA_LOG_FILE = ""
    monkeypatch.setattr(llama, "_detected", {"unit": "llama-server.service"})
    return env


def test_log_source_tails_existing_log_file(logenv, tmp_path):
    f = tmp_path / "llama-server.log"
    f.write_text("x\n")
    logenv.LLAMA_LOG_FILE = str(f)
    assert llama._llama_log_source() == ["tail", "-n", "0", "-F", str(f)]


@pytest.mark.parametrize("log_file", ["", "/nonexistent/llama-server.log"])
def test_log_source_uses_journal_of_effective_unit(logenv, log_file):
    logenv.LLAMA_LOG_FILE = log_file
    assert llama._llama_log_source() == ["journalctl", "-f", "-n", "0", "-u", "llama-server.service", "-o", "cat"]


def test_log_tail_reads_journal_when_no_log_file(logenv, monkeypatch):
    calls = []
    ignored = llama._LLAMA_LOG_IGNORE[0] if llama._LLAMA_LOG_IGNORE else None

    def run(cmd, **kw):
        calls.append((list(cmd), kw.get("timeout")))
        lines = ["first", f"noise {ignored}" if ignored else "", "second", ""]
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(lines), stderr="")
    monkeypatch.setattr(llama.subprocess, "run", run)
    out = llama.llama_log_tail()
    assert out == {"ok": True, "lines": ["first", "second"]}
    assert calls == [(["journalctl", "-u", "llama-server.service", "-n", "200", "-o", "cat", "--no-pager"], 5)]


def test_log_tail_journal_failure_returns_hint(logenv, monkeypatch):
    def run(cmd, **kw):
        raise FileNotFoundError("journalctl")
    monkeypatch.setattr(llama.subprocess, "run", run)
    out = llama.llama_log_tail()
    assert out == {"ok": True, "lines": [
        "the agent cannot read the server log; run: journalctl -u llama-server.service -n 50"]}


def test_log_stream_publishes_hint_when_journal_unreadable(logenv, monkeypatch):
    published, pumped = [], []
    monkeypatch.setattr(llama.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="denied"))
    monkeypatch.setattr(llama._log_state, "publish", lambda line: published.append(line))
    monkeypatch.setattr(llama._log_state, "pump", lambda argv, should_keep=None: pumped.append(argv))
    llama._llama_log_streamer()
    assert published == ["the agent cannot read the server log; run: journalctl -u llama-server.service -n 50"]
    assert pumped and pumped[0][0] == "journalctl"


def test_log_stream_no_hint_when_journal_readable(logenv, monkeypatch):
    published = []
    monkeypatch.setattr(llama.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="x", stderr=""))
    monkeypatch.setattr(llama._log_state, "publish", lambda line: published.append(line))
    monkeypatch.setattr(llama._log_state, "pump", lambda argv, should_keep=None: None)
    llama._llama_log_streamer()
    assert published == []
