# agent/tests/test_svcconfig_wrapper.py
"""#287: llm-svcconfig-apply rewrites ONLY the unit's ExecStart line from
validated stdin tokens, so the agent needs no grant to write unit content."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
TMPL = AGENT_DIR / "install" / "llm-svcconfig-apply.sh.tmpl"

UNIT_TEXT = """[Unit]
Description=llama.cpp server (llama-server)

[Service]
Type=simple
User=llmagent
Environment=CUDA_VISIBLE_DEVICES=0
ExecStart=/old/bin --metrics --host 127.0.0.1 --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


def _shims(binroot: Path):
    (binroot / "systemctl").write_text("#!/usr/bin/env bash\nexit 0\n")
    # install shim strips owner/mode flags so the test can run non-root
    (binroot / "install").write_text(
        "#!/usr/bin/env bash\nargs=()\n"
        "while [[ $# -gt 0 ]]; do case \"$1\" in -o|-g|-m) shift 2;; *) args+=(\"$1\"); shift;; esac; done\n"
        "cp \"${args[0]}\" \"${args[1]}\"\n")
    for f in ("systemctl", "install"):
        os.chmod(binroot / f, 0o755)


def _run(tmp_path, stdin_lines, binary):
    unit = tmp_path / "llama_server.service"
    unit.write_text(UNIT_TEXT)
    binroot = tmp_path / "bin"; binroot.mkdir()
    _shims(binroot)
    script = TMPL.read_text().replace("__UNIT_PATH__", str(unit)) \
                             .replace("__SYSTEMCTL__", "systemctl") \
                             .replace("__ETC__", str(tmp_path / "etc"))
    sh = tmp_path / "wrapper.sh"; sh.write_text(script); os.chmod(sh, 0o755)
    payload = "\n".join([binary, *stdin_lines]) + "\n"
    env = dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}")
    p = subprocess.run(["bash", str(sh)], input=payload, capture_output=True,
                       text=True, env=env)
    return p, unit


def _fake_bin(tmp_path) -> str:
    b = tmp_path / "llama-server"; b.write_text("#!/bin/sh\n"); os.chmod(b, 0o755)
    return str(b)


def _exec_line(unit: Path) -> str:
    return next(l for l in unit.read_text().splitlines() if l.startswith("ExecStart="))


def test_swaps_only_execstart_and_preserves_rest(tmp_path):
    b = _fake_bin(tmp_path)
    p, unit = _run(tmp_path, ["--metrics", "--host", "127.0.0.1", "--port", "9090"], b)
    assert p.returncode == 0, p.stderr
    text = unit.read_text()
    assert f"ExecStart={b} --metrics --host 127.0.0.1 --port 9090" in text
    assert "User=llmagent" in text
    assert "Environment=CUDA_VISIBLE_DEVICES=0" in text
    assert "/old/bin" not in text


def test_quotes_value_with_space(tmp_path):
    b = _fake_bin(tmp_path)
    p, unit = _run(tmp_path, ["--chat-template", "a b c"], b)
    assert p.returncode == 0, p.stderr
    assert '--chat-template "a b c"' in _exec_line(unit)


def test_trailing_backslash_arg_does_not_continue_line(tmp_path):
    # A raw trailing backslash would be a systemd line-continuation that swallows
    # the next directive (User=). It must be quoted+doubled, not emitted raw.
    b = _fake_bin(tmp_path)
    p, unit = _run(tmp_path, ["--host", "0.0.0.0\\"], b)
    assert p.returncode == 0, p.stderr
    line = _exec_line(unit)
    assert not line.rstrip("\n").endswith("\\") or line.rstrip("\n").endswith('\\"')
    assert '"0.0.0.0\\\\"' in line          # rendered as "0.0.0.0\\"
    # every other directive is preserved (nothing merged into ExecStart)
    text = unit.read_text()
    assert "User=llmagent" in text
    assert "Restart=on-failure" in text


def test_backslash_in_middle_arg_is_escaped(tmp_path):
    b = _fake_bin(tmp_path)
    p, unit = _run(tmp_path, ["--chat-template", "a\\b"], b)
    assert p.returncode == 0, p.stderr
    assert '"a\\\\b"' in _exec_line(unit)     # single backslash doubled inside quotes


