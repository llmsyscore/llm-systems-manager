# agent/tests/test_offer_llama_unit.py
from __future__ import annotations

import re
import subprocess
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"
TEMPLATE = AGENT_DIR / "install" / "llama_server.service.tmpl"


def _extract_func(name: str) -> str:
    text = INSTALL_SH.read_text()
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from install.sh"
    return m.group(0)


def _render(bin_: str, user: str, cfg: str, log: str) -> str:
    func = _extract_func("_render_llama_unit")
    script = f'set -euo pipefail\nSRC_DIR={AGENT_DIR}\n{func}\n_render_llama_unit "$1" "$2" "$3" "$4"\n'
    out = subprocess.run(
        ["bash", "-c", script, "bash", bin_, user, cfg, log],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def _exec_line(unit_text: str) -> str:
    return next(l for l in unit_text.splitlines() if l.startswith("ExecStart="))


def test_template_drops_add_model_flags_line():
    text = TEMPLATE.read_text()
    assert "Add model flags before enabling" not in text
    assert "__EXTRA_ARGS__" in text


def test_render_injects_preset_log_and_perf():
    out = _render("/usr/local/llama-server/llama-server", "llmuser",
                  "/etc/llama/models.ini", "/var/log/llama-server.log")
    line = _exec_line(out)
    assert "--metrics" in line
    assert '--models-preset "/etc/llama/models.ini"' in line
    assert '--log-file "/var/log/llama-server.log"' in line
    assert "--perf" in line
    assert "User=llmuser" in out
    assert "Add model flags before enabling" not in out


def test_render_omits_empty_preset_and_log_keeps_perf():
    out = _render("/usr/local/llama-server/llama-server", "llmuser", "", "")
    line = _exec_line(out)
    assert "--perf" in line
    assert "--models-preset" not in line
    assert "--log-file" not in line
    assert "  " not in line.replace("ExecStart=", "")


def test_render_survives_sed_metacharacters_in_paths():
    # | aborts a sed s|||; & / \ silently corrupt a sed replacement — bash
    # substitution must reproduce these paths verbatim instead.
    cfg = "/data/a&b|c/models.ini"
    log = "/var/log/x&y.log"
    out = _render("/opt/llama-server", "llmuser", cfg, log)
    line = _exec_line(out)
    assert f'--models-preset "{cfg}"' in line
    assert f'--log-file "{log}"' in line
    assert "__EXTRA_ARGS__" not in out


def test_render_quotes_paths_with_spaces():
    cfg = "/etc/llama/my models.ini"
    out = _render("/opt/llama-server", "llmuser", cfg, "")
    line = _exec_line(out)
    assert f'--models-preset "{cfg}"' in line
