"""#891: the manager wires autotune_batch with real agent callables and audits it."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "backend" / "llm-systems-manager.py").read_text()


def test_batch_routes_are_registered_with_every_dep():
    m = re.search(r"autotune_batch\.register_routes\((.*?)\n\)", SRC, re.S) or \
        re.search(r"autotune_batch\.register_routes\((.*?)\)\n", SRC, re.S)
    assert m, "autotune_batch.register_routes call missing"
    call = m.group(1)
    for dep in ("hosts=_fleet_hosts", "busy_agents=_batch_busy_agents", "preflight=_batch_preflight",
                "run_on_agent=_batch_run_on_agent", "stream_on_agent=_batch_stream_on_agent",
                "cancel_on_agent=_batch_cancel_on_agent", "stop_server=_batch_stop_server",
                "restart_server=_batch_restart_server", "read_config=_batch_read_config",
                "write_config=_batch_write_config", "active_profile=_batch_active_profile",
                "save_profile=_batch_save_profile", "alert=_ae_ingest_alert",
                'run_ended=lambda aid: tool_activity.note_end(aid, "autotune")',
                "shutting_down=lambda: _shutting_down", "models_for=_batch_models_for"):
        assert dep in SRC, f"{dep} not wired"


def test_batch_stream_follower_parses_data_lines():
    m = re.search(r"def _batch_stream_on_agent\(agent_id: str, last_id.*?\):(.*?)\n\n\n", SRC, re.S)
    assert m, "_batch_stream_on_agent missing"
    body = m.group(1)
    assert 'raw.startswith("data: ")' in body and "json.loads(raw[6:])" in body
    assert 'raw.startswith("id: ")' in body and '"Last-Event-ID"' in body
    assert "agent_callback_urls" in body and "agent_tls_kwargs" in body
    assert "stream=True" in body


def test_batch_run_notes_tool_start():
    m = re.search(r"def _batch_run_on_agent\(agent_id: str, body: dict\):(.*?)\n\n\n", SRC, re.S)
    assert m and 'tool_activity.note_start(agent_id, "llama", "autotune")' in m.group(1)


def test_batch_is_audited():
    assert '"autotune.batch": "Started an overnight autotune batch"' in SRC
    assert '"autotune.batch-cancel": "Cancelled an overnight autotune batch"' in SRC
    assert re.search(r'\("POST",\s+re\.compile\(r"\^/api/llm/autotune/batch\$"\),\s+"autotune\.batch",\s+"tools\.run"\)', SRC)
    assert re.search(r'\("POST",\s+re\.compile\(r"\^/api/llm/autotune/batch/\[\^/\]\+/cancel\$"\),\s+"autotune\.batch-cancel",\s+"tools\.run"\)', SRC)


def test_version_bumped():
    assert '__version__ = "v2026.09.10-7"' in SRC
