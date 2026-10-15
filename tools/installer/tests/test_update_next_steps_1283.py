"""#1283: update.sh ends with a Next steps block that names the host-by-host
actions still owed after the update, adjusted to what runs on this host."""
import os
import subprocess
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1]
UPDATE = INSTALLER / "update.sh"
INSTALL = INSTALLER / "install.sh"
ROLE_SAN = "DNS:alarm-engine.role.llmsys.internal"


def _next_steps_block():
    """The function definitions between the Next steps and Closing banner markers, verbatim."""
    text = UPDATE.read_text()
    start = text.index("# ── Next steps ──")
    return text[start:text.index("# ── Closing banner ──", start)]


def _layout(tmp_path, toml="", crt=True, ae_dir=False):
    root = tmp_path / "opt"
    (root / "config").mkdir(parents=True)
    (root / "config" / "llm-systems.toml").write_text(toml)
    (root / "data").mkdir()
    if crt:
        (root / "data" / "ae-tls.crt").write_text("cert\n")
    if ae_dir:
        (root / "llm-systems-alarm-engine" / "data").mkdir(parents=True)
        (root / "llm-systems-alarm-engine" / "data" / "ae-tls.crt").write_text("cert\n")
    return root


def _stub_bin(tmp_path, openssl_san=None):
    """sudo passes through; openssl prints a SAN block, or fails when no SAN is given."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "sudo").write_text('#!/usr/bin/env bash\nexec "$@"\n')
    (bin_dir / "sudo").chmod(0o755)
    body = (f'echo "X509v3 Subject Alternative Name:"; echo "    DNS:localhost, {openssl_san}"\n'
            if openssl_san else "exit 1\n")
    (bin_dir / "openssl").write_text("#!/usr/bin/env bash\n" + body)
    (bin_dir / "openssl").chmod(0o755)
    return bin_dir


def _run(tmp_path, root, flags, openssl_san=None):
    bin_dir = _stub_bin(tmp_path, openssl_san)
    script = (f'set -euo pipefail\n. "{INSTALLER / "lib-common.sh"}"\n'
              f'LLMSYS_INSTALL_DIR="{root}"\nLLMSYS_RUN_USER=llmsys; LLMSYS_RUN_GROUP=llmsys\n'
              "HAVE_MANAGER=false; HAVE_AE=false; HAVE_INFLUX=false; HAVE_AGENT=false\n"
              "_RESTART_DEFERRED=0; _DEFERRED_UNITS=(); _MANAGER_RESTARTED=0; _AE_CRT_MTIME_BEFORE=0\n"
              "TEMP_ADMIN=''; FAIL=0\n"
              f"{flags}\n" + _next_steps_block() + "\ncollect_next_steps\nprint_next_steps\n")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SUDO="sudo")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


SPLIT_MGR_TOML = ('[manager]\nhost = "0.0.0.0"\nalarm_engine_url = "https://ae.example:8081"\n'
                  '[influxdb]\nhost = "db.example"\n')
SPLIT_AE_TOML = ('[alarm_engine]\nhost = "0.0.0.0"\nmanager_url = "https://mgr.example:5000"\n'
                 '[influxdb]\nhost = "db.example"\n')


def test_manager_host_with_reissued_certificate_says_to_copy_it(tmp_path):
    root = _layout(tmp_path, SPLIT_MGR_TOML)
    out = _run(tmp_path, root, "HAVE_MANAGER=true; _MANAGER_RESTARTED=1; _AE_CRT_MTIME_BEFORE=1")
    assert "Next steps" in out
    assert "issued a new alarm engine certificate" in out
    assert "ae.example" in out and f"{root}/data/ae-tls.crt" in out
    assert f"{root}/llm-systems-alarm-engine/data/" in out
    assert "sudo chown llmsys:llmsys" in out and "systemctl restart llm-systems-alarm-engine" in out


def test_manager_host_whose_certificate_did_not_change_has_no_copy_step(tmp_path):
    root = _layout(tmp_path, SPLIT_MGR_TOML)
    mtime = int((root / "data" / "ae-tls.crt").stat().st_mtime)
    out = _run(tmp_path, root, f"HAVE_MANAGER=true; _MANAGER_RESTARTED=1; _AE_CRT_MTIME_BEFORE={mtime}")
    assert "certificate" not in out.lower()


def test_manager_host_with_a_pending_restart_tells_how_to_check_for_a_new_certificate(tmp_path):
    root = _layout(tmp_path, SPLIT_MGR_TOML)
    mtime = int((root / "data" / "ae-tls.crt").stat().st_mtime)
    out = _run(tmp_path, root, f"HAVE_MANAGER=true; _AE_CRT_MTIME_BEFORE={mtime}; "
               "_RESTART_DEFERRED=1; _DEFERRED_UNITS=(llm-systems-manager.service)")
    assert "After the manager restarts" in out and "journalctl -u llm-systems-manager" in out
    assert f"{root}/llm-systems-alarm-engine/data/" in out


def test_manager_host_with_nothing_to_restart_has_no_certificate_step(tmp_path):
    root = _layout(tmp_path, SPLIT_MGR_TOML)
    mtime = int((root / "data" / "ae-tls.crt").stat().st_mtime)
    out = _run(tmp_path, root, f"HAVE_MANAGER=true; _AE_CRT_MTIME_BEFORE={mtime}")
    assert "certificate" not in out.lower()
    out = _run(tmp_path, root, f"HAVE_MANAGER=true; _AE_CRT_MTIME_BEFORE={mtime}; "
               "_RESTART_DEFERRED=1; _DEFERRED_UNITS=(llm-systems-agent.service)")
    assert "certificate" not in out.lower()


def test_manager_host_whose_certificate_did_not_exist_before_says_to_copy_it(tmp_path):
    root = _layout(tmp_path, SPLIT_MGR_TOML)
    out = _run(tmp_path, root, "HAVE_MANAGER=true; _MANAGER_RESTARTED=1; _AE_CRT_MTIME_BEFORE=0")
    assert "issued a new alarm engine certificate" in out


def test_influxdb_host_comes_from_the_influxdb_section_only(tmp_path):
    root = _layout(tmp_path, '[manager]\nhost = "0.0.0.0"\n[alarm_engine]\nhost = "127.0.0.1"\n')
    out = _run(tmp_path, root, "HAVE_MANAGER=true; HAVE_AE=true; _MANAGER_RESTARTED=1")
    assert "InfluxDB host (<influxdb-host>)" in out


def test_manager_host_points_at_the_other_hosts_and_update_all(tmp_path):
    root = _layout(tmp_path, SPLIT_MGR_TOML)
    out = _run(tmp_path, root, "HAVE_MANAGER=true; _MANAGER_RESTARTED=1; _AE_CRT_MTIME_BEFORE=1")
    lines = [ln for ln in out.splitlines() if ln.strip()[:2].rstrip(".").isdigit()]
    text = "\n".join(lines)
    assert "InfluxDB host (db.example)" in out and "install.sh --update" in out
    assert "alarm engine host (ae.example)" in out
    assert "Update all" in out
    # Order: tuning, certificate, alarm engine host update, agents.
    assert text.index("InfluxDB") < text.index("certificate") < text.index("Update the alarm engine host") < text.index("Update all")


def test_alarm_engine_host_without_role_name_says_to_install_the_certificate(tmp_path):
    root = _layout(tmp_path, SPLIT_AE_TOML, crt=False, ae_dir=True)
    out = _run(tmp_path, root, "HAVE_AE=true", openssl_san="DNS:ae.example")
    assert "no role name yet" in out and "mgr.example" in out
    assert f"{root}/llm-systems-alarm-engine/data/" in out
    assert "systemctl restart llm-systems-alarm-engine" in out
    assert "Update all" in out and "InfluxDB host (db.example)" in out


def test_alarm_engine_host_with_role_name_has_no_certificate_step(tmp_path):
    root = _layout(tmp_path, SPLIT_AE_TOML, crt=False, ae_dir=True)
    out = _run(tmp_path, root, "HAVE_AE=true", openssl_san=ROLE_SAN)
    assert "certificate" not in out.lower()
    assert "Update all" in out


def test_alarm_engine_host_whose_certificate_cannot_be_read_prints_the_conditional_step(tmp_path):
    root = _layout(tmp_path, SPLIT_AE_TOML, crt=False, ae_dir=True)
    out = _run(tmp_path, root, "HAVE_AE=true")
    assert "If the manager host issued a new alarm engine certificate" in out
    assert "mgr.example" in out


def test_all_in_one_host_only_lists_the_agents(tmp_path):
    root = _layout(tmp_path, 'alarm_engine_url = "http://localhost:8081"\n')
    out = _run(tmp_path, root, "HAVE_MANAGER=true; HAVE_AE=true; HAVE_INFLUX=true; HAVE_AGENT=true; _MANAGER_RESTARTED=1")
    assert "Update all" in out
    assert "certificate" not in out.lower() and "InfluxDB host" not in out


def test_manager_and_alarm_engine_host_with_remote_influxdb_points_at_it(tmp_path):
    root = _layout(tmp_path, '[influxdb]\nhost = "db.example"\n')
    out = _run(tmp_path, root, "HAVE_MANAGER=true; HAVE_AE=true; _MANAGER_RESTARTED=1")
    assert "InfluxDB host (db.example)" in out and "certificate" not in out.lower()


def test_agent_only_host_says_nothing_more_is_owed(tmp_path):
    root = _layout(tmp_path, "", crt=False)
    out = _run(tmp_path, root, "HAVE_AGENT=true")
    assert "Nothing more on this host" in out and "Update all" not in out


def test_deferred_restarts_come_first(tmp_path):
    root = _layout(tmp_path, SPLIT_MGR_TOML)
    out = _run(tmp_path, root, "HAVE_MANAGER=true; _RESTART_DEFERRED=1; "
               "_DEFERRED_UNITS=(llm-systems-manager.service); _AE_CRT_MTIME_BEFORE=1")
    assert out.index("sudo systemctl restart llm-systems-manager.service") < out.index("InfluxDB host")


def test_temporary_admin_password_is_a_next_step(tmp_path):
    root = _layout(tmp_path, "")
    out = _run(tmp_path, root, "HAVE_MANAGER=true; HAVE_AE=true; HAVE_INFLUX=true; _MANAGER_RESTARTED=1; "
               "TEMP_ADMIN='admin s3cret'")
    assert "Sign in as:" in out and "s3cret" in out
    assert out.index("Sign in as:") < out.index("Update all")


def _unit_in_tree_fn():
    text = UPDATE.read_text()
    start = text.index("_unit_in_tree() {")
    return text[start:text.index("\n", start) + 1]


def test_a_unit_counts_only_when_it_runs_out_of_the_install_dir(tmp_path):
    unit = tmp_path / "llm-systems-manager.service"
    unit.write_text("[Service]\nWorkingDirectory=/srv/mgr/llm-systems-manager\n"
                    "ExecStart=/srv/mgr/llm-systems-manager/venv/bin/python3 backend/llm-systems-manager.py\n")
    for install_dir, want in (("/srv/mgr", 0), ("/srv/mgr-ae", 1), ("/srv/other", 1)):
        r = subprocess.run(["bash", "-c", f'LLMSYS_INSTALL_DIR="{install_dir}"\n' + _unit_in_tree_fn()
                            + f'\n_unit_in_tree "{unit}"'], capture_output=True, text=True)
        assert r.returncode == want, (install_dir, r.stderr)
    r = subprocess.run(["bash", "-c", 'LLMSYS_INSTALL_DIR=/srv/mgr\n' + _unit_in_tree_fn()
                        + f'\n_unit_in_tree "{tmp_path}/missing.service"'], capture_output=True, text=True)
    assert r.returncode == 1


def test_install_sh_mode_3_next_steps_name_the_certificate_copy():
    text = INSTALL.read_text()
    start = text.index('echo "  Next steps"')
    block = text[start:text.index('echo "  Operations"', start)]
    mode3 = block[block.index("  3)\n"):block.index("  4)\n")]
    assert "ae-tls.{crt,key}" in mode3 and "llm-systems-alarm-engine/data/" in mode3
    assert "systemctl restart llm-systems-alarm-engine" in mode3
