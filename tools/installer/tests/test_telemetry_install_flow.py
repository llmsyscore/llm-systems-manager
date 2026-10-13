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
    i_consent = INSTALL.index("telemetry_consent")
    i_mode5 = INSTALL.index('banner "Mode 5 — Agent installer"')
    assert i_lib < i_consent < i_mode5
    assert INSTALL.count("telemetry_consent\n") == 1


def test_exit_trap_reports_failed_stage():
    assert "_telemetry_on_exit" in INSTALL
    m = re.search(r"_telemetry_on_exit\(\) \{(.*?)\n\}", INSTALL, re.S)
    body = m.group(1)
    assert "failed:" in body and "LLMSYS_STAGE" in body
    assert "telemetry_report" in body
    assert re.search(r"trap '_telemetry_on_exit; rm -f", INSTALL)


def test_install_success_reports_ok():
    tail = INSTALL[INSTALL.index('ok "Installation succeeded'):]
    assert "telemetry_report install script" in INSTALL
    assert "_TELEMETRY_DONE=1" in INSTALL
    assert "failed:health_checks" in tail


def test_mode5_marks_report_done_before_exit():
    block = INSTALL[INSTALL.index('banner "Mode 5 — Agent installer"'):]
    block = block[:block.index("esac")]
    assert block.index("_TELEMETRY_DONE=1") < block.index('exit "$_rc"')


def test_update_merges_keys_without_prompt():
    assert "telemetry_consent" not in UPDATE
    assert "telemetry_report update script" in UPDATE
    assert "read_toml_key install_report" in UPDATE


def test_agent_installer_writes_consent_file():
    assert "telemetry.json" in AGENT
    assert "LLMSYS_TELEMETRY" in AGENT
    assert "telemetry-receiver" not in AGENT
    assert '"endpoint"' not in AGENT
    assert "telemetry-report.sh" in AGENT
