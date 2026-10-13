# tools/installer/tests/test_telemetry_shell.py
"""lib-common telemetry_*: consent from env/CI/prompt, payload preview, inline send, retry job."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1]
REPO = INSTALLER.parents[1]
LIB = INSTALLER / "lib-common.sh"
RETRY = INSTALLER / "telemetry-retry.sh"


def fake_curl(tmp_path, rc=0):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    c = b / "curl"
    c.write_text("#!/usr/bin/env bash\n"
                 f'printf "%s\\n" "$@" >> "{b}/curl.calls"\n'
                 f"exit {rc}\n")
    c.chmod(c.stat().st_mode | stat.S_IEXEC)
    return b


def run(tmp_path, script, env_extra=None, stdin=""):
    b = fake_curl(tmp_path, rc=env_extra.pop("_CURL_RC", 0) if env_extra else 0)
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "LLMSYS_TELEMETRY", "LLMSYS_HEARTBEAT")}
    env["PATH"] = f"{b}:{env['PATH']}"
    env["LLMSYS_INSTALL_DIR"] = str(tmp_path)
    env.update(env_extra or {})
    return subprocess.run(["bash", "-c", f'. "{LIB}"; {script}'], env=env, input=stdin,
                          capture_output=True, text=True)


def test_env_zero_beats_prompt(tmp_path):
    r = run(tmp_path, "telemetry_consent; echo T=$LLMSYS_TELEMETRY H=$LLMSYS_HEARTBEAT R=$LLMSYS_TELEMETRY_REASON",
            {"LLMSYS_TELEMETRY": "0"})
    assert r.returncode == 0, r.stderr
    assert "T=0 H=0 R=env" in r.stdout


def test_ci_guard(tmp_path):
    r = run(tmp_path, "telemetry_consent; echo T=$LLMSYS_TELEMETRY R=$LLMSYS_TELEMETRY_REASON", {"CI": "true"})
    assert "T=0 R=ci" in r.stdout
    assert "Install report skipped: CI is set" in r.stdout


def test_non_tty_defaults(tmp_path):
    r = run(tmp_path, "telemetry_consent; echo T=$LLMSYS_TELEMETRY H=$LLMSYS_HEARTBEAT R=$LLMSYS_TELEMETRY_REASON")
    assert "T=1 H=0 R=default" in r.stdout


def test_payload_preview_has_no_hostname(tmp_path):
    import socket
    r = run(tmp_path, f'telemetry_payload install script 3 ok "{tmp_path}" v1 "{REPO}"')
    assert r.returncode == 0, r.stderr
    p = json.loads(r.stdout)
    assert p["event"] == "install" and p["mode"] == "3"
    assert socket.gethostname() not in r.stdout


def test_report_sends_inline_and_records(tmp_path):
    r = run(tmp_path, f'LLMSYS_TELEMETRY=1 telemetry_report install script 3 ok "{tmp_path}" v1 "{REPO}" https://t.example.test/v1/event')
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "telemetry-last.json").exists()
    assert not (tmp_path / "telemetry-pending.json").exists()
    assert "https://t.example.test/v1/event" in (tmp_path / "bin" / "curl.calls").read_text()


def test_report_failure_leaves_pending_and_spawns_retry(tmp_path):
    r = run(tmp_path, f'LLMSYS_TELEMETRY=1 LLMSYS_TELEMETRY_RETRY_DELAYS="0 0" telemetry_report install script 3 ok "{tmp_path}" v1 "{REPO}" https://t.example.test/v1/event; sleep 2',
            {"_CURL_RC": 7})
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "telemetry.log").read_text()
    assert "attempt 1 failed" in log and "attempt 3 failed" in log
    assert (tmp_path / "telemetry-pending.json").exists()


def test_report_is_noop_without_consent(tmp_path):
    r = run(tmp_path, f'LLMSYS_TELEMETRY=0 telemetry_report install script 3 ok "{tmp_path}" v1 "{REPO}" https://t.example.test/v1/event')
    assert r.returncode == 0
    assert not (tmp_path / "bin" / "curl.calls").exists()


def test_retry_script_renames_on_success(tmp_path):
    pend = tmp_path / "telemetry-pending.json"
    pend.write_text(json.dumps({"schema": 1, "event": "install", "install_id": "x"}))
    b = fake_curl(tmp_path, rc=0)
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", LLMSYS_TELEMETRY_RETRY_DELAYS="0 0")
    r = subprocess.run(["bash", str(RETRY), str(pend), "https://t.example.test/v1/event",
                        str(tmp_path / "telemetry.log"), sys.executable, str(REPO)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "telemetry-last.json").exists() and not pend.exists()


def test_read_toml_key_bare_boolean(tmp_path):
    t = tmp_path / "c.toml"
    t.write_text('[manager.telemetry]\ninstall_report = true  # consent\n')
    r = run(tmp_path, f'SUDO=""; read_toml_key install_report "{t}"')
    assert r.stdout.split() == ["true"]


def test_report_replaces_planted_pending_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / "telemetry-pending.json").symlink_to(victim)
    r = run(tmp_path, f'LLMSYS_TELEMETRY=1 telemetry_report install script 3 ok "{tmp_path}" v1 "{REPO}" https://t.example.test/v1/event')
    assert r.returncode == 0, r.stderr
    assert victim.read_text() == "keep"
    assert not (tmp_path / "telemetry-pending.json").is_symlink()


def test_report_leaves_planted_log_symlink_alone(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / "telemetry.log").symlink_to(victim)
    r = run(tmp_path, f'LLMSYS_TELEMETRY=1 telemetry_report install script 3 ok "{tmp_path}" v1 "{REPO}" https://t.example.test/v1/event')
    assert r.returncode == 0, r.stderr
    assert victim.read_text() == "keep"


def test_endpoint_env_override_and_default(tmp_path):
    r = run(tmp_path, "telemetry_endpoint", {"LLMSYS_TELEMETRY_ENDPOINT": "https://x.example.test/e"})
    assert r.stdout.strip() == "https://x.example.test/e"
    r = run(tmp_path, "telemetry_endpoint", {"LLMSYS_TELEMETRY_ENDPOINT": "http://bad"})
    assert r.stdout.strip().startswith("https://telemetry-receiver.")


def run_wrapper(tmp_path, args, env_extra=None):
    b = fake_curl(tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "LLMSYS_TELEMETRY", "LLMSYS_HEARTBEAT")}
    env["PATH"] = f"{b}:{env['PATH']}"
    env["LLMSYS_INSTALL_DIR"] = str(tmp_path)
    env.update(env_extra or {})
    data = tmp_path / "state"
    r = subprocess.run(["bash", str(INSTALLER / "telemetry-report.sh"), "--event", "install", "--channel", "script",
                        "--mode", "5", "--data-dir", str(data), "--root", str(REPO), *args],
                       env=env, capture_output=True, text=True)
    return r, data


def test_wrapper_without_toml_honours_exported_consent(tmp_path):
    r, data = run_wrapper(tmp_path, [], {"LLMSYS_TELEMETRY": "1"})
    assert r.returncode == 0, r.stderr
    assert (data / "telemetry-last.json").exists()


def test_wrapper_without_toml_or_consent_sends_nothing(tmp_path):
    r, data = run_wrapper(tmp_path, [])
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "bin" / "curl.calls").exists()


def test_wrapper_toml_answer_beats_exported_consent(tmp_path):
    t = tmp_path / "c.toml"
    t.write_text("[manager.telemetry]\ninstall_report = false\n")
    r, data = run_wrapper(tmp_path, ["--toml", str(t)], {"LLMSYS_TELEMETRY": "1", "SUDO": ""})
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "bin" / "curl.calls").exists()