def test_rejects_relative_binary(tmp_path):
    p, unit = _run(tmp_path, ["--metrics"], "relative/bin")
    assert p.returncode != 0
    assert "absolute path" in p.stderr
    assert "/old/bin" in unit.read_text()  # untouched


def test_rejects_control_char_in_arg(tmp_path):
    b = _fake_bin(tmp_path)
    p, unit = _run(tmp_path, ["--host", "1.1.1.1\tUser=root"], b)
    assert p.returncode != 0
    assert "control character" in p.stderr
    assert "/old/bin" in unit.read_text()


def test_rejects_binary_with_space(tmp_path):
    p, _ = _run(tmp_path, ["--metrics"], "/bin/sh -c evil")
    assert p.returncode != 0
    assert "disallowed characters" in p.stderr


def test_errors_when_no_execstart_line(tmp_path):
    unit = tmp_path / "llama_server.service"
    unit.write_text("[Service]\nUser=x\n")
    binroot = tmp_path / "bin"; binroot.mkdir(); _shims(binroot)
    b = _fake_bin(tmp_path)
    script = TMPL.read_text().replace("__UNIT_PATH__", str(unit)) \
                             .replace("__SYSTEMCTL__", "systemctl") \
                             .replace("__ETC__", str(tmp_path / "etc"))
    sh = tmp_path / "w.sh"; sh.write_text(script); os.chmod(sh, 0o755)
    env = dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}")
    p = subprocess.run(["bash", str(sh)], input=f"{b}\n--metrics\n",
                       capture_output=True, text=True, env=env)
    assert p.returncode != 0
    assert "no ExecStart" in p.stderr


# ── header protocol (#1419) ──
HDR_UNIT = "llama-server.service"
HDR_ORIG = "[Service]\nUser=x\nExecStart=/bin/old --a\nRestart=always\n"

HDR_FAKE = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
[[ -z "${FAKE_FAIL:-}" ]] || exit 1
if [[ "$1" == show ]]; then
  case "$3" in
    LoadState) echo "${FAKE_LOADSTATE:-loaded}" ;;
    ExecStart) echo "$FAKE_EXEC" ;;
    FragmentPath) echo "$FAKE_FRAGMENT" ;;
    User) echo "${FAKE_USER-llmagent}" ;;
  esac
