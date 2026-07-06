# agent/tests/test_svcconfig_wrapper.py
"""#287: llm-svcconfig-apply rewrites ONLY the unit's ExecStart line from
validated stdin tokens, so the agent needs no grant to write unit content."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
TMPL = AGENT_DIR / "install" / "llm-svcconfig-apply.sh.tmpl"

UNIT_TEXT = """[Unit]
Description=llama.cpp server (llama-server)

[Service]
Type=simple
User=llmagent
Environment=CUDA_VISIBLE_DEVICES=0
ExecStart=/old/bin --metrics --host 127.0.0.1 --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


def _shims(binroot: Path):
    (binroot / "systemctl").write_text("#!/usr/bin/env bash\nexit 0\n")
    # install shim strips owner/mode flags so the test can run non-root
    (binroot / "install").write_text(
        "#!/usr/bin/env bash\nargs=()\n"
        "while [[ $# -gt 0 ]]; do case \"$1\" in -o|-g|-m) shift 2;; *) args+=(\"$1\"); shift;; esac; done\n"
        "cp \"${args[0]}\" \"${args[1]}\"\n")
    for f in ("systemctl", "install"):
        os.chmod(binroot / f, 0o755)


def _run(tmp_path, stdin_lines, binary):
    unit = tmp_path / "llama_server.service"
    unit.write_text(UNIT_TEXT)
    binroot = tmp_path / "bin"; binroot.mkdir()
    _shims(binroot)
    script = TMPL.read_text().replace("__UNIT_PATH__", str(unit)) \
                             .replace("/usr/bin/systemctl", "systemctl")
    sh = tmp_path / "wrapper.sh"; sh.write_text(script); os.chmod(sh, 0o755)
    payload = "\n".join([binary, *stdin_lines]) + "\n"
    env = dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}")
    p = subprocess.run(["bash", str(sh)], input=payload, capture_output=True,
                       text=True, env=env)
    return p, unit


def _fake_bin(tmp_path) -> str:
    b = tmp_path / "llama-server"; b.write_text("#!/bin/sh\n"); os.chmod(b, 0o755)
    return str(b)


def _exec_line(unit: Path) -> str:
    return next(l for l in unit.read_text().splitlines() if l.startswith("ExecStart="))


def test_swaps_only_execstart_and_preserves_rest(tmp_path):
    b = _fake_bin(tmp_path)
    p, unit = _run(tmp_path, ["--metrics", "--host", "127.0.0.1", "--port", "9090"], b)
    assert p.returncode == 0, p.stderr
    text = unit.read_text()
    assert f"ExecStart={b} --metrics --host 127.0.0.1 --port 9090" in text
    assert "User=llmagent" in text
    assert "Environment=CUDA_VISIBLE_DEVICES=0" in text
    assert "/old/bin" not in text


def test_quotes_value_with_space(tmp_path):
    b = _fake_bin(tmp_path)
    p, unit = _run(tmp_path, ["--chat-template", "a b c"], b)
    assert p.returncode == 0, p.stderr
    assert '--chat-template "a b c"' in _exec_line(unit)


def test_rejects_relative_binary(tmp_path):
    p, unit = _run(tmp_path, ["--metrics"], "relative/bin")
    assert p.returncode != 0
    assert "absolute path" in p.stderr
    assert "/old/bin" in unit.read_text()  # untouched


def test_rejects_control_char_in_arg(tmp_path):
    b = _fake_bin(tmp_path)
    p, unit = _run(tmp_path, ["--host", "1.1.1.1\tUser=root"], b)
    assert p.returncode != 0
    assert "control character" in p.stderr
    assert "/old/bin" in unit.read_text()


def test_rejects_binary_with_space(tmp_path):
    p, _ = _run(tmp_path, ["--metrics"], "/bin/sh -c evil")
    assert p.returncode != 0
    assert "disallowed characters" in p.stderr


def test_errors_when_no_execstart_line(tmp_path):
    unit = tmp_path / "llama_server.service"
    unit.write_text("[Service]\nUser=x\n")
    binroot = tmp_path / "bin"; binroot.mkdir(); _shims(binroot)
    b = _fake_bin(tmp_path)
    script = TMPL.read_text().replace("__UNIT_PATH__", str(unit)) \
                             .replace("/usr/bin/systemctl", "systemctl")
    sh = tmp_path / "w.sh"; sh.write_text(script); os.chmod(sh, 0o755)
    env = dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}")
    p = subprocess.run(["bash", str(sh)], input=f"{b}\n--metrics\n",
                       capture_output=True, text=True, env=env)
    assert p.returncode != 0
    assert "no ExecStart" in p.stderr
