# agent/tests/test_update_installs_sudoers_wrapper.py
"""Regression: a root --update must (re)install the sudoers file + svcconfig
helper. The --update block exits before the fresh-install sudoers step, so the
install lives in a shared _apply_sudoers_and_wrapper() called from both paths."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"


def _extract_func(name: str) -> str:
    lines = INSTALL_SH.read_text().splitlines()
    s = next(i for i, l in enumerate(lines) if l.startswith(name + "()"))
    e = next(i for i in range(s + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[s:e + 1])


def test_update_block_calls_installer_for_root_only():
    text = INSTALL_SH.read_text()
    # The root --update path invokes the shared installer, gated off self-update.
    assert "! $FROM_SELF_UPDATE \\\n       && [[ -f /etc/sudoers.d/llm-systems-agent ]]" in text
    # Both the update block and the fresh-install block go through the function.
    assert text.count("_apply_sudoers_and_wrapper || exit 1") == 2


def test_apply_installs_both_files_with_resolved_unit(tmp_path):
    fn = _extract_func("_apply_sudoers_and_wrapper")
    rl = _extract_func("_resolved_llama_unit")
    ys = _extract_func("_yaml_scalar")

    binroot = tmp_path / "bin"; binroot.mkdir()
    log = tmp_path / "install.log"
    (binroot / "visudo").write_text("#!/bin/sh\nexit 0\n")
    # install stub: log "<dest>\n<content>" for file installs; handle -d dirs.
    (binroot / "install").write_text(
        "#!/bin/sh\n"
        'src=""; dst=""\n'
        'while [ $# -gt 0 ]; do case "$1" in\n'
        '  -m|-o|-g) shift 2;;\n'
        '  -d) exit 0;;\n'
        f'  *) if [ -z "$src" ]; then src="$1"; else dst="$1"; fi; shift;; esac; done\n'
        f'{{ echo "DEST=$dst"; cat "$src"; echo "---END---"; }} >> "{log}"\n')
    for f in ("visudo", "install"):
        os.chmod(binroot / f, 0o755)

    tmpl = tmp_path / "tmpl"; tmpl.mkdir()
    (tmpl / "llm-systems-agent.sudoers.tmpl").write_text(
        "${AGENT_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart ${LLAMA_UNIT}\n")
    (tmpl / "llm-svcconfig-apply.sh.tmpl").write_text("UNIT_PATH='__UNIT_PATH__'\n")

    script = (
        "_ok(){ :; }\n"
        'SUDO=""\n'
        'USER_ARG="llmagent"\n'
        f'TMPL_DIR="{tmpl}"\n'
        'INSTALL_DIR="/nonexistent"\n'
        'LLAMA_SYSTEMD_UNIT_OVERRIDE="my-llama.service"\n'
        f"{ys}\n{rl}\n{fn}\n_apply_sudoers_and_wrapper\n"
    )
    env = dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    out = log.read_text()
    assert "DEST=/etc/sudoers.d/llm-systems-agent" in out
    assert "systemctl restart my-llama.service" in out          # sudoers uses resolved unit
    assert "DEST=/usr/local/sbin/llm-svcconfig-apply" in out
    assert "UNIT_PATH='/etc/systemd/system/my-llama.service'" in out  # wrapper baked unit
