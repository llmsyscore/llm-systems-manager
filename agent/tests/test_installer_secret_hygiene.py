# agent/tests/test_installer_secret_hygiene.py
# Secrets stay out of process argv (#293), heredoc interpolation (#299),
# and predictable /tmp paths (#294).
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent
AGENT_INSTALL_SH = AGENT_DIR / "install" / "install.sh"
INFLUX_SH = REPO_ROOT / "tools" / "installer" / "install-influxdb.sh"
BOOTSTRAP_SH = REPO_ROOT / "tools" / "installer" / "install-config-bootstrap.sh"
UNIVERSAL_SH = REPO_ROOT / "tools" / "installer" / "install.sh"

TOKEN = "sekrit-bearer-token-0123456789abcdef0123456789"


def _extract_func(source: Path, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", source.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from {source.name}"
    return m.group(0)


# ── #293: agent tarball fetch — token via header file, not argv ─────────────

def test_agent_fetch_keeps_token_out_of_curl_argv(tmp_path):
    install_dir = tmp_path / "inst"
    (install_dir / "data").mkdir(parents=True)
    tok_file = install_dir / "data" / "token"
    tok_file.write_text(TOKEN + "\n")
    (install_dir / "agent_config.yaml").write_text(
        f'MANAGER_URL: "https://mgr.test"\nTOKEN_FILE: "{tok_file}"\n')

    binroot = tmp_path / "bin"
    binroot.mkdir()
    argv_log = tmp_path / "argv.log"
    hdr_log = tmp_path / "hdr.log"
    (binroot / "curl").write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" >> "{argv_log}"\n'
        "prev=\"\"\n"
        'for a in "$@"; do\n'
        f'  [[ "$a" == @* ]] && cat "${{a#@}}" >> "{hdr_log}"\n'
        f'  [[ "$prev" == "-K" || "$prev" == "--config" ]] && cat "$a" >> "{hdr_log}"\n'
        '  prev="$a"\n'
        "done\n"
        "exit 22\n")
    os.chmod(binroot / "curl", 0o755)

    funcs = (_extract_func(AGENT_INSTALL_SH, "_yaml_scalar") + "\n"
             + _extract_func(AGENT_INSTALL_SH, "_fetch_agent_into"))
    user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    script = (f"INSTALL_DIR={install_dir}\n{funcs}\n"
              f'_fetch_agent_into "{tmp_path}/dest" "{user}"\n')
    env = dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode != 0  # stubbed curl fails the download
    argv = argv_log.read_text()
    assert TOKEN not in argv, "bearer token visible in curl argv"
    assert TOKEN in hdr_log.read_text(), "token not passed via header file"


# ── #293: influx wrapper — token via env from file, not argv ────────────────

def test_influx_wrapper_keeps_token_out_of_argv(tmp_path):
    binroot = tmp_path / "bin"
    binroot.mkdir()
    out = tmp_path / "influx.log"
    (binroot / "influx").write_text(
        "#!/bin/bash\n"
        f'{{ echo "ARGV:$*"; echo "ENV:${{INFLUX_TOKEN:-}}"; }} >> "{out}"\n')
    os.chmod(binroot / "influx", 0o755)

    tok_tmp = tmp_path / "tok"
    tok_tmp.write_text(TOKEN + "\n")
    func = _extract_func(INFLUX_SH, "_influx")
    script = (f'SUDO=""\nINFLUX_URL="http://localhost:8086"\n'
              f'INFLUX_OPERATOR_TOKEN="{TOKEN}"\nINFLUX_TOKEN_TMP="{tok_tmp}"\n'
              f"{func}\n_influx bucket list --json\n")
    env = dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}")
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=True)
    log = out.read_text()
    assert TOKEN not in [l for l in log.splitlines() if l.startswith("ARGV:")][0]
    assert f"ENV:{TOKEN.strip()}" in log


