"""Packaging, Docker and Homebrew carry the telemetry keys and the CI workflows silence them."""
import os
import re
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WF = REPO / ".github/workflows"


def test_debconf_templates_and_config():
    t = (REPO / "tools/packaging/debconf/templates").read_text()
    c = (REPO / "tools/packaging/debconf/config").read_text()
    assert "Template: llm-systems-manager/telemetry\nType: boolean\nDefault: true" in t
    assert "Template: llm-systems-manager/heartbeat\nType: boolean\nDefault: false" in t
    assert "db_input medium llm-systems-manager/telemetry" in c
    assert "db_input low llm-systems-manager/heartbeat" in c
    assert c.index("llm-systems-manager/heartbeat") < c.index("db_go")


def test_postinst_exports_answers_and_common_reports():
    p = (REPO / "tools/packaging/scripts/deb/postinst").read_text()
    r = (REPO / "tools/packaging/scripts/rpm/post").read_text()
    common = (REPO / "tools/packaging/scripts/common.sh").read_text()
    assert "telemetry:LLMSYS_TELEMETRY" in p and "heartbeat:LLMSYS_HEARTBEAT" in p
    assert "telemetry-report.sh" in common and '--channel "$LLMSYS_PKG_FORMAT"' in common
    assert p.index("LLMSYS_PKG_FORMAT=deb") < p.index("llmsys_configure")
    assert r.index("LLMSYS_PKG_FORMAT=rpm") < r.index("llmsys_configure")
    assert "LLMSYS_PKG_FORMAT:-pkg" in common


def _run_postinst_answers(tmp_path, env_extra, answers, old_version=""):
    """Runs the postinst debconf block against a stub confmodule; prints the exported values."""
    stub = tmp_path / "confmodule"
    cases = "".join(f'    llm-systems-manager/{k}) RET="{v}" ;;\n' for k, v in answers.items())
    stub.write_text(f'db_get() {{\n  case "$1" in\n{cases}    *) RET="" ;;\n  esac\n}}\n'
                    "db_reset() { :; }\ndb_stop() { :; }\n")
    body = (REPO / "tools/packaging/scripts/deb/postinst").read_text()
    body = body.replace("/usr/share/debconf/confmodule", str(stub)).replace("\nexit 0", "\n")
    script = ('llmsys_configure() { echo "T=${LLMSYS_TELEMETRY:-} H=${LLMSYS_HEARTBEAT:-} F=$LLMSYS_PKG_FORMAT"; }\n'
              + body)
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "LLMSYS_TELEMETRY", "LLMSYS_HEARTBEAT")}
    env.update(env_extra)
    r = subprocess.run(["bash", "-c", script, "postinst", "configure", old_version], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1]


def test_postinst_maps_debconf_booleans(tmp_path):
    out = _run_postinst_answers(tmp_path, {}, {"telemetry": "false", "heartbeat": "true"})
    assert out == "T=0 H=1 F=deb"


def test_postinst_env_overrides_debconf(tmp_path):
    out = _run_postinst_answers(tmp_path, {"LLMSYS_TELEMETRY": "0"}, {"telemetry": "true", "heartbeat": "false"})
    assert out == "T=0 H=0 F=deb"


