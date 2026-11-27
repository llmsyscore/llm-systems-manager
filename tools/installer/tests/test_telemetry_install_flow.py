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
    assert "llmsyscore-reports" not in AGENT and "telemetry-receiver" not in AGENT
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
          "Settings › Telemetry & analytics or set install_report = false in llm-systems.toml")


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


def test_agent_rerun_with_an_install_id_reports_update():
    i_check = AGENT.index('test -s "$INSTALL_DIR/state/install-id" 2>/dev/null && _tele_event=update')
    i_write = AGENT.index('_run_as "$USER_ARG" "$_tele_py" -I - "$INSTALL_DIR/state"')
    assert i_check < i_write
    assert '--event "$_tele_event" --channel script --mode 5' in AGENT


def test_agent_exit_hook_reports_the_failed_stage_once():
    body = re.search(r"_agent_tele_on_exit\(\) \{(.*?)\n\}", AGENT, re.S).group(1)
    assert "(( rc != 0 )) || return 0" in body
    assert '_agent_tele_report "failed:$_tele_stage"' in body
    assert "trap _agent_tele_on_exit EXIT" in AGENT
    report = re.search(r"_agent_tele_report\(\) \{(.*?)\n\}", AGENT, re.S).group(1)
    assert report.index("$_tele_sent && return 0") < report.index("_tele_sent=true") < report.index("telemetry-report.sh\" --event")
    for stage in ("files", "runtime", "config", "venv", "sudoers", "service", "finish"):
        assert f"_tele_stage={stage}" in AGENT, stage


def test_agent_report_carries_provider_and_outcome():
    report = re.search(r"_agent_tele_report\(\) \{(.*?)\n\}", AGENT, re.S).group(1)
    assert 'LLMSYS_TELEMETRY_PROVIDER="$_tele_provider"' in report
    assert '--outcome "$1"' in report
    assert AGENT.rstrip().endswith('_agent_tele_report "$_tele_outcome"')


def test_linux_start_without_health_marks_agent_start_failed():
    i_start = AGENT.index('$SUDO systemctl is-active --quiet llm-systems-agent.service')
    block = AGENT[i_start:AGENT.index("  else\n    PLIST_DEST=", i_start)]
    assert "$SUDO systemctl restart llm-systems-agent.service" in block
    assert block.index("restart llm-systems-agent") < block.index("_agent_health_ok")
    assert "_agent_health_ok" in block
    assert '_tele_outcome="failed:agent_start"' in block
    helper = re.search(r"_agent_health_ok\(\) \{(.*?)\n\}", AGENT, re.S).group(1)
    assert "AGENT_BIND_PORT" in helper and "AGENT_BIND_HOST" in helper
    assert "for scheme in http https" in helper


def _agent_report_harness(tmp_path, tail, env=None):
    import subprocess
    funcs = "\n".join(re.search(rf"\n({name}\(\) \{{.*?\n\}})", AGENT, re.S).group(1)
                      for name in ("_agent_tele_report", "_agent_tele_on_exit"))
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "tree"
    (root / "agent").mkdir(parents=True)
    (root / "agent" / "llm-systems-agent.py").write_text('VERSION = "v9"\n')
    (root / "llm-systems-manager" / "backend").mkdir(parents=True)
    (root / "llm-systems-manager" / "backend" / "telemetry.py").write_text("")
    (root / "tools" / "installer").mkdir(parents=True)
    log = tmp_path / "calls.txt"
    (root / "tools" / "installer" / "telemetry-report.sh").write_text(
        f'#!/usr/bin/env bash\necho "$LLMSYS_TELEMETRY_PROVIDER $*" >> "{log}"\n')
    script = f"""set -euo pipefail
_run_as() {{ shift; "$@"; }}
SUDO="" USER_ARG=me SRC_DIR="{root}/agent" INSTALL_DIR="{tmp_path}/inst" VERSION=v0
ENABLE_LLAMA=false ENABLE_LMS=true ENABLE_VLLM=false LLMSYS_TELEMETRY="${{T:-1}}"
_tele_event=update _tele_outcome=ok _tele_stage=files _tele_sent=false
{funcs}
trap _agent_tele_on_exit EXIT
{tail}
"""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **(env or {})})
    return r, (log.read_text().splitlines() if log.exists() else [])


