"""#763: skipping the management_token paste on a manager-only install must
say what it costs (Admin → Settings) and print the two-host fix at the end."""
import re
import subprocess
from pathlib import Path

BOOTSTRAP = Path(__file__).resolve().parents[1] / "install-config-bootstrap.sh"
TEXT = BOOTSTRAP.read_text()


def test_prompt_states_the_settings_tab_consequence():
    prompt = TEXT.index('read -rp "  AE management_token (or Enter to skip): "')
    before = TEXT[prompt - 600:prompt]
    assert "Admin → Settings" in before
    assert "SAME" in before and "both hosts" in before


def test_skip_path_is_not_worded_as_conditional():
    assert "if the remote engine enforces a management_token" not in TEXT
    m = re.search(r"MGMT_SKIPPED=1\n(.*?)\n  fi\n", TEXT, re.DOTALL)
    assert m, "MGMT_SKIPPED=1 branch missing"
    assert "403" in m.group(1) and "Admin → Settings" in m.group(1)


def test_end_of_bootstrap_todo_block_lists_both_hosts():
    m = re.search(r"if \(\( MGMT_SKIPPED \)\); then\n  cat <<EOF\n(.*?)\nEOF\nfi",
                  TEXT, re.DOTALL)
    assert m, "MGMT_SKIPPED todo block missing"
    block = m.group(1)
    assert "management_token" in block and "BOTH hosts" in block
    assert "systemctl restart llm-systems-manager" in block
    assert "systemctl restart llm-systems-alarm-engine" in block
    assert "openssl rand -hex 32" in block
    assert TEXT.rstrip().endswith("EOF\nfi"), "todo block must be the script's last output"


def test_mode4_banner_names_the_settings_tab():
    m = re.search(r"Save these tokens.*?\nEOF\n", TEXT, re.DOTALL)
    assert m and "Admin → Settings" in m.group(0)


def test_script_parses():
    r = subprocess.run(["bash", "-n", str(BOOTSTRAP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
