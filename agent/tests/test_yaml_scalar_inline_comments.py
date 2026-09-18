# agent/tests/test_yaml_scalar_inline_comments.py
"""#1066: install.sh's _yaml_scalar returns the value alone when the line carries
the example's inline comment (the reconcile writes every value that way)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_selfupdate_svcconfig_guard import YAML_SCALAR  # noqa: E402

_CFG = """\
MANAGER_URL:      "http://192.0.2.10:5000"                 # Use the format http://<IP Address>:5000 for the MANAGER_URL
TOKEN_FILE:       ""      # blank on purpose
TLS_CA_FILE: 'data/tls-ca.pem'
LLAMA_ENABLED: false   # inline note
VLLM_ENABLED: true
LLAMA_SYSTEMD_UNIT: llama_server.service
FRAG: "http://x/#frag"  # a hash inside the quotes is part of the value
"""


def _scalar(tmp_path: Path, key: str) -> str:
    cfg = tmp_path / "agent_config.yaml"
    cfg.write_text(_CFG)
    r = subprocess.run(["bash", "-c", YAML_SCALAR + f'\n_yaml_scalar "{cfg}" {key}\n'], capture_output=True, text=True, check=True)
    return r.stdout.rstrip("\n")


def test_quoted_values_drop_the_inline_comment(tmp_path):
    assert _scalar(tmp_path, "MANAGER_URL") == "http://192.0.2.10:5000"
    assert _scalar(tmp_path, "TOKEN_FILE") == ""
    assert _scalar(tmp_path, "TLS_CA_FILE") == "data/tls-ca.pem"
    assert _scalar(tmp_path, "FRAG") == "http://x/#frag"


def test_unquoted_values_end_at_the_comment(tmp_path):
    assert _scalar(tmp_path, "LLAMA_ENABLED") == "false"
    assert _scalar(tmp_path, "VLLM_ENABLED") == "true"
    assert _scalar(tmp_path, "LLAMA_SYSTEMD_UNIT") == "llama_server.service"
    assert _scalar(tmp_path, "MISSING") == ""