def test_agent_failed_run_sends_one_failed_report(tmp_path):
    r, calls = _agent_report_harness(tmp_path, "_tele_stage=venv\nfalse")
    assert r.returncode == 1
    assert len(calls) == 1
    assert calls[0].startswith("lms --event update --channel script --mode 5 --outcome failed:venv ")


def test_agent_finished_run_sends_one_report_and_no_failure(tmp_path):
    r, calls = _agent_report_harness(tmp_path, '_agent_tele_report "$_tele_outcome"\nexit 0')
    assert r.returncode == 0, r.stderr
    assert len(calls) == 1 and " --outcome ok " in calls[0]


def test_no_runtime_hint_gives_runnable_commands():
    block = AGENT[AGENT.index("Recommended next step: install an inference runtime"):]
    assert "$INSTALL_DIR/src/agent/install/install.sh" not in block
    assert "--manager-url $MANAGER_URL --install-llama --llama-backend vulkan" in block
    assert "--manager-url $MANAGER_URL --install-llama=source --llama-backend cuda" in block
    assert "sudo bash <(curl" not in AGENT
    assert "LMS_ENABLED: true" in block


def test_agent_report_sends_nothing_without_consent_or_for_a_full_install(tmp_path):
    for n, env in enumerate(({"T": "0"}, {"LLMSYS_INSTALL_MODE": "1"})):
        r, calls = _agent_report_harness(tmp_path / str(n), "false", env)
        assert r.returncode == 1 and calls == [], env


def test_agent_ctrl_c_reports_cancelled_and_says_so(tmp_path):
    r, calls = _agent_report_harness(tmp_path, "_tele_stage=venv\nexit 130")
    assert r.returncode == 130
    assert len(calls) == 1 and " --outcome failed:cancelled " in calls[0]
    assert "sending the failure report" in r.stderr


def test_agent_failure_after_the_final_report_sends_nothing_more(tmp_path):
    r, calls = _agent_report_harness(tmp_path, '_agent_tele_report "$_tele_outcome"\nfalse')
    assert r.returncode == 1
    assert len(calls) == 1 and " --outcome ok " in calls[0]
    assert "sending the failure report" not in r.stderr


def test_macos_start_check_uses_the_configured_health_address():
    mac = AGENT[AGENT.index('launchctl load -w "$PLIST_DEST"'):AGENT.index("# 6b. Agent state dir")]
    assert "_agent_health_ok" in mac
    assert "127.0.0.1:8082/health" not in mac
    assert '_tele_outcome="failed:agent_start"' in mac


def test_agent_real_ctrl_c_reports_cancelled(tmp_path):
    import signal
    import subprocess
    import time
    funcs = "\n".join(re.search(rf"\n({name}\(\) \{{.*?\n\}})", AGENT, re.S).group(1)
                      for name in ("_agent_tele_report", "_agent_tele_on_exit"))
    int_trap = re.search(r"\n(trap '_tele_stage=cancelled; exit 130' INT)\n", AGENT).group(1)
    root = tmp_path / "tree"
    (root / "agent").mkdir(parents=True)
    (root / "agent" / "llm-systems-agent.py").write_text('VERSION = "v9"\n')
    (root / "llm-systems-manager" / "backend").mkdir(parents=True)
    (root / "llm-systems-manager" / "backend" / "telemetry.py").write_text("")
    (root / "tools" / "installer").mkdir(parents=True)
    log = tmp_path / "calls.txt"
    (root / "tools" / "installer" / "telemetry-report.sh").write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n')
    ready = tmp_path / "ready"
    script = f"""set -euo pipefail
_run_as() {{ shift; "$@"; }}
SUDO="" USER_ARG=me SRC_DIR="{root}/agent" INSTALL_DIR="{tmp_path}/inst" VERSION=v0
ENABLE_LLAMA=false ENABLE_LMS=false ENABLE_VLLM=false LLMSYS_TELEMETRY=1
_tele_event=install _tele_outcome=ok _tele_stage=venv _tele_sent=false
{funcs}
{int_trap}
trap _agent_tele_on_exit EXIT
touch "{ready}"
for _i in $(seq 1 200); do sleep 0.05; done
"""
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={"PATH": "/usr/bin:/bin"})
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGINT)
    _out, err = proc.communicate(timeout=20)
    assert proc.returncode == 130
    assert "sending the failure report" in err
    assert " --outcome failed:cancelled " in log.read_text()


