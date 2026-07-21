"""#462: the managed InfluxDB tuning block scales query-concurrency (and
queue depth) to detected cores instead of hardcoding the 2-core values.
Extracts the real block-writer section from install-influxdb.sh and runs it
against a temp config file."""
import os
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "install-influxdb.sh"


def _run_block(tmp_path, prior="", nproc=None):
    src = SCRIPT.read_text()
    start = src.index('CONF=/etc/influxdb/config.toml')
    end = src.index('ok "tuned config block written', start)
    section = src[start:end]

    conf = tmp_path / "config.toml"
    conf.write_text(prior)
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "SUDO=\nbanner(){ :; }\nok(){ :; }\n"
        # root-less stand-in for `install -o root -g root -m 0644 SRC DST`
        "install(){ cp \"${@: -2:1}\" \"${@: -1}\"; }\n"
        + section.replace("CONF=/etc/influxdb/config.toml", f'CONF="{conf}"')
    )
    env = dict(os.environ)
    if nproc is not None:
        # shim nproc on PATH so core-count is deterministic in the test
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        shim = bindir / "nproc"
        shim.write_text(f"#!/usr/bin/env bash\necho {nproc}\n")
        shim.chmod(0o755)
        env["PATH"] = f"{bindir}:{env['PATH']}"
    r = subprocess.run(["bash", str(harness)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return conf.read_text()


def _val(text, key):
    m = re.search(rf"^{re.escape(key)}\s*=\s*(\S+)", text, re.M)
    return m.group(1) if m else None


def test_scales_to_detected_cores(tmp_path):
    out = _run_block(tmp_path, nproc=6)
    assert _val(out, "query-concurrency") == "6"
    assert _val(out, "query-queue-size") == "96"


def test_two_core_host_matches_legacy_values(tmp_path):
    out = _run_block(tmp_path, nproc=2)
    assert _val(out, "query-concurrency") == "2"
    assert _val(out, "query-queue-size") == "32"


def test_single_core_floors_at_two(tmp_path):
    out = _run_block(tmp_path, nproc=1)
    assert _val(out, "query-concurrency") == "2"
    assert _val(out, "query-queue-size") == "32"


def test_static_values_unchanged(tmp_path):
    out = _run_block(tmp_path, nproc=8)
    assert _val(out, "storage-cache-snapshot-memory-size") == "134217728"
    assert _val(out, "query-memory-bytes") == "268435456"
    assert _val(out, "storage-max-concurrent-compactions") == "1"


def test_idempotent_rerun_single_block(tmp_path):
    first = _run_block(tmp_path, nproc=4)
    second = _run_block(tmp_path, prior=first, nproc=4)
    assert second.count("=== llm-systems-manager tuning (managed) ===") == 1
    assert second.count("query-concurrency") == 1


def test_operator_lines_outside_markers_preserved(tmp_path):
    prior = "bolt-path = \"/var/lib/influxdb/influxd.bolt\"\ncustom-key = 7\n"
    out = _run_block(tmp_path, prior=prior, nproc=4)
    assert "custom-key = 7" in out
    assert _val(out, "query-concurrency") == "4"