def test_render_config_writes_block(tmp_path):
    cfg = tmp_path / "c.toml"
    env = dict(os.environ, LSM_TELEMETRY="0", LSM_HEARTBEAT="1", LSM_INFLUX_TOKEN="t",
               LSM_AE_INGEST_TOKEN="a" * 64, LSM_AE_MANAGEMENT_TOKEN="b" * 64,
               LLM_SYSTEMS_CONFIG=str(cfg), LLM_SYSTEMS_AE_DATA=str(tmp_path / "ae"))
    r = subprocess.run(["bash", "-c", f'. "{REPO}/docker/render-config.sh"; render_config'],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = cfg.read_text()
    doc = tomllib.loads(text)
    assert doc["manager"]["telemetry"] == {"install_report": False, "crash_reports": False, "heartbeat": True}
    assert "endpoint" not in text


def test_render_config_defaults(tmp_path):
    cfg = tmp_path / "c.toml"
    env = {k: v for k, v in os.environ.items() if k not in ("LSM_TELEMETRY", "LSM_HEARTBEAT")}
    env.update(LSM_INFLUX_TOKEN="t", LSM_AE_INGEST_TOKEN="a" * 64, LSM_AE_MANAGEMENT_TOKEN="b" * 64,
               LLM_SYSTEMS_CONFIG=str(cfg), LLM_SYSTEMS_AE_DATA=str(tmp_path / "ae"))
    r = subprocess.run(["bash", "-c", f'. "{REPO}/docker/render-config.sh"; render_config'],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    doc = tomllib.loads(cfg.read_text())
    assert doc["manager"]["telemetry"] == {"install_report": True, "crash_reports": True, "heartbeat": False}


def test_compose_and_env_example_carry_keys():
    compose = (REPO / "docker-compose.yml").read_text()
    assert "LSM_TELEMETRY: ${LSM_TELEMETRY:-1}" in compose
    assert "LSM_HEARTBEAT: ${LSM_HEARTBEAT:-0}" in compose
    env = (REPO / ".env.example").read_text()
    assert "LSM_TELEMETRY" in env and "LSM_HEARTBEAT" in env


def test_brew_seeder_guards_telemetry_block():
    assert r"^\[manager.telemetry\]" in (REPO / "tools/installer/brew-seed-config.sh").read_text()


def _logical_lines(path):
    """Workflow run lines with backslash continuations joined; comments dropped."""
    text = re.sub(r"\\\n\s*", " ", path.read_text())
    return [ln.strip() for ln in text.splitlines() if not ln.strip().startswith("#")]


_INSTALLER = re.compile(r"(tools/installer/(install|update)\.sh|/install\.sh\")")


def test_ci_workflows_silence_reports():
    for wf in ("install-test.yml", "backup-restore-test.yml", "package-test.yml"):
        for ln in _logical_lines(WF / wf):
            if "sudo" in ln and _INSTALLER.search(ln) and "agent/install" not in ln:
                assert "LLMSYS_TELEMETRY=0" in ln, f"{wf}: {ln}"
            if "update.sh" in ln and "sudo" in ln:
                assert "CI=true" in ln, f"{wf}: {ln}"
    for ln in _logical_lines(WF / "package-test.yml"):
        if re.search(r"(apt-get|dnf) install .*\./(dist|old)/llm-systems-manager", ln):
            assert "LLMSYS_TELEMETRY=0" in ln, ln
    docker = (WF / "docker.yml").read_text()
    assert docker.count('echo "LSM_TELEMETRY=0"') == 2


def test_install_sh_revision_bumped():
    assert "_INSTALL_SH_REVISION=20261014002" in (REPO / "tools/installer/install.sh").read_text()


def test_postinst_upgrade_ignores_stored_debconf_answers(tmp_path):
    out = _run_postinst_answers(tmp_path, {}, {"telemetry": "true", "heartbeat": "true"}, old_version="1.0.6")
    assert out == "T= H= F=deb"


COMMON = REPO / "tools/packaging/scripts/common.sh"


def test_common_reports_as_the_run_user_without_chown():
    text = COMMON.read_text()
    body = text[text.index("llmsys_telemetry_report() {"):]
    body = body[:body.index("\n}\n")]
    assert 'runuser -u "$LLMSYS_RUN_USER" -- env' in body
    assert "chown" not in body


def _upgrade_answers(tmp_path, env_extra):
    cfg = tmp_path / "c.toml"
    cfg.write_text((REPO / "config/llm-systems.toml.example").read_text())
    script = (f'set -e; . "{COMMON}"; LLMSYS_INSTALL_DIR="{REPO}"; LLMSYS_CFG="{cfg}"; llmsys_telemetry_answers')
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "LLMSYS_TELEMETRY", "LLMSYS_HEARTBEAT")}
    env.update(env_extra)
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return tomllib.loads(cfg.read_text())["manager"]["telemetry"], r.stdout


def test_package_upgrade_writes_explicit_answers(tmp_path):
    tele, out = _upgrade_answers(tmp_path, {"LLMSYS_TELEMETRY": "0"})
    assert tele == {"install_report": False, "crash_reports": False, "heartbeat": False}
    assert "Anonymous install and crash reports are on" not in out
    tele, out = _upgrade_answers(tmp_path, {"LLMSYS_HEARTBEAT": "1"})
    assert tele == {"install_report": True, "crash_reports": True, "heartbeat": True}
    assert "Anonymous install and crash reports are on" in out


def test_package_upgrade_without_answers_keeps_defaults_and_says_so(tmp_path):
    tele, out = _upgrade_answers(tmp_path, {})
    assert tele["install_report"] is True and tele["heartbeat"] is False
    assert "Anonymous install and crash reports are on" in out