def test_rerun_that_installed_llama_writes_only_llama_keys(tmp_path):
    import subprocess
    m = re.search(r'elif \[\[ "\$LLAMA_INSTALLED_THIS_RUN" == true \]\]; then.*?<<\'PYEOF\'\n(.*?)\nPYEOF\n', AGENT, re.S)
    code = m.group(1)
    cfg = tmp_path / "agent_config.yaml"
    cfg.write_text('MANAGER_URL: "https://mgr.example:5000"\nLLAMA_ENABLED: false\n# LLAMA_BIN: ""\n'
                   '# LLAMA_LOG_FILE: "/old.log"\nLLAMA_LOG_FILE: "/live.log"\n'
                   '# LLAMA_BUILD_OPTS:\n#   backend: cpu\n#   jobs: 4\nLMS_ENABLED: true\n')
    cfg.chmod(0o600)
    subprocess.run(["python3", "-", str(cfg), "/opt/llama/bin/llama-server", "", "release_binary", "/opt/llama",
                    "vulkan", "", "", "/opt/llama/llama-server.log", ""], input=code, text=True, check=True)
    text = cfg.read_text()
    assert 'MANAGER_URL: "https://mgr.example:5000"' in text and "LMS_ENABLED: true" in text
    assert "LLAMA_ENABLED: true" in text
    assert 'LLAMA_BIN: "/opt/llama/bin/llama-server"' in text
    assert 'LLAMA_BUILD_METHOD: "release_binary"' in text
    assert "LLAMA_BUILD_OPTS:\n  backend: vulkan\n#   jobs: 4" in text
    assert "LLAMA_CONFIG_INI" not in text
    assert '# LLAMA_LOG_FILE: "/old.log"\nLLAMA_LOG_FILE: "/opt/llama/llama-server.log"' in text
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    assert not (tmp_path / "agent_config.yaml.tmp").exists()


def test_rerun_llama_config_write_keeps_a_backup():
    block = AGENT[AGENT.index('elif [[ "$LLAMA_INSTALLED_THIS_RUN" == true ]]; then'):]
    block = block[:block.index("PYEOF")]
    assert '_run_as "$USER_ARG" cp -p "$INSTALL_DIR/agent_config.yaml" "$_cfg_bak"' in block
    assert '_run_as "$USER_ARG" "${PYTHON3:-python3}" -I - "$INSTALL_DIR/agent_config.yaml"' in block
    assert "LLAMA_INSTALLED_THIS_RUN=true" in AGENT


def test_agent_update_detection_runs(tmp_path):
    import subprocess
    line = next(l for l in AGENT.splitlines() if "_tele_event=update" in l and "install-id" in l).strip()
    state = tmp_path / "state"
    state.mkdir()
    script = f'_run_as() {{ shift; "$@"; }}\nINSTALL_DIR="{tmp_path}"\n_tele_event=install\n{line}\necho "$_tele_event"\n'
    first = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip()
    (state / "install-id").write_text("11111111-1111-4111-8111-111111111111\n")
    again = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip()
    assert (first, again) == ("install", "update")


def test_closing_health_command_names_the_configured_port():
    tail = AGENT[AGENT.index("── Agent Installation Complete"):]
    assert "localhost:8082" not in tail
    assert '_yaml_scalar "$INSTALL_DIR/agent_config.yaml" AGENT_BIND_PORT' in tail
    assert "curl -k https://localhost:$_hc_port/health" in tail
