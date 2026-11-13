"""LM Studio server log view: newest file by time, packet blocks dropped, long lines shortened."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import test_lms_detect_runtime as _rt

lms = _rt.lms
env = _rt.env  # shared fixture, registered here by name


def test_filter_drops_packet_blocks_and_summarises_reasoning(env):
    lines = [
        "[2026-11-13 07:06:40][INFO][m] Generated packet: {",
        '  "choices": [{"delta": {"content": "hi"}}]',
        "}",
        "[2026-11-13 07:06:40][INFO][m] Accumulated 1558 tokens in reasoning content Thinking Process:\\n\\n1. **Analyze**",
        "[2026-11-13 07:06:41][INFO][m] Finished streaming response",
    ]
    assert lms._filter_lms_log(lines) == [
        "[2026-11-13 07:06:40][INFO][m] Accumulated 1558 tokens in reasoning content",
        "[2026-11-13 07:06:41][INFO][m] Finished streaming response",
    ]


def test_filter_shortens_long_lines(env):
    out = lms._filter_lms_log(["[2026-11-13 07:06:40][INFO] " + "x" * 1000])
    assert len(out) == 1 and out[0].endswith(" …") and len(out[0]) == lms._LMS_LOG_LINE_MAX + 2


def test_server_log_reads_the_newest_file_not_the_last_name(env, tmp_path, monkeypatch):
    env.cfg.AGENT_USER = ""
    monkeypatch.setattr(lms.os.path, "expanduser", lambda p: str(tmp_path))
    d = tmp_path / ".lmstudio" / "server-logs" / datetime.now(timezone.utc).strftime("%Y-%m")
    d.mkdir(parents=True)
    old, new = d / "2026-11-13.9.log", d / "2026-11-13.29.log"
    old.write_text("[2026-11-13 07:00:00][INFO] old line\n")
    new.write_text("[2026-11-13 14:00:00][INFO] new line\n")
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))
    r = lms.lms_server_log_endpoint(authorization=None)
    assert r["source"] == str(new)
    assert r["lines"] == ["[2026-11-13 14:00:00][INFO] new line"]


def test_server_log_keeps_real_lines_behind_a_packet_heavy_tail(env, tmp_path, monkeypatch):
    env.cfg.AGENT_USER = ""
    monkeypatch.setattr(lms.os.path, "expanduser", lambda p: str(tmp_path))
    d = tmp_path / ".lmstudio" / "server-logs" / datetime.now(timezone.utc).strftime("%Y-%m")
    d.mkdir(parents=True)
    packet = "[2026-11-13 14:00:01][INFO][m] Generated packet: {\n  \"x\": 1\n}\n"
    (d / "a.log").write_text("[2026-11-13 14:00:00][INFO][m] Running chat completion\n" + packet * 400)
    r = lms.lms_server_log_endpoint(authorization=None)
    assert r["lines"] == ["[2026-11-13 14:00:00][INFO][m] Running chat completion"]
