"""#880: the manager proxies the autotune preflight and keeps the run/stream/cancel routes."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "backend" / "llm-systems-manager.py").read_text()


def test_preflight_route_proxies_to_the_agent():
    m = re.search(r'@app\.route\("/api/llm/autotune/preflight"\)\s*\ndef (\w+)\(\):(.*?)\n\n', SRC, re.S)
    assert m, "preflight route missing"
    body = m.group(2)
    assert 'proxy_to_primary("llama", "GET", "/llama/autotune/preflight"' in body


def test_run_route_still_notes_tool_start():
    m = re.search(r'@app\.route\("/api/llm/autotune/run", methods=\["POST"\]\)(.*?)\n\n', SRC, re.S)
    assert m and '_note_tool_start("llama", "autotune")' in m.group(1)
