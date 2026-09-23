"""#1073/#1074: influxdb OOM drop-in + host-sized GOMEMLIMIT block from lib-common.sh."""
import os
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib-common.sh"
BEGIN = "# === llm-systems-manager memory (managed) ==="
END = "# === END llm-systems-manager memory ==="


def _bash(script, env=None):
    r = subprocess.run(["bash", "-c", f'. "{LIB}"\n{script}'],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _gib(n):
    return int(n * 1024 * 1024)


def test_sizing():
    got = {g: int(_bash(f"influx_gomemlimit_mib {_gib(g)}")) for g in (1, 2, 4, 6, 16)}
    assert got[1] == 1024 and got[2] == 1024      # floor
    assert got[4] == 1600                           # 4 GiB+ floor
    assert got[6] == 2150                           # 35%
    assert got[16] == 5734


def _apply(tmp_path, colocated=1, env_text=None, mem_gib=6):
    dropdir = tmp_path / "influxdb.service.d"
    envf = tmp_path / "influxdb2"
    if env_text is not None:
        envf.write_text(env_text)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal:       {_gib(mem_gib)} kB\n")
    env = dict(os.environ, LLMSYS_INFLUX_DROPIN_DIR=str(dropdir),
               LLMSYS_INFLUX_ENV_FILE=str(envf), LLMSYS_MEMINFO=str(meminfo))
    out = _bash(
        "SUDO=\n"
        'install(){ if [[ "$1" == -d ]]; then mkdir -p "${@: -1}"; else cp "${@: -2:1}" "${@: -1}"; fi; }\n'
        "systemctl(){ echo RELOAD >&2; }\n"
        f"apply_influxdb_host_tuning {colocated}\n"
        'echo "changed=$LLMSYS_INFLUX_TUNING_CHANGED"\n'
        'echo "value=$LLMSYS_INFLUX_GOMEMLIMIT"\n', env=env)
    return out, dropdir / "llm-systems-manager.conf", envf


def test_colocated_writes_dropin_and_block(tmp_path):
    out, dropin, envf = _apply(tmp_path, env_text="INFLUXD_CONFIG_PATH=/etc/influxdb/config.toml\n")
    assert "changed=1" in out and "value=2150MiB" in out
    assert dropin.read_text() == "[Service]\nOOMScoreAdjust=-500\n"
    assert envf.read_text() == (
        f"INFLUXD_CONFIG_PATH=/etc/influxdb/config.toml\n{BEGIN}\nGOMEMLIMIT=2150MiB\n{END}\n")


def test_rerun_is_idempotent(tmp_path):
    _apply(tmp_path, env_text="A=1\n")
    first = (tmp_path / "influxdb2").read_text()
    out, _, envf = _apply(tmp_path)
    assert "changed=0" in out
    assert envf.read_text() == first


def test_rerun_resizes_block(tmp_path):
    _apply(tmp_path, env_text="A=1\n", mem_gib=6)
    out, _, envf = _apply(tmp_path, mem_gib=16)
    assert "changed=1" in out
    assert envf.read_text() == f"A=1\n{BEGIN}\nGOMEMLIMIT=5734MiB\n{END}\n"


def test_operator_override_preserved(tmp_path):
    out, _, envf = _apply(tmp_path, env_text="GOMEMLIMIT=2048MiB\n")
    assert "value=2048MiB (operator setting" in out
    assert envf.read_text() == "GOMEMLIMIT=2048MiB\n"


def test_operator_override_drops_stale_block(tmp_path):
    _apply(tmp_path, env_text="A=1\n")
    text = (tmp_path / "influxdb2").read_text() + "GOMEMLIMIT=3GiB\n"
    out, _, envf = _apply(tmp_path, env_text=text)
    assert BEGIN not in envf.read_text()
    assert "GOMEMLIMIT=3GiB" in envf.read_text()


def test_dedicated_host_has_no_block(tmp_path):
    _apply(tmp_path, env_text="A=1\n")
    out, dropin, envf = _apply(tmp_path, colocated=0)
    assert "value=unset (dedicated InfluxDB host)" in out
    assert envf.read_text() == "A=1\n"
    assert dropin.exists()


def test_missing_env_file_created(tmp_path):
    out, _, envf = _apply(tmp_path, env_text=None, mem_gib=2)
    assert envf.read_text() == f"{BEGIN}\nGOMEMLIMIT=1024MiB\n{END}\n"


def test_block_only_file_rerun_is_idempotent(tmp_path):
    _apply(tmp_path, env_text=None, mem_gib=2)
    out, _, envf = _apply(tmp_path, mem_gib=2)
    assert "changed=0" in out
    assert envf.read_text() == f"{BEGIN}\nGOMEMLIMIT=1024MiB\n{END}\n"


def test_dedicated_missing_env_file_not_created(tmp_path):
    out, _, envf = _apply(tmp_path, colocated=0, env_text=None)
    assert not envf.exists()
