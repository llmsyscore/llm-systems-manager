"""#828: a Mode 3 bootstrap cannot finish without a management_token (paste,
'new', a previous config's value, or LLMSYS_CFG_AE_MGMT_TOKEN); no skip."""
import re
import subprocess
from pathlib import Path

BOOTSTRAP = Path(__file__).resolve().parents[1] / "install-config-bootstrap.sh"
TEXT = BOOTSTRAP.read_text()


def test_management_prompt_has_no_skip_but_ingest_keeps_it():
    assert 'read -rp "  AE ingest_token (or Enter to skip): "' in TEXT
    assert "AE management_token (or Enter to skip)" not in TEXT
    assert 'read -rp "  AE management_token ($_mgmt_hint): "' in TEXT
    assert '_mgmt_hint="paste, or \'new\' to generate"' in TEXT


def test_mint_helper_is_the_only_generator():
    m = re.search(r"mint_mgmt_token\(\) \{\n(.*?)\n\}", TEXT, re.DOTALL)
    assert m, "mint_mgmt_token helper missing"
    assert "openssl rand -hex 32" in m.group(1) and "MGMT_GENERATED=1" in m.group(1)
    assert TEXT.count("MGMT_GENERATED=1") == 1


def test_prompt_loops_until_a_value_or_new_and_keeps_a_previous_token():
    start = TEXT.index('read -rp "  AE management_token ($_mgmt_hint): "')
    loop = TEXT[TEXT.rindex("while :; do", 0, start):TEXT.index("done", start)]
    assert "continue" in loop and "required on a split install" in loop
    assert "mint_mgmt_token" in loop and "validate_influx_token" in loop
    assert 'MGR_MGMT_TOKEN_PASTE="$PREV_MGMT_TOKEN"' in loop
    assert '"generate"' not in loop
    intro = TEXT[TEXT.rindex("\n    echo\n", 0, start):start]
    assert "REQUIRED" in intro and "Admin → Settings" in intro and "both hosts" in intro


def test_previous_token_is_read_before_the_template_overwrites_the_config():
    read_at = TEXT.index('PREV_MGMT_TOKEN="$($SUDO sed')
    assert read_at < TEXT.index('$SUDO cp -a "$EXAMPLE" "$REAL"')
    assert 'REPLACE_ME" ]] && PREV_MGMT_TOKEN=""' in TEXT


def test_non_tty_mode3_takes_env_then_previous_then_mints():
    m = re.search(r'if \(\( HAS_MGR && ! HAS_AE \)\); then\n    ALARM_ENGINE_URL="http://\$\{DETECTED_IP\}:8081"(.*?)\n  elif',
                  TEXT, re.DOTALL)
    assert m, "non-TTY mode-3 block missing"
    block = m.group(1)
    env = block.index("LLMSYS_CFG_AE_MGMT_TOKEN")
    prev = block.index('MGR_MGMT_TOKEN_PASTE="$PREV_MGMT_TOKEN"')
    mint = block.index("mint_mgmt_token")
    assert env < prev < mint
    assert "validate_influx_token" in block


def test_skip_path_is_gone():
    assert "MGMT_SKIPPED" not in TEXT
    assert "no management_token entered" not in TEXT
    assert "no management_token set" not in TEXT


def test_generated_path_warns_engine_is_open():
    m = re.search(r"if \(\( MGMT_GENERATED \)\); then\n(.*?)\n  else\n", TEXT, re.DOTALL)
    assert m, "MGMT_GENERATED warn branch missing"
    assert "open" in m.group(1) and "403" in m.group(1) and "alarm-engine host" in m.group(1)


def test_end_of_bootstrap_block_prints_the_token_and_engine_steps():
    m = re.search(r"if \(\( MGMT_GENERATED \)\); then\n  cat <<EOF\n(.*?)\nEOF\nfi",
                  TEXT, re.DOTALL)
    assert m, "MGMT_GENERATED todo block missing"
    block = m.group(1)
    assert "${MGR_MGMT_TOKEN_PASTE}" in block
    assert "BOTH hosts" in block and "OPEN" in block
    assert "systemctl restart llm-systems-alarm-engine" in block
    assert "already running" in block and "systemctl restart llm-systems-manager" in block
    assert "ALARM ENGINE AUTH" in block and "auth open" in block
    assert TEXT.rstrip().endswith("EOF\nfi"), "todo block must be the script's last output"


def test_mode4_banner_names_the_new_prompt_and_settings_tab():
    m = re.search(r"Save these tokens.*?\nEOF\n", TEXT, re.DOTALL)
    assert m and "Admin → Settings" in m.group(0)
    assert "AE management_token (paste, or 'new' to generate):" in m.group(0)
    assert "cannot be skipped" in m.group(0)


def test_script_parses():
    r = subprocess.run(["bash", "-n", str(BOOTSTRAP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
