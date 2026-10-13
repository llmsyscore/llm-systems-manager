"""#1263: the managed InfluxDB block snapshots the write cache at 16 MiB instead of
128 MiB, and the tuning refresh brings existing installs' snapshot size and query
concurrency up to date in place."""
import os
import subprocess
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1]
LIB = INSTALLER / "lib-common.sh"
SCRIPT = INSTALLER / "install-influxdb.sh"
TUNE = INSTALLER / "tune-influxdb.sh"
UPDATE = INSTALLER / "update.sh"

BEGIN = "# === llm-systems-manager tuning (managed) ==="
END = "# === END llm-systems-manager tuning ==="
MANAGED = (f'bolt-path = "/x"\nquery-concurrency                          = 1\n{BEGIN}\n'
           "# Cache\nstorage-cache-snapshot-memory-size         = 134217728\n"
           "# Queries (concurrency scaled to 2 detected cores)\n"
           "query-concurrency                          = 2\n"
           "query-queue-size                           = 32\n"
           "query-memory-bytes                         = 268435456\n"
           f"{END}\n")


def test_install_block_snapshots_at_16_mib():
    text = SCRIPT.read_text()
    assert "storage-cache-snapshot-memory-size         = 16777216" in text
    assert "134217728" not in text


def test_refresh_paths_call_both_helpers():
    for path in (TUNE, UPDATE):
        text = path.read_text()
        assert "apply_influxdb_cache_snapshot_size" in text, path.name
        assert "apply_influxdb_query_concurrency" in text, path.name


def _apply(tmp_path, text, fn, nproc=None):
    conf = tmp_path / "config.toml"
    conf.write_text(text)
    env = dict(os.environ, LLMSYS_INFLUX_CONF=str(conf))
    if nproc is not None:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        shim = bindir / "nproc"
        shim.write_text(f"#!/usr/bin/env bash\necho {nproc}\n")
        shim.chmod(0o755)
        env["PATH"] = f"{bindir}:{env['PATH']}"
    r = subprocess.run(["bash", "-c", f'set -euo pipefail\n. "{LIB}"\nSUDO=\n'
                        'install(){ cp "${@: -2:1}" "${@: -1}"; }\n'
                        f"{fn}\n"
                        'echo "changed=$LLMSYS_INFLUX_TUNING_CHANGED"\n'],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout, conf.read_text()


def test_managed_128_mib_snapshot_is_rewritten(tmp_path):
    out, text = _apply(tmp_path, MANAGED, "apply_influxdb_cache_snapshot_size")
    assert "changed=1" in out
    assert "storage-cache-snapshot-memory-size         = 16777216" in text
    assert "134217728" not in text
    assert text.count("\n") == MANAGED.count("\n")


def test_snapshot_operator_value_is_preserved(tmp_path):
    custom = MANAGED.replace("134217728", "67108864")
    out, text = _apply(tmp_path, custom, "apply_influxdb_cache_snapshot_size")
    assert "changed=0" in out and text == custom


def test_snapshot_outside_the_block_is_the_operators(tmp_path):
    outside = f'storage-cache-snapshot-memory-size         = 134217728\n{BEGIN}\nquery-concurrency                          = 2\n{END}\n'
    out, text = _apply(tmp_path, outside, "apply_influxdb_cache_snapshot_size")
    assert "changed=0" in out and text == outside


def test_query_queue_drift_alone_is_corrected(tmp_path):
    drifted = MANAGED.replace("query-queue-size                           = 32", "query-queue-size                           = 8")
    out, text = _apply(tmp_path, drifted, "apply_influxdb_query_concurrency", nproc=2)
    assert "changed=1" in out
    assert "query-queue-size                           = 32\n" in text
    assert "query-concurrency                          = 2\n" in text


def test_query_concurrency_follows_the_host_cores(tmp_path):
    out, text = _apply(tmp_path, MANAGED, "apply_influxdb_query_concurrency", nproc=8)
    assert "changed=1" in out
    assert "# Queries (concurrency scaled to 8 detected cores)" in text
    assert "query-concurrency                          = 8\n" in text
    assert "query-queue-size                           = 128\n" in text
    # the operator's own line outside the managed block is untouched
    assert text.startswith('bolt-path = "/x"\nquery-concurrency                          = 1\n')
    assert text.count("\n") == MANAGED.count("\n")


def test_query_concurrency_floor_is_two(tmp_path):
    out, text = _apply(tmp_path, MANAGED.replace("= 2\n", "= 4\n"), "apply_influxdb_query_concurrency", nproc=1)
    assert "changed=1" in out
    assert "query-concurrency                          = 2\n" in text
    assert "query-queue-size                           = 32\n" in text


def test_query_concurrency_already_current_is_a_no_op(tmp_path):
    out, text = _apply(tmp_path, MANAGED, "apply_influxdb_query_concurrency", nproc=2)
    assert "changed=0" in out and text == MANAGED


def test_block_without_query_lines_is_left_alone(tmp_path):
    plain = f'{BEGIN}\nstorage-wal-fsync-delay                    = "0s"\n{END}\n'
    out, text = _apply(tmp_path, plain, "apply_influxdb_query_concurrency", nproc=8)
    assert "changed=0" in out and text == plain


def test_missing_file_is_a_no_op(tmp_path):
    env = dict(os.environ, LLMSYS_INFLUX_CONF=str(tmp_path / "absent.toml"))
    r = subprocess.run(["bash", "-c", f'set -euo pipefail\n. "{LIB}"\nSUDO=\n'
                        "apply_influxdb_cache_snapshot_size\napply_influxdb_query_concurrency\n"
                        'echo "changed=$LLMSYS_INFLUX_TUNING_CHANGED"'],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "changed=0" in r.stdout
