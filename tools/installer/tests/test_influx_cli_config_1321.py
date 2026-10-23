# install-influxdb.sh: reads token/org from the active InfluxDB CLI config entry
# and runs first-boot setup when the instance is not onboarded (#1321).
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install-influxdb.sh"
TEXT = SCRIPT.read_text()
CLI_ENTRY = re.search(r"^CLI_ENTRY='(.+)'$", TEXT, re.M).group(1)

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def entry(doc, field):
    out = subprocess.run(["jq", "-r", f"{CLI_ENTRY} | .{field}"], input=json.dumps(doc),
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_reads_the_only_entry_of_the_cli_object():
    doc = {"default": {"url": "u", "token": "tok-a", "org": "org-a", "active": True}}
    assert entry(doc, "token") == "tok-a" and entry(doc, "org") == "org-a"


def test_prefers_the_active_entry():
    doc = {"default": {"token": "tok-a", "org": "org-a"},
           "second": {"token": "tok-b", "org": "org-b", "active": True}}
    assert entry(doc, "token") == "tok-b" and entry(doc, "org") == "org-b"


def test_falls_back_to_the_first_entry_when_none_is_active():
    doc = {"default": {"token": "tok-a", "org": "org-a"}, "second": {"token": "tok-b", "org": "org-b"}}
    assert entry(doc, "org") == "org-a"


def test_reads_a_list_shaped_config():
    assert entry([{"token": "tok-a", "org": "org-a"}], "org") == "org-a"


def test_no_list_index_reads_remain():
    assert ".[0]." not in TEXT


def test_stored_tokens_are_skipped_when_setup_is_still_allowed():
    reuse = [l for l in TEXT.splitlines() if "/root/.influxdb-operator-token; then" in l
             or l.startswith("elif ") and "SETUP_ALLOWED" in l]
    assert len(reuse) == 2
    assert all('[[ "$SETUP_ALLOWED" != "true" ]]' in l for l in reuse)