def test_influxdb_script_has_no_argv_secrets():
    text = INFLUX_SH.read_text()
    assert '--password "$ADMIN_PASS"' not in text
    assert '--token "$INFLUX_OPERATOR_TOKEN"' not in text
    # Stash writes must pipe via stdin, not expand secrets into bash -c argv.
    assert "printf '%s\\n' '$INFLUX_OPERATOR_TOKEN'" not in text
    assert "'$ADMIN_PASS'" not in text


# ── #299: bootstrap heredoc must not interpolate operator values ────────────

def _bootstrap_writer() -> str:
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", BOOTSTRAP_SH.read_text(), re.S)
    writer = [b for b in blocks if "sub_in_section" in b]
    assert len(writer) == 1, "config-writer heredoc must be quoted (<<'PYEOF')"
    return writer[0]


def test_bootstrap_heredoc_quoted_and_uninterpolated():
    body = _bootstrap_writer()
    assert '"""${' not in body
    assert "${" not in body, "shell interpolation remains in python heredoc"


def test_bootstrap_paste_prompts_are_validated():
    text = BOOTSTRAP_SH.read_text()
    for var in ("MGR_INGEST_TOKEN_PASTE", "MGR_MGMT_TOKEN_PASTE"):
        m = re.search(rf'validate_influx_token[^\n]*"\${var}"', text)
        assert m, f"{var} is never run through validate_influx_token"


def test_bootstrap_writer_treats_hostile_values_as_literals(tmp_path):
    toml = tmp_path / "cfg.toml"
    toml.write_text(
        '[manager]\nhost = "0.0.0.0"\n'
        '[alarm_engine]\nhost = "0.0.0.0"\ningest_token = ""\nmanagement_token = ""\n'
        '[notifications.smtp]\nserver = ""\nuser = ""\npassword = ""\n'
        '[influxdb]\nhost = "localhost"\n[influxdb.tokens]\nmetrics = ""\n'
        'metrics_rollup = ""\nadmin = ""\n')
    pwn = tmp_path / "pwn"
    hostile_pass = 'x"""+__import__("os").system("touch ' + str(pwn) + '")+r"""y'
    vals = {
        "HAS_MGR": "0", "HAS_AE": "1",
        "MGR_HOST": "", "MGR_PORT": "", "MGR_IP": "", "ADMIN_CIDR": "",
        "ADMIN_USER": "", "ADMIN_PW_HASH": "", "AE_HOST": "", "AE_PORT": "",
        "ALARM_ENGINE_URL": "", "MANAGER_URL": "", "SMTP_SERVER": "smtp.test",
        "SMTP_USER": "u", "SMTP_PASS": hostile_pass,
        "INFLUX_METRICS_TOKEN": "", "INFLUX_METRICS_ROLLUP_TOKEN": "",
        "INFLUX_OPERATOR_TOKEN": "", "INGEST_TOKEN": "", "INGEST_COMMENTED": "0",
        "MGR_INGEST_TOKEN_PASTE": "", "MGMT_TOKEN": "", "MGMT_COMMENTED": "0",
        "MGR_MGMT_TOKEN_PASTE": "", "INFLUX_HOSTNAME": "", "INFLUX_PORT": "",
    }
    vals_file = tmp_path / "vals"
    vals_file.write_text("".join(f"{k}={v}\n" for k, v in vals.items()))
    r = subprocess.run(
        ["python3", "-c", _bootstrap_writer(), str(toml), str(vals_file)],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not pwn.exists(), "hostile SMTP password executed as python"
    assert hostile_pass in toml.read_text()


# ── #294: no PID-predictable /tmp paths for root-executed scripts ────────────

def test_universal_installer_has_no_pid_tempfiles():
    hits = [ln for ln in UNIVERSAL_SH.read_text().splitlines()
            if re.search(r'="/tmp/[^"]*\$\$', ln)]
    assert not hits, f"PID-based temp paths remain: {hits}"
