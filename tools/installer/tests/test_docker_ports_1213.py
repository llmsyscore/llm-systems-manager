"""#1213: every install path exposes the tool-origin ports."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_dockerfile_exposes_tool_ports():
    text = (ROOT / "docker" / "Dockerfile").read_text()
    m = re.search(r"^EXPOSE (.+)$", text, re.M)
    assert m, "no EXPOSE line"
    assert {"5447", "5448"} <= set(m.group(1).split())


def test_compose_maps_tool_ports():
    text = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r'^\s*-\s*"5447:5447"', text, re.M)
    assert re.search(r'^\s*-\s*"5448:5448"', text, re.M)


def test_bootstrap_banner_names_tool_port():
    text = (ROOT / "tools" / "installer" / "install-config-bootstrap.sh").read_text()
    assert "[manager].tools_port" in text


def test_deployment_doc_lists_tool_ports():
    text = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
    assert "| 5447 / 5448 |" in text and "`[manager].tools_port`" in text and ":5448" in text
