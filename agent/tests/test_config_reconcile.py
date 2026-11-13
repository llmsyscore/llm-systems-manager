# agent/tests/test_config_reconcile.py
"""Exercises the agent_config.yaml reconcile (the embedded Python heredoc in
install.sh's --update path) by extracting it and running it on fixtures."""
from __future__ import annotations

import re
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


def test_reconcile_adds_missing_subkeys_to_activated_block(tmp_path):
    live = "AGENT_OS: linux\nLLAMA_BUILD_OPTS:\n  backend: cuda\n"
    cfg, stdout = _run(tmp_path, live, _NEW)
    # operator's activated value is never clobbered
    assert "backend: cuda" in cfg
    # sub-keys missing entirely are now added (commented, inert)
    assert "install_in_place" in cfg
    assert "backup_retain" in cfg
    assert "LLAMA_BUILD_OPTS.install_in_place" in stdout


def test_reconcile_adds_only_missing_subkeys_skips_existing(tmp_path):
    # parent uncommented with git_ref + backend uncommented and install_in_place
    # present-but-commented; only the truly-missing sub-keys should be added
    live = ("AGENT_OS: linux\n"
            "LLAMA_BUILD_OPTS:\n"
            "  git_ref: master\n"
            "  backend: cuda\n"
            "#   install_in_place: true\n")
    example = ("AGENT_OS: linux\n"
               "# LLAMA_BUILD_OPTS:          # per-method knobs\n"
               "#   git_ref: master          #   source\n"
               "#   backend: cpu             #   source\n"
               "#   install_in_place: false  #   in-place upgrade\n"
               "#   backup_retain: 2         #   how many backups to keep\n")
    cfg, _ = _run(tmp_path, live, example)
    assert "git_ref: master" in cfg                  # operator values preserved
    assert "backend: cuda" in cfg
    assert "backup_retain" in cfg                    # missing entirely -> added
    assert cfg.count("install_in_place") == 1        # present commented -> not duplicated
    assert cfg.count("git_ref") == 1                 # present uncommented -> not re-added
    assert cfg.count("backend") == 1


def test_reconcile_is_idempotent_on_second_run(tmp_path):
    live = "AGENT_OS: linux\nLLAMA_BUILD_OPTS:\n  backend: cuda\n"
    cfg1, _ = _run(tmp_path, live, _NEW)
    cfg2, stdout2 = _run(tmp_path, cfg1, _NEW)
    assert cfg2 == cfg1                       # second pass changes nothing
    assert "NEW CONFIG OPTIONS" not in stdout2


def test_reconcile_does_not_corrupt_activated_list_block(tmp_path):
    # operator activated a list-style block; example adds a per-item property —
    # it must NOT be appended as orphaned lines, only flagged for manual add
    live = ("AGENT_OS: linux\n"
            "PROCESS_WATCHLIST:\n"
            "  - name: llama-server\n"
            "    match: llama-server\n")
    example = ("AGENT_OS: linux\n"
               "# PROCESS_WATCHLIST:\n"
               "#   - name: llama-server\n"
               "#     match: llama-server\n"
               "#     enabled: true\n")
    cfg, stdout = _run(tmp_path, live, example)
    assert "  - name: llama-server\n    match: llama-server\n" in cfg   # preserved verbatim
    assert "enabled" not in cfg                                         # not injected
    assert "PROCESS_WATCHLIST.enabled" in stdout                        # flagged instead


def test_llama_cache_and_models_dir_keys_documented_and_default_blank():
    example = (_AGENT_ROOT / "agent_config.yaml.example").read_text()
    src = (_AGENT_ROOT / "llm-systems-agent.py").read_text()
    for key in ("LLAMA_CACHE_DIR", "LLAMA_MODELS_DIR"):
        assert re.search(rf'^# {key}:\s+""', example, re.M), key
        assert re.search(rf'^\s+{key}: str = ""$', src, re.M), key


def test_llama_cache_and_models_dir_values_preserved_by_reconcile(tmp_path):
    live = 'AGENT_OS: linux\nLLAMA_CACHE_DIR: "/x/cache"\nLLAMA_MODELS_DIR: "/x/models"\n'
    out, _ = _run(tmp_path, live, (_AGENT_ROOT / "agent_config.yaml.example").read_text())
    assert re.search(r'^LLAMA_CACHE_DIR:\s+"/x/cache"', out, re.M)
    assert re.search(r'^LLAMA_MODELS_DIR:\s+"/x/models"', out, re.M)


def test_default_lms_api_url_is_1234():
    src = (_AGENT_ROOT / "llm-systems-agent.py").read_text()
    assert re.search(r'^\s+LMS_API_URL: str = "http://localhost:1234"$', src, re.M)
