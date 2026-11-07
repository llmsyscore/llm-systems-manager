# agent/tests/test_install_detect_llama_wiring.py
"""install.sh wiring of detect_llama.py: overrides, defaults, agent-user proposal."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"
FUNCS = ("_run_detect_llama_py", "_llama_summary_line",
         "_llama_notty_overrides", "_propose_agent_user")


def _func(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", INSTALL_SH.read_text(), re.S | re.M)
    assert m, f"{name} not found in install.sh"
    return m.group(0)


def _run(tmp_path: Path, fake_out: str, body: str) -> subprocess.CompletedProcess:
    (tmp_path / "detect_llama.py").write_text(f"print({fake_out!r})\n")
    script = "\n".join([
        "set -euo pipefail",
        f"TMPL_DIR={tmp_path}",
        "_user_exists() { [[ \"$1\" == llama || \"$1\" == llmsys || \"$1\" == root ]]; }",
        *(_func(f) for f in FUNCS),
        body,
    ])
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=30)


FOUND = "\n".join([
    "LLAMA_DETECTED_FOUND=true", "LLAMA_DETECTED_PORT=9931",
    "LLAMA_DETECTED_HOST=0.0.0.0", "LLAMA_DETECTED_UNIT=llama-server.service",
    "LLAMA_DETECTED_PRESETS_FILE=/srv/presets.ini", "LLAMA_DETECTED_MODE=router",
    "LLAMA_DETECTED_UNIT_USER=llama", "LLAMA_DETECTED_PROCESS_USER=llama",
    "LLAMA_DETECTED_BINARY=/opt/ls/llama-server",
])

BODY = """
detected_port=""; detected_host=""; detected_config=""; detected_bin=""
USER_ARG=llmsys
_run_detect_llama_py "$detected_port"
detected_port="${LLAMA_DETECTED_PORT:-$detected_port}"
detected_host="${LLAMA_DETECTED_HOST:-$detected_host}"
detected_config="${LLAMA_DETECTED_PRESETS_FILE:-$detected_config}"
_llama_summary_line "${LLAMA_DETECTED_BINARY:-$detected_bin}" "$detected_port" "$detected_config"
_llama_notty_overrides "$detected_host" "$detected_port" "$detected_config"
_propose_agent_user
echo "URL=${LLAMA_API_URL_OVERRIDE:-} UNIT=${LLAMA_SYSTEMD_UNIT_OVERRIDE:-} USER=$USER_ARG"
"""


def test_detected_values_become_overrides(tmp_path):
    r = _run(tmp_path, FOUND, BODY)
    assert r.returncode == 0, r.stderr
    assert ("Found llama-server: /opt/ls/llama-server, port 9931, unit llama-server.service, "
            "presets /srv/presets.ini, mode many models, runs as llama") in r.stdout
    assert "URL=http://127.0.0.1:9931 UNIT=llama-server.service USER=llama" in r.stdout
    assert "using default" not in r.stdout
    assert "llama-server runs as llama; running the agent as the same user" in r.stdout
    assert "ⓘ running the agent as llama (llama-server's user)" in r.stdout


def test_nothing_detected_keeps_defaults(tmp_path):
    r = _run(tmp_path, "LLAMA_DETECTED_FOUND=false", BODY)
    assert r.returncode == 0, r.stderr
    for key in ("LLAMA_API_URL", "LLAMA_SYSTEMD_UNIT", "LLAMA_CONFIG_INI"):
        assert f"ⓘ using default {key}" in r.stdout
    assert "URL= UNIT= USER=llmsys" in r.stdout


def test_root_is_never_proposed(tmp_path):
    out = FOUND.replace("UNIT_USER=llama", "UNIT_USER=root").replace(
        "PROCESS_USER=llama", "PROCESS_USER=root")
    r = _run(tmp_path, out, BODY)
    assert r.returncode == 0, r.stderr
    assert "USER=llmsys" in r.stdout
    assert "runs as root" in r.stdout
    assert "running the agent as the same user" not in r.stdout


def test_detect_llama_copied_to_install_dir_in_both_paths():
    text = INSTALL_SH.read_text()
    line = 'cp "$TMPL_DIR/detect_llama.py" "$INSTALL_DIR/install/detect_llama.py"'
    assert text.count(line) == 2
    assert text.count('chmod 0644 "$INSTALL_DIR/install/detect_llama.py"') == 2
    assert text.count('chmod 0755 "$INSTALL_DIR/install"') == 2


def test_summary_line_printed_only_when_something_was_found():
    text = INSTALL_SH.read_text()
    assert ('if [[ "${LLAMA_DETECTED_FOUND:-}" == true || -n "$detected_bin" ]]; then\n'
            '    _llama_summary_line "$detected_bin"') in text
