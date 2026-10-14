# tools/installer/tests/test_telemetry_shell.py
"""lib-common telemetry_*: consent from env/CI/prompt, payload preview, inline send, retry job."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
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


def test_env_heartbeat_alone_keeps_report_on(tmp_path):
    r = run(tmp_path, "telemetry_consent; echo T=$LLMSYS_TELEMETRY H=$LLMSYS_HEARTBEAT R=$LLMSYS_TELEMETRY_REASON",
            {"LLMSYS_HEARTBEAT": "1"})
    assert r.returncode == 0, r.stderr
    assert "T=1 H=1 R=env" in r.stdout


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


def test_payload_includes_release_from_release_file(tmp_path):
    root = tmp_path / "root"
    shutil.copytree(REPO / "llm-systems-manager" / "backend", root / "llm-systems-manager" / "backend")
    (root / "RELEASE").write_text("v2026.10.7\n")
    r = run(tmp_path, f'telemetry_payload install script 3 ok "{tmp_path}" v1 "{root}"')
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["release"] == "v2026.10.7"


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
    data.mkdir(exist_ok=True)
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


def test_report_is_noop_under_ci(tmp_path):
    r = run(tmp_path, f'CI=1 LLMSYS_TELEMETRY=1 telemetry_report install script 3 ok "{tmp_path}" v1 "{REPO}" https://t.example.test/v1/event')
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "bin" / "curl.calls").exists()
    assert not list(tmp_path.glob("telemetry*")) and not (tmp_path / "install-id").exists()


def test_report_never_creates_missing_data_dir(tmp_path):
    d = tmp_path / "missing" / "data"
    r = run(tmp_path, f'LLMSYS_TELEMETRY=1 telemetry_report install script 3 ok "{d}" v1 "{REPO}" https://t.example.test/v1/event')
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "missing").exists()
    assert not (tmp_path / "bin" / "curl.calls").exists()


def _fake_bin(tmp_path, name):
    b = fake_curl(tmp_path)
    c = b / name
    c.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{b}/{name}.calls"\n')
    c.chmod(c.stat().st_mode | stat.S_IEXEC)
    return b


def test_report_as_user_runs_through_sudo_with_explicit_env(tmp_path):
    b = _fake_bin(tmp_path, "sudo")
    r = run(tmp_path, f'LLMSYS_TELEMETRY=1 LLMSYS_TELEMETRY_RETRY_DELAYS="0 0" '
                      f'telemetry_report_as_user svc install script 3 ok "{tmp_path}/data" v9 /r; echo done')
    assert r.returncode == 0 and "done" in r.stdout, r.stderr
    argv = (b / "sudo.calls").read_text().splitlines()
    assert argv[:4] == ["-n", "-u", "svc", "env"]
    assert "LLMSYS_TELEMETRY=1" in argv and "LLMSYS_TELEMETRY_RETRY_DELAYS=0 0" in argv
    assert "LLMSYS_TELEMETRY_ENDPOINT=" in argv and "CI=" in argv
    assert any(a.startswith("LLMSYS_INSTALL_DIR=") for a in argv)
    i = argv.index("bash")
    assert argv[i + 1:] == ["/r/tools/installer/telemetry-report.sh", "--event", "install", "--channel", "script",
                            "--mode", "3", "--outcome", "ok", "--data-dir", f"{tmp_path}/data",
                            "--version", "v9", "--root", "/r"]


def test_report_as_user_uses_runuser_when_root(tmp_path):
    b = _fake_bin(tmp_path, "runuser")
    bash = subprocess.run(["bash", "-c", "command -v bash"], capture_output=True, text=True).stdout.strip()
    env_bin = subprocess.run(["bash", "-c", "command -v env"], capture_output=True, text=True).stdout.strip()
    iso = tmp_path / "iso"
    iso.mkdir()
    for tool, src in (("bash", bash), ("env", env_bin), ("runuser", str(b / "runuser"))):
        (iso / tool).symlink_to(src)
    fake_id = iso / "id"
    fake_id.write_text(f'#!{bash}\nif [ "$1" = "-u" ]; then echo 0; else echo root; fi\n')
    fake_id.chmod(0o755)
    r = subprocess.run([bash, "-c", f'. "{LIB}"; telemetry_report_as_user svc update script "" ok /d v1 /r; echo done'],
                       env={"PATH": str(iso)}, capture_output=True, text=True)
    assert r.returncode == 0 and "done" in r.stdout, r.stderr
    argv = (b / "runuser.calls").read_text().splitlines()
    assert argv[:4] == ["-u", "svc", "--", "env"]


def test_report_as_user_runs_directly_when_already_that_user(tmp_path):
    b = _fake_bin(tmp_path, "sudo")
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    data = tmp_path / "data"
    data.mkdir()
    r = run(tmp_path, f'LLMSYS_TELEMETRY=1 telemetry_report_as_user {me} install script 3 ok "{data}" v9 "{REPO}"')
    assert r.returncode == 0, r.stderr
    assert not (b / "sudo.calls").exists()
    assert json.loads((data / "telemetry-last.json").read_text())["version"] == "v9"


def test_lib_common_has_no_chown_helper():
    assert "telemetry_chown" not in LIB.read_text()


def test_write_answers_sets_the_section(tmp_path):
    t = tmp_path / "c.toml"
    t.write_text("[manager]\nx = 1\n\n[manager.telemetry]\ninstall_report = true\ncrash_reports = true\n"
                 "heartbeat = false\n\n[other]\nheartbeat = false\n")
    r = run(tmp_path, f'SUDO=""; telemetry_write_answers "{t}" 0 1')
    assert r.returncode == 0, r.stderr
    import tomllib
    doc = tomllib.loads(t.read_text())
    assert doc["manager"]["telemetry"] == {"install_report": False, "crash_reports": False, "heartbeat": True}
    assert doc["other"]["heartbeat"] is False


def test_report_as_user_stages_a_readable_copy_when_root_is_unreadable(tmp_path):
    b = fake_curl(tmp_path)
    sudo = b / "sudo"
    sudo.write_text("#!/usr/bin/env bash\n"
                    'if [ "$1" = "-n" ] && [ "$2" = "-u" ]; then\n'
                    '  if [ "$4" = "test" ]; then exit 1; fi\n'
                    f'  printf "%s\\n" "$@" > "{b}/sudo.calls"; exit 0\n'
                    'fi\n'
                    '[ "$1" = "-n" ] && shift\n'
                    'exec "$@"\n')
    sudo.chmod(0o755)
    real_install = subprocess.run(["bash", "-c", "command -v install"], capture_output=True, text=True).stdout.strip()
    inst = b / "install"
    inst.write_text("#!/usr/bin/env bash\nargs=()\nwhile [ $# -gt 0 ]; do\n"
                    '  if [ "$1" = "-o" ]; then shift 2; continue; fi\n  args+=("$1"); shift\ndone\n'
                    f'exec {real_install} "${{args[@]}}"\n')
    inst.chmod(0o755)
    chown = b / "chown"
    chown.write_text("#!/usr/bin/env bash\nexit 0\n")
    chown.chmod(0o755)
    before = set(Path("/tmp").glob("llmsys-telemetry.*"))
    r = run(tmp_path, f'SUDO=""; LLMSYS_TELEMETRY=1 telemetry_report_as_user svc install script 4 ok "{tmp_path}/data" v1 "{REPO}"')
    assert r.returncode == 0, r.stderr
    new = set(Path("/tmp").glob("llmsys-telemetry.*")) - before
    assert len(new) == 1
    staged = new.pop()
    try:
        for rel in ("llm-systems-manager/backend/telemetry.py", "tools/installer/lib-common.sh",
                    "tools/installer/telemetry-report.sh", "tools/installer/telemetry-retry.sh"):
            assert (staged / rel).read_bytes() == (REPO / rel).read_bytes(), rel
        argv = (b / "sudo.calls").read_text().splitlines()
        assert argv[argv.index("bash") + 1] == f"{staged}/tools/installer/telemetry-report.sh"
        assert argv[argv.index("--root") + 1] == str(staged)
    finally:
        shutil.rmtree(staged)


def test_report_as_user_keeps_a_readable_root(tmp_path):
    b = fake_curl(tmp_path)
    sudo = b / "sudo"
    sudo.write_text("#!/usr/bin/env bash\n"
                    'if [ "$4" = "test" ]; then exit 0; fi\n'
                    f'printf "%s\\n" "$@" > "{b}/sudo.calls"\n')
    sudo.chmod(0o755)
    r = run(tmp_path, f'LLMSYS_TELEMETRY=1 telemetry_report_as_user svc install script 3 ok "{tmp_path}/data" v1 "{REPO}"')
    assert r.returncode == 0, r.stderr
    argv = (b / "sudo.calls").read_text().splitlines()
    assert argv[argv.index("--root") + 1] == str(REPO)


def test_report_as_user_skips_without_sudo_when_not_root(tmp_path):
    bash = subprocess.run(["bash", "-c", "command -v bash"], capture_output=True, text=True).stdout.strip()
    iso = tmp_path / "iso"
    iso.mkdir()
    fake_id = iso / "id"
    fake_id.write_text(f'#!{bash}\nif [ "$1" = "-u" ]; then echo 1000; else echo someone; fi\n')
    fake_id.chmod(0o755)
    r = subprocess.run([bash, "-c", f'. "{LIB}"; telemetry_report_as_user svc update script "" ok /d v1 /r; echo done'],
                       env={"PATH": str(iso)}, capture_output=True, text=True)
    assert r.returncode == 0 and "done" in r.stdout, r.stderr


def test_write_answers_refuses_a_symlinked_toml(tmp_path):
    target = tmp_path / "target.toml"
    body = "[manager.telemetry]\ninstall_report = true\ncrash_reports = true\nheartbeat = false\n"
    target.write_text(body)
    link = tmp_path / "llm-systems.toml"
    link.symlink_to(target)
    r = run(tmp_path, f'SUDO=""; telemetry_write_answers "{link}" 0 1; echo rc=$?')
    assert "rc=0" not in r.stdout
    assert link.is_symlink() and os.readlink(link) == str(target)
    assert target.read_text() == body
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "bin") == ["llm-systems.toml", "target.toml"]


def test_write_answers_keeps_the_file_mode(tmp_path):
    t = tmp_path / "c.toml"
    t.write_text("[manager.telemetry]\ninstall_report = true\ncrash_reports = true\nheartbeat = false\n")
    t.chmod(0o600)
    r = run(tmp_path, f'SUDO=""; telemetry_write_answers "{t}" 0 0')
    assert r.returncode == 0, r.stderr
    assert oct(t.stat().st_mode & 0o777) == "0o600"
    assert "install_report = false" in t.read_text()
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def _staged_copy():
    staged = Path(tempfile.mkdtemp(prefix="llmsys-telemetry.", dir="/tmp"))
    for rel in ("llm-systems-manager/backend/telemetry.py", "tools/installer/lib-common.sh",
                "tools/installer/telemetry-report.sh", "tools/installer/telemetry-retry.sh"):
        (staged / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, staged / rel)
    return staged


def test_inline_success_removes_a_staged_root(tmp_path):
    staged = _staged_copy()
    data = tmp_path / "data"
    data.mkdir()
    try:
        r = run(tmp_path, f'LLMSYS_TELEMETRY=1 telemetry_report install script 3 ok "{data}" v1 "{staged}" https://t.example.test/e')
        assert r.returncode == 0, r.stderr
        assert (data / "telemetry-last.json").is_file()
        assert not staged.exists()
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def test_inline_success_keeps_a_real_root(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    r = run(tmp_path, f'LLMSYS_TELEMETRY=1 telemetry_report install script 3 ok "{data}" v1 "{REPO}" https://t.example.test/e')
    assert r.returncode == 0, r.stderr
    assert (REPO / "tools/installer/telemetry-report.sh").is_file()


def test_retry_removes_a_staged_root_when_done(tmp_path):
    staged = _staged_copy()
    pend = tmp_path / "telemetry-pending.json"
    pend.write_text("{}")
    b = fake_curl(tmp_path)
    try:
        subprocess.run(["bash", str(staged / "tools/installer/telemetry-retry.sh"), str(pend),
                        "https://t.example.test/e", str(tmp_path / "telemetry.log"), sys.executable, str(staged)],
                       env={**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "LLMSYS_TELEMETRY_RETRY_DELAYS": "0 0"},
                       check=True, timeout=30)
        assert (tmp_path / "telemetry-last.json").is_file()
        assert not staged.exists()
    finally:
        shutil.rmtree(staged, ignore_errors=True)
