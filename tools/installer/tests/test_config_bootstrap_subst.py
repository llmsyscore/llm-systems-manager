# Runs the substitution heredoc from install-config-bootstrap.sh against the
# live llm-systems.toml.example and asserts every targeted key substitutes.
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPO / "tools" / "installer" / "install-config-bootstrap.sh"
TEMPLATE = REPO / "config" / "llm-systems.toml.example"


def extract_heredoc():
    text = BOOTSTRAP.read_text()
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF\n", text, re.DOTALL)
    assert m, "PYEOF heredoc not found in install-config-bootstrap.sh"
    return m.group(1)


def run_bootstrap_python(tmp_path, vals):
    script = tmp_path / "subst.py"
    script.write_text(extract_heredoc())
    toml = tmp_path / "llm-systems.toml"
    toml.write_text(TEMPLATE.read_text())
    vals_file = tmp_path / "vals"
    vals_file.write_text("".join(f"{k}={v}\n" for k, v in vals.items()))
    proc = subprocess.run(
        [sys.executable, str(script), str(toml), str(vals_file)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return tomllib.loads(toml.read_text())


MODE3_VALS = {
    "HAS_MGR": "1",
    "HAS_AE": "0",
    "MGR_HOST": "0.0.0.0",
    "MGR_PORT": "5000",
    "MGR_IP": "192.0.2.10",
    "ADMIN_CIDR": "192.0.2.0/24",
    "ALARM_ENGINE_URL": "http://192.0.2.20:8081",
    "INFLUX_HOSTNAME": "192.0.2.20",
    "INFLUX_PORT": "8086",
    "ADMIN_USER": "opuser",
    "ADMIN_PW_HASH": "scrypt$fake$hash",
}


@pytest.fixture(scope="module")
def mode3_config(tmp_path_factory):
    return run_bootstrap_python(tmp_path_factory.mktemp("mode3"), MODE3_VALS)


def test_manager_cors_origins_substituted(mode3_config):
    assert mode3_config["manager"]["cors_origins"] == (
        "http://192.0.2.10:5000,http://localhost:5000,http://192.0.2.10:8081"
    )


def test_manager_alarm_engine_url_substituted(mode3_config):
    assert mode3_config["manager"]["alarm_engine_url"] == "http://192.0.2.20:8081"


def test_manager_auth_substituted(mode3_config):
    # [manager.auth] sits below an in-comment bracket too; pre-fix, custom
    # installer credentials silently fell back to the template defaults.
    assert mode3_config["manager"]["auth"]["username"] == "opuser"
    assert mode3_config["manager"]["auth"]["password_hash"] == "scrypt$fake$hash"


def test_admin_cidrs_substituted(mode3_config):
    assert mode3_config["manager"]["security"]["admin_cidrs"] == [
        "127.0.0.1", "::1", "192.0.2.0/24",
    ]


def test_every_key_reachable_by_section_scan():
    # Every scalar key in every [section] must be matchable by sub_in_section's
    # section-scan regex, no matter what brackets appear in comments above it.
    text = TEMPLATE.read_text()
    headers = list(re.finditer(r"^\[([A-Za-z0-9_.]+)\]$", text, re.MULTILINE))
    checked = 0
    for i, header in enumerate(headers):
        start = header.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end].split("\n[[", 1)[0]
        for m in re.finditer(r"^([a-z_]+)\s*=", body, re.MULTILINE):
            key = m.group(1)
            pattern = re.compile(
                r"^\[" + re.escape(header.group(1)) + r"\](?:(?!^\[)[\s\S])*?\n"
                + re.escape(key) + r"\s*=\s*",
                re.MULTILINE | re.DOTALL,
            )
            assert pattern.search(text), (
                f"[{header.group(1)}].{key} unreachable by section scan"
            )
            checked += 1
    assert checked > 50


def test_ae_side_still_substitutes(tmp_path):
    cfg = run_bootstrap_python(tmp_path, {
        "HAS_MGR": "0",
        "HAS_AE": "1",
        "AE_HOST": "0.0.0.0",
        "AE_PORT": "8081",
        "MGR_IP": "192.0.2.10",
        "MANAGER_URL": "http://192.0.2.10:5000",
    })
    assert cfg["alarm_engine"]["manager_url"] == "http://192.0.2.10:5000"
    assert cfg["alarm_engine"]["cors_origins"] == (
        "http://192.0.2.10:5000,http://localhost:5000,http://192.0.2.10:8081"
    )
