# agent/tests/test_config_reconcile.py
"""Exercises the agent_config.yaml reconcile (the embedded Python heredoc in
install.sh's --update path) by extracting it and running it on fixtures."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_INSTALL_SH = _AGENT_ROOT / "install" / "install.sh"


def _extract_reconcile() -> str:
    lines = _INSTALL_SH.read_text().splitlines()
    blocks, start = [], None
    for idx, ln in enumerate(lines):
        if start is None:
            if ln.rstrip().endswith("<<'PYEOF'"):
                start = idx + 1
        elif ln.strip() == "PYEOF":
            blocks.append("\n".join(lines[start:idx]))
            start = None
    for b in blocks:
        if "live_by_key" in b:           # the reconcile heredoc
            return b
    raise AssertionError("reconcile PYEOF block not found in install.sh")


def _run(tmp_path: Path, live: str, example: str):
    """Returns (reconciled config text, reconcile stdout)."""
    script = tmp_path / "reconcile.py"
    script.write_text(_extract_reconcile())
    live_f = tmp_path / "agent_config.yaml"
    ex_f = tmp_path / "agent_config.yaml.example"
    live_f.write_text(live)
    ex_f.write_text(example)
    import getpass
    user = getpass.getuser()
    r = subprocess.run([sys.executable, str(script), str(live_f), str(ex_f), user, user],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return live_f.read_text(), r.stdout


_OLD = """\
AGENT_OS: linux
# LLAMA_BUILD_METHOD:  ""    # custom_script|source|release_binary
# LLAMA_BUILD_OPTS:          # per-method knobs
#   git_ref: master          #   source
#   backend: cpu             #   source
"""

_NEW = """\
AGENT_OS: linux
# LLAMA_BUILD_METHOD:  ""    # custom_script|source|release_binary
# LLAMA_BUILD_OPTS:          # per-method knobs
#   git_ref: master          #   source
#   backend: cpu             #   source
#   install_in_place: false  #   in-place upgrade
#   backup_retain: 2         #   how many backups to keep
"""


def test_reconcile_propagates_new_subkeys_into_untouched_block(tmp_path):
    cfg, stdout = _run(tmp_path, _OLD, _NEW)
    assert "install_in_place" in cfg
    assert "backup_retain" in cfg
    # and the run reports what it surfaced (was previously silent)
    assert "NEW CONFIG OPTIONS" in stdout
    assert "LLAMA_BUILD_OPTS.install_in_place" in stdout
    assert "LLAMA_BUILD_OPTS.backup_retain" in stdout


def test_reconcile_preserves_operator_activated_block(tmp_path):
    live = "AGENT_OS: linux\nLLAMA_BUILD_OPTS:\n  backend: cuda\n"
    cfg, stdout = _run(tmp_path, live, _NEW)
    # operator's activated values are never clobbered
    assert "backend: cuda" in cfg
    # the new options are NOT injected into a customized block, but are flagged
    assert "LLAMA_BUILD_OPTS.install_in_place" in stdout
