"""install.sh / update.sh / agent install.sh: consent wiring, EXIT-trap failure report, no re-prompt."""
import re
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1]
REPO = INSTALLER.parents[1]
INSTALL = (INSTALLER / "install.sh").read_text()
UPDATE = (INSTALLER / "update.sh").read_text()
AGENT = (REPO / "agent" / "install" / "install.sh").read_text()


def test_install_asks_once_after_lib_common_and_before_mode5():
    i_lib = INSTALL.index('. "$REPO_SRC/tools/installer/lib-common.sh"')
    i_consent = INSTALL.index("\n  telemetry_consent\n")
    i_mode5 = INSTALL.index('banner "Mode 5 — Agent installer"')
    assert i_lib < i_consent < i_mode5
    assert INSTALL.count("telemetry_consent\n") == 1


def test_exit_trap_reports_failed_stage():
    assert "_telemetry_on_exit" in INSTALL
    m = re.search(r"_telemetry_on_exit\(\) \{(.*?)\n\}", INSTALL, re.S)
    body = m.group(1)
    assert "failed:" in body and "LLMSYS_STAGE" in body
    assert "telemetry_report_as_user" in body
    assert re.search(r"trap '_telemetry_on_exit; rm -f", INSTALL)


def test_install_success_reports_ok():
    tail = INSTALL[INSTALL.index('ok "Installation succeeded'):]
    assert 'telemetry_report_as_user "$LLMSYS_RUN_USER" install script' in INSTALL
    assert "_TELEMETRY_DONE=1" in INSTALL
    assert "failed:health_checks" in tail


def test_mode5_marks_report_done_before_exit():
    block = INSTALL[INSTALL.index('banner "Mode 5 — Agent installer"'):]
    block = block[:block.index("esac")]
    assert block.index("_TELEMETRY_DONE=1") < block.index('exit "$_rc"')


def test_update_merges_keys_without_prompt():
    assert UPDATE.count("telemetry_consent") == 1
    assert 'telemetry_report_as_user "$LLMSYS_RUN_USER" update script' in UPDATE
    assert "read_toml_key install_report" in UPDATE


def test_agent_installer_writes_consent_file():
    assert "telemetry.json" in AGENT
    assert "LLMSYS_TELEMETRY" in AGENT
    assert "telemetry-receiver" not in AGENT
    assert '"endpoint"' not in AGENT
    assert "telemetry-report.sh" in AGENT


def test_update_skips_under_ci():
    line = next(l for l in UPDATE.splitlines() if "read_toml_key install_report" in l)
    assert '-z "${CI:-}"' in line


def test_root_callers_send_as_the_run_user_and_never_chown():
    for text in (INSTALL, UPDATE):
        assert "telemetry_chown" not in text
        assert re.search(r"\btelemetry_report (install|update) ", text) is None
    assert "chown -h" not in AGENT
    assert '_run_as "$USER_ARG" "$_tele_py" -I - "$INSTALL_DIR/state"' in AGENT
    assert '_run_as "$USER_ARG" env LLMSYS_TELEMETRY=1' in AGENT


def test_exit_hook_needs_existing_data_dir():
    body = re.search(r"_telemetry_on_exit\(\) \{(.*?)\n\}", INSTALL, re.S).group(1)
    assert body.index('[[ -d "$d" && ! -L "$d" ]] || return 0') < body.index("telemetry_report_as_user")


def test_final_block_creates_missing_data_dir_before_report():
    i_ok = INSTALL.index('telemetry_report_as_user "$LLMSYS_RUN_USER" install script "$MODE" ok')
    head = INSTALL[INSTALL.rindex("_TELEMETRY_DONE=1", 0, i_ok):i_ok]
    assert '[[ "$MODE" != "6" && ! -e "$LLMSYS_INSTALL_DIR/data" ]]' in head
    assert ('$SUDO install -d -m 0750 -o "$LLMSYS_RUN_USER" -g "$LLMSYS_RUN_GROUP" "$LLMSYS_INSTALL_DIR/data"'
            ' 2>/dev/null || true') in head


NOTICE = ("Anonymous install and crash reports are on after this upgrade; turn them off in "
          "Settings › Usage reports or set install_report = false in llm-systems.toml")


def test_update_asks_only_when_the_section_is_new_and_a_human_is_present():
    i_check = UPDATE.index("grep -q '^\\[manager.telemetry\\]' \"$_live_toml\"")
    i_consent = UPDATE.index("telemetry_consent")
    guard = UPDATE[UPDATE.rindex("if [[", 0, i_consent):i_consent]
    assert "(( ! ASSUME_YES ))" in guard
    assert i_check < i_consent < UPDATE.index("toml_reconcile.py\" merge")
    assert "-t 0" in guard
    assert NOTICE in UPDATE
    assert 'telemetry_write_answers "$_live_toml"' in UPDATE


def test_install_sh_revision_is_bumped():
    rev = int(re.search(r"^_INSTALL_SH_REVISION=(\d+)$", INSTALL, re.M).group(1))
    assert rev >= 20261014002


def test_agent_stages_a_readable_root_for_the_report():
    block = AGENT[AGENT.index("# Agent-only install report"):]
    assert '_run_as "$USER_ARG" test -r "$_tele_root/llm-systems-manager/backend/telemetry.py"' in block
    assert "mktemp -d /tmp/llmsys-telemetry.XXXXXX" in block
    assert '--root "$_tele_root"' in block
    assert '--version "$_tele_ver"' in block
    assert """_tele_ver="$(sed -nE 's/^VERSION = "(.*)"/\\1/p' "$SRC_DIR/llm-systems-agent.py" | head -1)\"""" in block
