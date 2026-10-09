"""#1249: tune-influxdb.sh refreshes the InfluxDB host tuning on its own, so
update.sh on an InfluxDB-only host and the deb/rpm upgrade can run it."""
import os
import subprocess
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1]
SCRIPT = INSTALLER / "tune-influxdb.sh"
UPDATE = INSTALLER / "update.sh"
COMMON = INSTALLER.parents[0] / "packaging" / "scripts" / "common.sh"
MEM_BEGIN = "# === llm-systems-manager memory (managed) ==="
MEM_END = "# === END llm-systems-manager memory ==="
CONF_100MS = ('bolt-path = "/x"\n# === llm-systems-manager tuning (managed) ===\n'
              'storage-wal-fsync-delay                    = "100ms"\n'
              "# === END llm-systems-manager tuning ===\n")


def _stub_bin(tmp_path, units):
    """A PATH dir whose systemctl lists <units> and records every other call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "systemctl.calls"
    unit_lines = "".join(f"{u} enabled enabled\n" for u in units)
    (bin_dir / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "$1" == list-unit-files ]]; then printf %s "{unit_lines}"; exit 0; fi\n'
        f'echo "$*" >> "{calls}"\n')
    (bin_dir / "systemctl").chmod(0o755)
    (bin_dir / "curl").write_text("#!/usr/bin/env bash\necho 200\n")
    (bin_dir / "curl").chmod(0o755)
    (bin_dir / "sudo").write_text('#!/usr/bin/env bash\nexec "$@"\n')
    (bin_dir / "sudo").chmod(0o755)
    (bin_dir / "install").write_text(
        "#!/usr/bin/env bash\nargs=()\n"
        'while (( $# )); do case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done\n'
        'exec /usr/bin/install "${args[@]}"\n')
    (bin_dir / "install").chmod(0o755)
    return bin_dir, calls


def _layout(tmp_path, conf=CONF_100MS, env_text=""):
    conf_path = tmp_path / "config.toml"
    conf_path.write_text(conf)
    envf = tmp_path / "influxdb2"
    envf.write_text(env_text)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       6291456 kB\n")
    return conf_path, envf, tmp_path / "influxdb.service.d"


def _run(tmp_path, units, args=(), stdin_tty=False):
    bin_dir, calls = _stub_bin(tmp_path, units)
    conf, envf, dropdir = _layout(tmp_path)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               LLMSYS_INFLUX_CONF=str(conf), LLMSYS_INFLUX_ENV_FILE=str(envf),
               LLMSYS_INFLUX_DROPIN_DIR=str(dropdir), LLMSYS_MEMINFO=str(tmp_path / "meminfo"),
               LLMSYS_INFLUX_HEALTH_WAIT="1")
    r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                       env=env, stdin=subprocess.DEVNULL)
    return r, conf, envf, dropdir / "llm-systems-manager.conf", calls


def test_script_is_a_standalone_bash_script():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert '. "$HERE/lib-common.sh"' in text


def test_no_local_influxdb_is_a_no_op(tmp_path):
    r, conf, envf, dropin, calls = _run(tmp_path, ["llm-systems-manager.service"])
    assert r.returncode == 0, r.stderr
    assert "no local influxdb.service" in r.stdout
    assert conf.read_text() == CONF_100MS and not dropin.exists()


def test_db_only_host_gets_dropin_and_fsync_but_no_gomemlimit(tmp_path):
    r, conf, envf, dropin, calls = _run(tmp_path, ["influxdb.service"])
    assert r.returncode == 0, r.stderr
    assert dropin.read_text() == "[Service]\nOOMScoreAdjust=-500\n"
    assert '"0s"' in conf.read_text() and "100ms" not in conf.read_text()
    assert "GOMEMLIMIT" not in envf.read_text()
    assert "restart influxdb" in r.stderr
    assert not calls.exists() or "restart" not in calls.read_text()


def test_colocated_host_gets_gomemlimit(tmp_path):
    r, conf, envf, dropin, calls = _run(
        tmp_path, ["influxdb.service", "llm-systems-alarm-engine.service"])
    assert r.returncode == 0, r.stderr
    assert envf.read_text() == f"{MEM_BEGIN}\nGOMEMLIMIT=2150MiB\n{MEM_END}\n"


def test_restart_flag_restarts_only_after_a_change(tmp_path):
    r, conf, envf, dropin, calls = _run(tmp_path, ["influxdb.service"], ["--restart"])
    assert r.returncode == 0, r.stderr
    assert "restart influxdb" in calls.read_text()
    calls.unlink()
    r2 = subprocess.run(["bash", str(SCRIPT), "--restart"], capture_output=True, text=True,
                        env=dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}",
                                 LLMSYS_INFLUX_CONF=str(conf), LLMSYS_INFLUX_ENV_FILE=str(envf),
                                 LLMSYS_INFLUX_DROPIN_DIR=str(tmp_path / "influxdb.service.d"),
                                 LLMSYS_MEMINFO=str(tmp_path / "meminfo")),
                        stdin=subprocess.DEVNULL)
    assert r2.returncode == 0, r2.stderr
    assert "already current" in r2.stdout and not calls.exists()


def test_dry_run_changes_nothing(tmp_path):
    r, conf, envf, dropin, calls = _run(tmp_path, ["influxdb.service"], ["--dry-run"])
    assert r.returncode == 0, r.stderr
    assert conf.read_text() == CONF_100MS and not dropin.exists()


def _update_sh_discovery_block():
    """The `if ! $HAVE_MANAGER && ! $HAVE_AE && ! $HAVE_AGENT` block of update.sh, verbatim."""
    text = UPDATE.read_text()
    start = text.index("if ! $HAVE_MANAGER && ! $HAVE_AE && ! $HAVE_AGENT; then")
    return text[start:text.index("\nfi\n", start) + 4]


def _run_update_block(tmp_path, influx, pkg_agent, flags=""):
    calls = tmp_path / "tune.calls"
    calls.unlink(missing_ok=True)
    (tmp_path / "tune-influxdb.sh").write_text(f'#!/usr/bin/env bash\necho "tune${{*:+ $*}}" >> "{calls}"\n')
    script = (f'set -euo pipefail\n. "{INSTALLER / "lib-common.sh"}"\nTHIS_DIR="{tmp_path}"\n'
              f"HAVE_MANAGER=false; HAVE_AE=false; HAVE_AGENT=false; HAVE_INFLUX={influx}\n"
              f"_AGENT_PKG_SKIPPED={pkg_agent}\nDRY_RUN=0; SKIP_RESTART=0; ASSUME_YES=0\n{flags}\n"
              + _update_sh_discovery_block() + '\necho "fell through"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r, (calls.read_text() if calls.exists() else "")


def test_update_sh_tunes_an_influxdb_only_host_instead_of_dying(tmp_path):
    r, calls = _run_update_block(tmp_path, "true", "false", "ASSUME_YES=1")
    assert r.returncode == 0, r.stderr
    assert calls == "tune --restart\n" and "fell through" not in r.stdout
    assert "nothing to update" not in r.stdout + r.stderr


def test_update_sh_maps_its_flags_onto_the_tune_script(tmp_path):
    assert _run_update_block(tmp_path, "true", "false", "SKIP_RESTART=1; ASSUME_YES=1")[1] == "tune --no-restart\n"
    assert _run_update_block(tmp_path, "true", "false", "DRY_RUN=1")[1] == "tune --dry-run\n"
    assert _run_update_block(tmp_path, "true", "false")[1] == "tune\n"


def test_update_sh_still_reports_a_package_managed_agent(tmp_path):
    r, calls = _run_update_block(tmp_path, "true", "true", "ASSUME_YES=1")
    assert r.returncode == 0 and "package-managed; use apt/dnf" in r.stdout and calls == "tune --restart\n"
    r, calls = _run_update_block(tmp_path, "false", "true")
    assert r.returncode == 0 and "package-managed; use apt/dnf" in r.stdout and calls == ""


def test_update_sh_still_dies_with_nothing_installed(tmp_path):
    r, calls = _run_update_block(tmp_path, "false", "false")
    assert r.returncode == 1 and "nothing to update" in r.stderr and calls == ""


def _run_configure(tmp_path, units, arg):
    bin_dir, calls = _stub_bin(tmp_path, units)
    root = tmp_path / "opt"
    tune = root / "tools" / "installer" / "tune-influxdb.sh"
    tune.parent.mkdir(parents=True)
    tune.write_text(f'#!/usr/bin/env bash\necho "tune $*" >> "{calls}"\n')
    stubs = " ".join(f"{f}(){{ :; }};" for f in (
        "llmsys_create_user", "llmsys_build_venvs", "llmsys_write_config", "llmsys_write_marker",
        "llmsys_warn_if_shadowed", "llmsys_enable_start", "llmsys_influx_notice", "llmsys_admin_signin",
        "llmsys_restart_upgraded", "install", "chown"))
    script = (f'. "{COMMON}"; LLMSYS_INSTALL_DIR="{root}"; LLMSYS_PKG_MARKER="{root}/marker"; '
              f'LLMSYS_CFG="{root}/cfg"; LLMSYS_LOG_DIR="{root}/log"; LLMSYS_RUN_USER="$(id -un)"; '
              f'llmsys_run_group(){{ id -gn; }}; llmsys_systemd_ready(){{ return 0; }}; {stubs} '
              f'llmsys_configure "{arg}"')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}"))
    assert r.returncode == 0, r.stderr
    return calls.read_text() if calls.exists() else ""


def test_package_upgrade_runs_the_tune_script_when_influxdb_is_local(tmp_path):
    assert _run_configure(tmp_path, ["influxdb.service"], "upgrade") == "tune --restart\n"


def test_package_fresh_install_does_not_run_the_tune_script(tmp_path):
    assert _run_configure(tmp_path, ["influxdb.service"], "") == ""


def test_package_upgrade_skips_the_tune_script_without_local_influxdb(tmp_path):
    bin_dir, calls = _stub_bin(tmp_path, ["llm-systems-manager.service"])
    root = tmp_path / "opt"
    tune = root / "tools" / "installer" / "tune-influxdb.sh"
    tune.parent.mkdir(parents=True)
    tune.write_text(f'#!/usr/bin/env bash\necho "tune $*" >> "{calls}"\n')
    script = (f'. "{COMMON}"; LLMSYS_INSTALL_DIR="{root}"; '
              'llmsys_systemd_ready(){ return 0; }; llmsys_influx_tune')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}"))
    assert r.returncode == 0, r.stderr
    assert not calls.exists()
