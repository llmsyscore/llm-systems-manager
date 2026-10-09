"""#1247: the managed InfluxDB block no longer delays WAL fsync, and update.sh
rewrites the managed 100ms line in place so existing installs follow."""
import os
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib-common.sh"
SCRIPT = Path(__file__).resolve().parents[1] / "install-influxdb.sh"


def test_install_block_writes_no_fsync_delay():
    assert 'storage-wal-fsync-delay                    = "0s"' in SCRIPT.read_text()
    assert '"100ms"' not in SCRIPT.read_text()


def _apply(tmp_path, text):
    conf = tmp_path / "config.toml"
    conf.write_text(text)
    env = dict(os.environ, LLMSYS_INFLUX_CONF=str(conf))
    r = subprocess.run(["bash", "-c", f'set -euo pipefail\n. "{LIB}"\nSUDO=\n'
                        'install(){ cp "${@: -2:1}" "${@: -1}"; }\n'
                        "apply_influxdb_wal_fsync_delay\n"
                        'echo "changed=$LLMSYS_INFLUX_TUNING_CHANGED"\n'],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout, conf.read_text()


MANAGED = ("bolt-path = \"/x\"\n# === llm-systems-manager tuning (managed) ===\n"
           "# Write path\nstorage-wal-fsync-delay                    = \"100ms\"\n"
           "query-concurrency                          = 2\n"
           "# === END llm-systems-manager tuning ===\n")


def test_managed_100ms_line_is_rewritten_and_flags_a_restart(tmp_path):
    out, text = _apply(tmp_path, MANAGED)
    assert "changed=1" in out
    assert 'storage-wal-fsync-delay                    = "0s"' in text
    assert "100ms" not in text
    assert text.count("\n") == MANAGED.count("\n")


def test_already_current_file_is_left_alone(tmp_path):
    _, first = _apply(tmp_path, MANAGED)
    out, second = _apply(tmp_path, first)
    assert "changed=0" in out and second == first


def test_operator_value_is_preserved(tmp_path):
    custom = MANAGED.replace('"100ms"', '"50ms"')
    out, text = _apply(tmp_path, custom)
    assert "changed=0" in out and text == custom


def test_missing_file_is_a_no_op(tmp_path):
    env = dict(os.environ, LLMSYS_INFLUX_CONF=str(tmp_path / "absent.toml"))
    r = subprocess.run(["bash", "-c", f'set -euo pipefail\n. "{LIB}"\nSUDO=\napply_influxdb_wal_fsync_delay\n'
                        'echo "changed=$LLMSYS_INFLUX_TUNING_CHANGED"'],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "changed=0" in r.stdout
