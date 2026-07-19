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


def _run(config: Path, stub_bin: Path, state: Path, stubs_only=False, extra_env=None):
    env = dict(os.environ)
    # stubs_only: PATH holds ONLY the stub dir, so a host-installed influx
    # can never be found (or run) when a test simulates a missing CLI.
    env["PATH"] = str(stub_bin) if stubs_only else f"{stub_bin}:{env['PATH']}"
    env["LSM_BREW_CONFIG"] = str(config)
    env["STUB_STATE"] = str(state)
    # Unroutable discard port — even a leak past the stubs can't reach a
    # real InfluxDB.
    env["INFLUX_HOST_URL"] = "http://127.0.0.1:9"
    env.update(extra_env or {})
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


DEAD_CURL_STUB = """#!/usr/bin/env bash
# Probe never turns healthy; no other endpoint should be reached.
case "$*" in
  *"%{http_code}"*) printf '000' ;;
  *) exit 7 ;;
esac
"""

BREW_STUB = """#!/usr/bin/env bash
# Stub brew: influxdb formula installed; service starts; state + prefix
# come from files the test writes into $STUB_STATE.
case "$*" in
  "list --versions influxdb") echo "influxdb 2.7.11" ;;
  "services start influxdb") : ;;
  "services info influxdb --json")
    printf '[{"name":"influxdb","status":"%s"}]\\n' "$(cat "$STUB_STATE/svc_status")" ;;
  "services info influxdb") echo "influxdb ($(cat "$STUB_STATE/svc_status"))" ;;
  "--prefix") cat "$STUB_STATE/prefix" ;;
  *) exit 64 ;;
esac
"""


def _unhealthy_env(stub_env, status: str):
    config, stub_bin, state = stub_env
    _write_stub(stub_bin / "curl", DEAD_CURL_STUB)
    _write_stub(stub_bin / "brew", BREW_STUB)
    (state / "svc_status").write_text(status)
    prefix = state / "brewprefix"
    (prefix / "var" / "log").mkdir(parents=True)
    (prefix / "var" / "log" / "influxdb2.log").write_text("influxd boom: lock timeout\n")
    (state / "prefix").write_text(str(prefix))
    return config, stub_bin, state


def test_unhealthy_server_dumps_diagnostics(stub_env):
    # WAIT_S=6 also drives the i%5==4 state check through its non-error
    # branch (status "started" → keep waiting, print a progress dot).
    config, stub_bin, state = _unhealthy_env(stub_env, "started")
    r = _run(config, stub_bin, state, extra_env={"LSM_INFLUX_WAIT_S": "6"})
    assert r.returncode != 0
    assert "did not become healthy" in r.stderr
    # The diagnostics really ran the command (dynamic stub output), not just
    # the static section heading.
    assert "influxdb (started)" in r.stderr
    assert "influxd boom: lock timeout" in r.stderr
    assert "." in r.stderr  # progress dot from the non-error wait branch
    # Nothing was provisioned and the config is untouched.
    assert 'metrics        = "REPLACE_ME"' in config.read_text()


def test_service_error_state_bails_early_with_diagnostics(stub_env):
    config, stub_bin, state = _unhealthy_env(stub_env, "error")
    r = _run(config, stub_bin, state, extra_env={"LSM_INFLUX_WAIT_S": "30"})
    assert r.returncode != 0
    assert "error state" in r.stderr
    assert "influxdb (error)" in r.stderr
    assert "influxd boom: lock timeout" in r.stderr


def test_failed_service_start_dumps_diagnostics(stub_env):
    config, stub_bin, state = _unhealthy_env(stub_env, "none")
    brew = (stub_bin / "brew").read_text().replace(
        '"services start influxdb") : ;;',
        '"services start influxdb") echo "Error: bootstrap failed" >&2; exit 1 ;;')
    _write_stub(stub_bin / "brew", brew)
    r = _run(config, stub_bin, state)
    assert r.returncode != 0
    assert "brew services start influxdb failed" in r.stderr
    assert "influxd boom: lock timeout" in r.stderr


DELAYED_CURL_STUB = """#!/usr/bin/env bash
# Probe reports 000 for the first 8 calls, then 200 — exercises the real
# wait loop's keep-waiting → healthy transition.
state="$STUB_STATE"
case "$*" in
  *"%{http_code}"*)
    n=$(( $(cat "$state/probe_count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$state/probe_count"
    if [ "$n" -le 8 ]; then printf '000'; else printf '200'; fi ;;
  *"-X POST"*"/api/v2/setup"*) touch "$state/setup_ran" ;;
  *"/api/v2/setup"*) printf '{"allowed": true}\\n' ;;
  *) exit 22 ;;
esac
"""


def test_slow_start_waits_then_provisions(stub_env):
    config, stub_bin, state = stub_env
    _write_stub(stub_bin / "curl", DELAYED_CURL_STUB)
    _write_stub(stub_bin / "brew", BREW_STUB)
    (state / "svc_status").write_text("started")
    (state / "prefix").write_text(str(state))
    r = _run(config, stub_bin, state)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "waiting for InfluxDB" in r.stdout
    assert "." in r.stderr  # at least one progress dot before it turned healthy
    assert int((state / "probe_count").read_text()) > 8
    # Full provisioning still completed after the slow start.
    assert (state / "setup_ran").exists()
    text = config.read_text()
    assert 'metrics        = "tok-bid_metrics"' in text
    assert 'metrics_rollup = "tok-bid_rollup"' in text
