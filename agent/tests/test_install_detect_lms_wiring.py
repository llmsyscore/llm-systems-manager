# agent/tests/test_install_detect_lms_wiring.py
"""install.sh wiring of detect_lms.py: overrides, enable, agent-user proposal."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"
FUNCS = ("_run_detect_lms_py", "_propose_agent_user", "_detect_lms")


_GETENT = ("getent() { [[ \"$1\" == passwd ]] || return 2; case \"$2\" in "
           "ana) echo 'ana:x:2001:2001::/home/ana:/bin/sh' ;; bob) echo 'bob:x:2002:2002::/home/bob:/bin/sh' ;; "
           "*) return 2 ;; esac; }")


def _func(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", INSTALL_SH.read_text(), re.S | re.M)
    assert m, f"{name} not found in install.sh"
    return m.group(0)


def _run(tmp_path: Path, fake_out: str, pre: str = "") -> subprocess.CompletedProcess:
    (tmp_path / "detect_lms.py").write_text(f"print({fake_out!r})\n")
    script = "\n".join([
        "set -euo pipefail",
        f"TMPL_DIR={tmp_path}",
        "AGENT_OS=linux ENABLE_LMS=false LMS_API_URL_OVERRIDE='' LMS_CMD_OVERRIDE='' USER_ARG=llmsys",
        "_user_exists() { [[ \"$1\" == ana || \"$1\" == llama || \"$1\" == llmsys || \"$1\" == root ]]; }",
        _GETENT,
        *(_func(f) for f in FUNCS),
        pre,
        "_detect_lms",
        'echo "ENABLE=$ENABLE_LMS URL=$LMS_API_URL_OVERRIDE CMD=$LMS_CMD_OVERRIDE USER=$USER_ARG"',
    ])
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=30)


RUNNING = "\n".join([
    "LMS_DETECTED_FOUND=true", "LMS_DETECTED_RUNNING=true", "LMS_DETECTED_PID=4242",
    "LMS_DETECTED_PROCESS_USER=ana", "LMS_DETECTED_PORT=1240",
    "LMS_DETECTED_API_URL=http://127.0.0.1:1240",
    "LMS_DETECTED_LMS_CMD=/home/ana/.lmstudio/bin/lms", "LMS_DETECTED_LMS_USER=ana",
])

STOPPED = "\n".join([
    "LMS_DETECTED_FOUND=true", "LMS_DETECTED_RUNNING=false", "LMS_DETECTED_PID=''",
    "LMS_DETECTED_PROCESS_USER=''", "LMS_DETECTED_PORT=''", "LMS_DETECTED_API_URL=''",
    "LMS_DETECTED_LMS_CMD=/home/ana/.lmstudio/bin/lms", "LMS_DETECTED_LMS_USER=ana",
])


def test_running_sets_url_cmd_enable(tmp_path):
    r = _run(tmp_path, RUNNING)
    assert r.returncode == 0, r.stderr
    assert ("Found LM Studio: server running on port 1240, "
            "lms /home/ana/.lmstudio/bin/lms, runs as ana") in r.stdout
    assert ("ENABLE=true URL=http://127.0.0.1:1240 "
            "CMD=/home/ana/.lmstudio/bin/lms USER=ana") in r.stdout
    assert "LM Studio runs as ana; running the agent as the same user" in r.stdout


def test_stopped_still_enables(tmp_path):
    r = _run(tmp_path, STOPPED)
    assert r.returncode == 0, r.stderr
    assert ("Found LM Studio: server not running, "
            "lms /home/ana/.lmstudio/bin/lms, runs as ana") in r.stdout
    assert "ENABLE=true URL= CMD=/home/ana/.lmstudio/bin/lms USER=ana" in r.stdout


def test_lms_user_fallback_requires_own_home(tmp_path):
    outside = STOPPED.replace("LMS_CMD=/home/ana/.lmstudio/bin/lms", "LMS_CMD=/srv/shared/lms")
    r = _run(tmp_path, outside)
    assert r.returncode == 0, r.stderr
    assert "USER=llmsys" in r.stdout and "LM Studio runs as" not in r.stdout
    other = STOPPED.replace("LMS_CMD=/home/ana/", "LMS_CMD=/home/bob/")
    r = _run(tmp_path, other)
    assert r.returncode == 0, r.stderr
    assert "USER=llmsys" in r.stdout and "LM Studio runs as" not in r.stdout
    r = _run(tmp_path, STOPPED)
    assert r.returncode == 0, r.stderr
    assert "USER=ana" in r.stdout


def test_nothing_found_no_tty_skips(tmp_path):
    r = _run(tmp_path, "LMS_DETECTED_FOUND=false")
    assert r.returncode == 0, r.stderr
    assert "→ skipped" in r.stdout
    assert "ENABLE=false URL= CMD= USER=llmsys" in r.stdout


def test_root_never_proposed(tmp_path):
    out = RUNNING.replace("PROCESS_USER=ana", "PROCESS_USER=root").replace(
        "LMS_USER=ana", "LMS_USER=root")
    r = _run(tmp_path, out)
    assert r.returncode == 0, r.stderr
    assert "runs as root" in r.stdout
    assert "USER=llmsys" in r.stdout
    assert "running the agent as the same user" not in r.stdout


def test_llama_user_wins_over_lms_user_in_detect_lms(tmp_path):
    r = _run(tmp_path, RUNNING, "LLAMA_DETECTED_UNIT_USER=llama")
    assert r.returncode == 0, r.stderr
    assert "USER=llama" in r.stdout
    assert "LM Studio runs as" not in r.stdout


def test_lms_user_not_proposed_with_explicit_user(tmp_path):
    r = _run(tmp_path, RUNNING, "USER_ARG_EXPLICIT=true")
    assert r.returncode == 0, r.stderr
    assert "USER=llmsys" in r.stdout


def test_detect_lms_shipped_and_copied_in_both_paths():
    text = INSTALL_SH.read_text()
    assert '"$TMPL_DIR/detect_lms.py"\n' in _func("_compute_required_install_files")
    line = 'cp "$TMPL_DIR/detect_lms.py" "$INSTALL_DIR/install/detect_lms.py"'
    assert text.count(line) == 2
    assert text.count('chmod 0644 "$INSTALL_DIR/install/detect_lms.py"') == 2


def test_old_bash_probes_removed():
    body = _func("_detect_lms")
    for gone in ("pgrep", "lsof", "lms status", "_probe_http_code"):
        assert gone not in body, gone


LLAMA_FOUND = "\n".join([
    "LLAMA_DETECTED_FOUND=true", "LLAMA_DETECTED_UNIT_USER=llama", "LLAMA_DETECTED_PROCESS_USER=llama",
])


def _precheck() -> str:
    text = INSTALL_SH.read_text()
    m = re.search(r"^# Fresh auto installs: a running llama-server's user.*?^fi\n", text, re.S | re.M)
    assert m, "pre-account detection block not found"
    assert text.index(m.group(0)) < text.index('while ! _user_exists "$USER_ARG"; do')
    return m.group(0)


def _run_precheck(tmp_path: Path, llama_out: str, lms_out: str, after: str = "",
                  flags: str = "") -> subprocess.CompletedProcess:
    (tmp_path / "detect_llama.py").write_text(f"print({llama_out!r})\n")
    (tmp_path / "detect_lms.py").write_text(f"print({lms_out!r})\n")
    script = "\n".join([
        "set -euo pipefail",
        f"TMPL_DIR={tmp_path}",
        "_user_exists() { [[ \"$1\" == ana || \"$1\" == llama || \"$1\" == root ]]; }",
        _GETENT,
        *(_func(f) for f in FUNCS), _func("_run_detect_llama_py"),
        "AGENT_OS=linux ENABLE_LMS=false LMS_API_URL_OVERRIDE='' LMS_CMD_OVERRIDE=''",
        "ROLE=auto LLAMA_FLAG_EXPLICIT=false LMS_FLAG_EXPLICIT=false INSTALL_LLAMA=false USER_ARG=llmsys",
        flags, _precheck(), after, 'echo "USER=$USER_ARG"',
    ])
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=30)


def test_lms_user_proposed_before_account_step_when_no_llama(tmp_path):
    r = _run_precheck(tmp_path, "LLAMA_DETECTED_FOUND=false", RUNNING)
    assert r.returncode == 0, r.stderr
    assert "LM Studio runs as ana; running the agent as the same user" in r.stdout
    assert "USER=ana" in r.stdout


def test_lms_user_used_when_llama_user_is_root(tmp_path):
    llama_root = LLAMA_FOUND.replace("=llama", "=root")
    r = _run_precheck(tmp_path, llama_root, RUNNING)
    assert r.returncode == 0, r.stderr
    assert "USER=ana" in r.stdout


def test_llama_user_wins_over_lms_user(tmp_path):
    r = _run_precheck(tmp_path, LLAMA_FOUND, RUNNING)
    assert r.returncode == 0, r.stderr
    assert "llama-server runs as llama" in r.stdout
    assert "LM Studio runs as" not in r.stdout
    assert "USER=llama" in r.stdout


def test_lms_user_not_proposed_twice(tmp_path):
    r = _run_precheck(tmp_path, "LLAMA_DETECTED_FOUND=false", RUNNING, "USER_ARG=other\n_detect_lms")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("LM Studio runs as ana;") == 1
    assert "USER=other" in r.stdout


def test_precheck_skips_lms_for_explicit_flag_or_user(tmp_path):
    for flags in ("LMS_FLAG_EXPLICIT=true", "USER_ARG_EXPLICIT=true", "ROLE=system_only"):
        r = _run_precheck(tmp_path, "LLAMA_DETECTED_FOUND=false", RUNNING, flags=flags)
        assert r.returncode == 0, r.stderr
        assert "USER=llmsys" in r.stdout, flags