fi
exit 0
"""


@pytest.fixture
def hdr_env(tmp_path):
    etc = tmp_path / "etc" / "systemd" / "system"
    etc.mkdir(parents=True)
    vend = tmp_path / "usr" / "lib" / "systemd" / "system"
    vend.mkdir(parents=True)
    binary = tmp_path / "llama-server"
    binary.write_text("x")
    unit_path = etc / HDR_UNIT
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(TMPL.read_text().replace("__UNIT_PATH__", str(unit_path))
                       .replace("__SYSTEMCTL__", str(tmp_path / "fakesystemctl"))
                       .replace("__ETC__", str(etc)))
    fake = tmp_path / "fakesystemctl"
    fake.write_text(HDR_FAKE)
    fake.chmod(0o755)
    log = tmp_path / "log"
    log.write_text("")
    e = dict(os.environ, FAKE_LOG=str(log), FAKE_FRAGMENT=str(unit_path), SUDO_USER="llmagent",
             FAKE_EXEC="{ path=/usr/bin/llama-server ; argv[]=/usr/bin/llama-server }")
    return dict(wrapper=wrapper, env=e, etc=etc, vend=vend, binary=binary,
                unit_path=unit_path, log=log)


def _hdr_run(hdr_env, stdin, **extra):
    return subprocess.run(["bash", str(hdr_env["wrapper"])], input=stdin, text=True,
                          capture_output=True, env=dict(hdr_env["env"], **extra), timeout=30)


def test_legacy_protocol_still_applies_in_place(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    r = _hdr_run(hdr_env, f"{hdr_env['binary']}\n--port\n9931\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
    assert f"ExecStart={hdr_env['binary']} --port 9931" in hdr_env["unit_path"].read_text()
    assert Path(str(hdr_env["unit_path"]) + ".llmsys-bak").read_text() == HDR_ORIG


def test_header_apply_vendor_unit_writes_drop_in(hdr_env):
    vf = hdr_env["vend"] / HDR_UNIT
    vf.write_text(HDR_ORIG)
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n--port\n9931\n", FAKE_FRAGMENT=str(vf))
    assert r.returncode == 0, r.stderr
    assert vf.read_text() == HDR_ORIG
    drop = hdr_env["etc"] / f"{HDR_UNIT}.d" / "50-llm-systems-agent.conf"
    assert drop.read_text() == f"[Service]\nExecStart=\nExecStart={hdr_env['binary']} --port 9931\n"
    assert "daemon-reload" in hdr_env["log"].read_text()


def test_header_apply_etc_unit_edits_in_place_with_backup(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n--port\n9931\n")
    assert r.returncode == 0, r.stderr
    assert f"ExecStart={hdr_env['binary']} --port 9931" in hdr_env["unit_path"].read_text()
    assert Path(str(hdr_env["unit_path"]) + ".llmsys-bak").read_text() == HDR_ORIG
    assert not (hdr_env["etc"] / f"{HDR_UNIT}.d").exists()


def test_restore_removes_drop_in(hdr_env):
    vf = hdr_env["vend"] / HDR_UNIT
    vf.write_text(HDR_ORIG)
    _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n", FAKE_FRAGMENT=str(vf))
    r = _hdr_run(hdr_env, f"op=restore unit={HDR_UNIT}\n", FAKE_FRAGMENT=str(vf))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
    assert not (hdr_env["etc"] / f"{HDR_UNIT}.d").exists()


def test_restore_puts_backup_back(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n--x\n")
    r = _hdr_run(hdr_env, f"op=restore unit={HDR_UNIT}\n")
    assert r.returncode == 0, r.stderr
    assert hdr_env["unit_path"].read_text() == HDR_ORIG
    assert not Path(str(hdr_env["unit_path"]) + ".llmsys-bak").exists()


def test_second_apply_refreshes_backup(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n--x\n")
    first = hdr_env["unit_path"].read_text()
    _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n--y\n")
    assert Path(str(hdr_env["unit_path"]) + ".llmsys-bak").read_text() == first
    r = _hdr_run(hdr_env, f"op=restore unit={HDR_UNIT}\n")
    assert r.returncode == 0 and hdr_env["unit_path"].read_text() == first


def test_restore_dropin_puts_previous_dropin_back(hdr_env):
    vf = hdr_env["vend"] / HDR_UNIT
    vf.write_text(HDR_ORIG)
    drop = hdr_env["etc"] / f"{HDR_UNIT}.d" / "50-llm-systems-agent.conf"
    _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n--x\n", FAKE_FRAGMENT=str(vf))
    first = drop.read_text()
    _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n--y\n", FAKE_FRAGMENT=str(vf))
    assert drop.read_text() != first
    r = _hdr_run(hdr_env, f"op=restore unit={HDR_UNIT}\n", FAKE_FRAGMENT=str(vf))
    assert r.returncode == 0 and drop.read_text() == first
    assert not Path(str(drop) + ".llmsys-bak").exists()


@pytest.mark.parametrize("user,ok", [("", False), ("root", False), ("llmagent", True)])
def test_apply_and_restore_refuse_root_units(hdr_env, user, ok):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n", FAKE_USER=user)
    assert (r.returncode == 0) is ok
    if not ok:
        assert "unit runs as root; refusing to edit" in r.stderr
        r2 = _hdr_run(hdr_env, f"op=restore unit={HDR_UNIT}\n", FAKE_USER=user)
        assert "unit runs as root" in r2.stderr
    r3 = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n", FAKE_USER=user)
    assert r3.returncode == 0


def test_apply_and_restore_refuse_units_of_another_user(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n", FAKE_USER="llama", SUDO_USER="llmagent")
    assert r.returncode != 0 and "runs as llama, not as the agent user llmagent" in r.stderr
    r2 = _hdr_run(hdr_env, f"op=restore unit={HDR_UNIT}\n", FAKE_USER="llama", SUDO_USER="llmagent")
    assert r2.returncode != 0 and "not as the agent user" in r2.stderr
    r3 = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n", FAKE_USER="llama", SUDO_USER="llmagent")
    assert r3.returncode == 0
    # run by root at a terminal (no sudo): the owner check does not apply
    r4 = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n", FAKE_USER="llama", SUDO_USER="")
    assert r4.returncode == 0


def test_restore_with_nothing_dies(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    r = _hdr_run(hdr_env, f"op=restore unit={HDR_UNIT}\n")
    assert r.returncode == 1
    assert "nothing to restore" in r.stderr


def test_restart_calls_systemctl_restart_unit(hdr_env):
    r = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n")
    assert r.returncode == 0, r.stderr
    assert f"restart -- {HDR_UNIT}" in hdr_env["log"].read_text().splitlines()


def test_wrapper_refuses_foreign_unit(hdr_env):
    vf = hdr_env["vend"] / HDR_UNIT
    vf.write_text(HDR_ORIG)
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n", FAKE_FRAGMENT=str(vf),
            FAKE_EXEC="{ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D }")
    assert r.returncode == 1
    assert "not a llama-server or vllm unit" in r.stderr
    assert not (hdr_env["etc"] / f"{HDR_UNIT}.d").exists()
    assert vf.read_text() == HDR_ORIG


@pytest.mark.parametrize("unit", ["../x.service", "foo", "-H.service"])
def test_wrapper_refuses_bad_unit_name(hdr_env, unit):
    r = _hdr_run(hdr_env, f"op=restart unit={unit}\n")
    assert r.returncode == 1
    assert "bad header" in r.stderr


def test_wrapper_refuses_unloaded_unit(hdr_env):
    r = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n", FAKE_LOADSTATE="not-found")
    assert r.returncode == 1
    assert not [ln for ln in hdr_env["log"].read_text().splitlines() if ln.startswith("restart")]


def test_wrapper_refuses_exec_path_not_llama(hdr_env):
    r = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n",
                 FAKE_EXEC="{ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd --vllm }")
    assert r.returncode == 1
    assert "not a llama-server or vllm unit" in r.stderr


def test_wrapper_accepts_python_vllm(hdr_env):
    r = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n",
                 FAKE_EXEC="{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -m vllm serve }")
    assert r.returncode == 0, r.stderr


def test_apply_header_without_binary_dies(hdr_env):
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n")
    assert r.returncode == 1
    assert "empty input" in r.stderr


def test_unit_query_failure_dies_with_message(hdr_env):
    r = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n", FAKE_FAIL="1")
    assert r.returncode == 1
    assert "cannot query unit" in r.stderr


def test_no_etc_in_place_backup_when_no_execstart(hdr_env):
    hdr_env["unit_path"].write_text("[Service]\nUser=x\n")
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n")
    assert r.returncode == 1
    assert not Path(str(hdr_env["unit_path"]) + ".llmsys-bak").exists()


def test_rendered_template_has_no_env_overrides_or_placeholders():
    assert "LLM_SVCCONFIG_" not in TMPL.read_text()
    out = TMPL.read_text().replace("__UNIT_PATH__", "/u").replace(
        "__SYSTEMCTL__", "/usr/bin/systemctl").replace("__ETC__", "/etc/systemd/system")
    assert "__SYSTEMCTL__" not in out and "__ETC__" not in out


def test_header_apply_refuses_binary_outside_allowlist(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    other = hdr_env["binary"].parent / "evil"
    other.write_text("x")
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{other}\n--port\n9931\n")
    assert r.returncode != 0 and "binary must be" in r.stderr
    assert hdr_env["unit_path"].read_text() == HDR_ORIG


def test_header_apply_refuses_symlinked_dropin(hdr_env):
    vf = hdr_env["vend"] / HDR_UNIT
    vf.write_text(HDR_ORIG)
    dropdir = hdr_env["etc"] / f"{HDR_UNIT}.d"
    dropdir.mkdir()
    target = hdr_env["etc"] / "victim.conf"
    target.write_text("keep\n")
    (dropdir / "50-llm-systems-agent.conf").symlink_to(target)
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{hdr_env['binary']}\n", FAKE_FRAGMENT=str(vf))
    assert r.returncode != 0 and "refusing symlink" in r.stderr
    assert target.read_text() == "keep\n"


def test_header_restore_refuses_symlinked_backup(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    target = hdr_env["etc"] / "other.service"
    target.write_text("other\n")
    Path(str(hdr_env["unit_path"]) + ".llmsys-bak").symlink_to(target)
    r = _hdr_run(hdr_env, f"op=restore unit={HDR_UNIT}\n")
    assert r.returncode != 0 and "refusing symlink" in r.stderr
    assert hdr_env["unit_path"].read_text() == HDR_ORIG


def test_wrapper_rejects_python_unit_mentioning_vllm_late(hdr_env):
    r = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n",
                 FAKE_EXEC="{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 app.py --name vllm }")
    assert r.returncode != 0 and "not a llama-server or vllm unit" in r.stderr


def test_wrapper_accepts_vllm_entrypoint_path(hdr_env):
    r = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n",
                 FAKE_EXEC="{ path=/opt/venv/bin/python ; argv[]=/opt/venv/bin/python /opt/venv/bin/vllm serve }")
    assert r.returncode == 0, r.stderr


def test_failed_apply_drops_stale_backup(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    bak = Path(str(hdr_env["unit_path"]) + ".llmsys-bak")
    bak.write_text("[Service]\nUser=x\nExecStart=/bin/stale\n")
    missing = hdr_env["binary"].parent / "gone" / "llama-server"
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{missing}\n--port\n9931\n")
    assert r.returncode != 0 and "binary is not a regular file" in r.stderr
    assert not bak.exists()
    r2 = _hdr_run(hdr_env, f"op=restore unit={HDR_UNIT}\n")
    assert r2.returncode != 0 and "nothing to restore" in r2.stderr
    assert hdr_env["unit_path"].read_text() == HDR_ORIG


def _py_bin(hdr_env, name):
    p = hdr_env["binary"].parent / name
    p.write_text("x")
    return p


def test_header_apply_refuses_python_lookalike(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    py = _py_bin(hdr_env, "python-x")
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{py}\n-m\nvllm\nserve\n")
    assert r.returncode != 0 and "binary must be" in r.stderr
    assert hdr_env["unit_path"].read_text() == HDR_ORIG


def test_header_apply_accepts_python_m_vllm(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    py = _py_bin(hdr_env, "python3")
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{py}\n-m\nvllm\nserve\n")
    assert r.returncode == 0, r.stderr
    assert f"ExecStart={py} -m vllm serve" in hdr_env["unit_path"].read_text()


def test_header_apply_refuses_python_script(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    py = _py_bin(hdr_env, "python3")
    r = _hdr_run(hdr_env, f"op=apply unit={HDR_UNIT}\n{py}\napp.py\n")
    assert r.returncode != 0 and "python binary needs -m vllm or a vllm entrypoint" in r.stderr
    assert hdr_env["unit_path"].read_text() == HDR_ORIG


def test_wrapper_refuses_python_lookalike_unit(hdr_env):
    r = _hdr_run(hdr_env, f"op=restart unit={HDR_UNIT}\n",
                 FAKE_EXEC="{ path=/home/x/python-x ; argv[]=/home/x/python-x -m vllm serve }")
    assert r.returncode != 0 and "not a llama-server or vllm unit" in r.stderr


def test_legacy_protocol_applies_binary_allowlist(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    other = hdr_env["binary"].parent / "evil"
    other.write_text("x")
    r = _hdr_run(hdr_env, f"{other}\n--port\n9931\n")
    assert r.returncode != 0 and "binary must be" in r.stderr
    assert hdr_env["unit_path"].read_text() == HDR_ORIG


def test_legacy_protocol_refuses_root_unit(hdr_env):
    hdr_env["unit_path"].write_text(HDR_ORIG)
    r = _hdr_run(hdr_env, f"{hdr_env['binary']}\n--port\n9931\n", FAKE_USER="root")
    assert r.returncode != 0 and "runs as root" in r.stderr
    assert hdr_env["unit_path"].read_text() == HDR_ORIG
