"""#1200: the Docker entrypoint generates the alarm-engine tokens when .env leaves them
blank, shares them through the ae-data volume, and refuses equal tokens."""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RENDER = REPO / "docker" / "render-config.sh"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def render(tmp_path, cfg_name="llm-systems.toml", **env):
    cfg = tmp_path / cfg_name
    proc = subprocess.run(
        ["bash", "-c", f'. "{RENDER}"; render_config'],
        capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "LLM_SYSTEMS_CONFIG": str(cfg),
             "LLM_SYSTEMS_AE_DATA": str(tmp_path / "ae-data"), **env},
    )
    return proc, cfg


def tokens_in(cfg):
    ae = tomllib.loads(Path(cfg).read_text())["alarm_engine"]
    return ae["ingest_token"], ae["management_token"]


def test_blank_tokens_are_generated_once_and_shared(tmp_path):
    proc, cfg = render(tmp_path)
    assert proc.returncode == 0, proc.stderr
    ingest, mgmt = tokens_in(cfg)
    assert HEX64.match(ingest) and HEX64.match(mgmt) and ingest != mgmt
    store = tmp_path / "ae-data" / "docker-tokens.env"
    assert oct(store.stat().st_mode & 0o777) == "0o600"
    assert "generated once" in proc.stdout and "docker compose exec" in proc.stdout
    assert ingest not in proc.stdout and mgmt not in proc.stdout   # values never reach the log
    # The second container renders from the same volume and gets the same tokens.
    proc2, cfg2 = render(tmp_path, cfg_name="manager.toml")
    assert proc2.returncode == 0, proc2.stderr
    assert tokens_in(cfg2) == (ingest, mgmt)
    assert "generated once" not in proc2.stdout
    assert not list((tmp_path / "ae-data").glob(".tokens.*"))


def test_env_tokens_win_and_only_the_missing_one_is_generated(tmp_path):
    proc, cfg = render(tmp_path, LSM_AE_INGEST_TOKEN="a" * 64)
    assert proc.returncode == 0, proc.stderr
    ingest, mgmt = tokens_in(cfg)
    assert ingest == "a" * 64 and HEX64.match(mgmt) and mgmt != ingest


def test_replace_me_counts_as_blank(tmp_path):
    proc, cfg = render(tmp_path, LSM_AE_INGEST_TOKEN="REPLACE_ME", LSM_AE_MANAGEMENT_TOKEN="m" * 64)
    assert proc.returncode == 0, proc.stderr
    ingest, mgmt = tokens_in(cfg)
    assert HEX64.match(ingest) and mgmt == "m" * 64


def test_equal_tokens_are_refused(tmp_path):
    proc, cfg = render(tmp_path, LSM_AE_INGEST_TOKEN="s" * 64, LSM_AE_MANAGEMENT_TOKEN="s" * 64)
    assert proc.returncode == 1
    assert "must differ" in proc.stderr
    assert not cfg.exists()
