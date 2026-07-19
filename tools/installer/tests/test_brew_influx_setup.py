"""#437: brew-influx-setup.sh — fresh-install provisioning end to end.

Runs the real script against a config seeded from the tracked example, with
`influx` + `curl` stubbed on PATH, and asserts the [influxdb.tokens] rewrite.
Using the real example means .example drift breaks this test loudly.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1]
REPO_ROOT = INSTALLER.parents[1]
SCRIPT = INSTALLER / "brew-influx-setup.sh"
EXAMPLE = REPO_ROOT / "config" / "llm-systems.toml.example"

INFLUX_STUB = """#!/usr/bin/env bash
# Stub influx CLI: rollup bucket exists only after create; auth create
# returns a token derived from the bucket id. Argv is logged so the test
# can assert no secret ever reaches a command line.
state="$STUB_STATE"
echo "$*" >> "$state/influx_argv.log"
case "$1 $2" in
  "bucket list")
    name=""
    prev=""
    for a in "$@"; do [ "$prev" = "--name" ] && name="$a"; prev="$a"; done
    if [ "$name" = "alarm_engine_metrics" ]; then
      echo "bid_metrics	$name	720h0m0s	orgid"
    elif [ "$name" = "alarm_engine_metrics_rollup" ] && [ -f "$state/rollup_created" ]; then
      echo "bid_rollup	$name	infinite	orgid"
    fi
    exit 0 ;;
  "bucket create")
    touch "$state/rollup_created"; exit 0 ;;
  "auth create")
    bid=""
    prev=""
    for a in "$@"; do [ "$prev" = "--read-bucket" ] && bid="$a"; prev="$a"; done
    printf '{"id": "x", "token": "tok-%s", "status": "active"}\\n' "$bid"
    exit 0 ;;
esac
echo "influx stub: unhandled: $*" >&2
exit 64
"""

CURL_STUB = """#!/usr/bin/env bash
# Stub curl: health probe (-w) reports 200; GET /api/v2/setup reports
# allowed; POST /api/v2/setup records that onboarding ran.
case "$*" in
  *"%{http_code}"*) printf '200' ;;
  *"-X POST"*"/api/v2/setup"*) touch "$STUB_STATE/setup_ran" ;;
  *"/api/v2/setup"*) printf '{"allowed": true}\\n' ;;
  *) exit 22 ;;
esac
"""


def _write_stub(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(config: Path, stub_bin: Path, state: Path, stubs_only=False):
    env = dict(os.environ)
    # stubs_only: PATH holds ONLY the stub dir, so a host-installed influx
    # can never be found (or run) when a test simulates a missing CLI.
    env["PATH"] = str(stub_bin) if stubs_only else f"{stub_bin}:{env['PATH']}"
    env["LSM_BREW_CONFIG"] = str(config)
    env["STUB_STATE"] = str(state)
    # Unroutable discard port — even a leak past the stubs can't reach a
    # real InfluxDB.
    env["INFLUX_HOST_URL"] = "http://127.0.0.1:9"
    return subprocess.run(["/bin/bash", str(SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.fixture()
def stub_env(tmp_path):
    stub_bin = tmp_path / "bin"
    state = tmp_path / "state"
    stub_bin.mkdir()
    state.mkdir()
    _write_stub(stub_bin / "influx", INFLUX_STUB)
    _write_stub(stub_bin / "curl", CURL_STUB)
    config = tmp_path / "llm-systems.toml"
    shutil.copy(EXAMPLE, config)
    return config, stub_bin, state


def test_fresh_setup_writes_tokens(stub_env):
    config, stub_bin, state = stub_env
    r = _run(config, stub_bin, state)
    assert r.returncode == 0, r.stdout + r.stderr
    text = config.read_text()
    assert 'metrics        = "tok-bid_metrics"' in text
    assert 'metrics_rollup = "tok-bid_rollup"' in text
    # admin = the generated operator token (64 hex chars).
    assert re.search(r'^admin    = "[0-9a-f]{64}"$', text, re.MULTILINE), \
        "operator token not written to [influxdb.tokens] admin"
    assert (state / "setup_ran").exists()
    assert (state / "rollup_created").exists()
    # No secret (long hex token / password) may appear on any influx argv.
    argv_log = (state / "influx_argv.log").read_text()
    assert not re.search(r"[0-9a-f]{24}", argv_log), \
        f"secret leaked onto influx argv:\n{argv_log}"
    mode = stat.S_IMODE(config.stat().st_mode)
    assert mode == 0o600, f"config mode {oct(mode)} — must stay 0600"
    assert "RECORD THIS NOW" in r.stdout


def test_idempotent_when_tokens_already_filled(stub_env):
    config, stub_bin, state = stub_env
    assert _run(config, stub_bin, state).returncode == 0
    before = config.read_text()
    r = _run(config, stub_bin, state)
    assert r.returncode == 0
    assert "nothing to do" in r.stdout
    assert config.read_text() == before


def test_dies_without_influx_cli(stub_env):
    config, stub_bin, state = stub_env
    (stub_bin / "influx").unlink()
    r = _run(config, stub_bin, state, stubs_only=True)
    assert r.returncode != 0
    assert "influxdb-cli" in r.stderr


def test_dies_without_config(stub_env, tmp_path):
    _, stub_bin, state = stub_env
    r = _run(tmp_path / "missing.toml", stub_bin, state)
    assert r.returncode != 0
    assert "config not found" in r.stderr
