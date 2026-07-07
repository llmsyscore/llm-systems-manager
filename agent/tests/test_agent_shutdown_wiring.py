# agent/tests/test_agent_shutdown_wiring.py
# Agent shutdown drains the metric buffer (#279) and terminates tracked
# bench/autotune process groups (#280).
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
AGENT_PY = AGENT_DIR / "llm-systems-agent.py"
LLAMA_PY = AGENT_DIR / "providers" / "llama.py"


def _lifespan_body() -> str:
    text = AGENT_PY.read_text()
    m = re.search(r"^async def _lifespan\(.*?(?=^app = FastAPI)", text,
                  re.MULTILINE | re.DOTALL)
    assert m, "could not locate _lifespan in llm-systems-agent.py"
    return m.group(0)


def test_lifespan_drains_metric_client_on_shutdown():
    body = _lifespan_body()
    assert re.search(r"_metric_client\.stop\b", body), \
        "_lifespan never calls _metric_client.stop() — buffered samples drop on restart"


def test_lifespan_terminates_bench_autotune_children():
    body = _lifespan_body()
    assert "shutdown_children" in body, \
        "_lifespan never terminates tracked bench/autotune process groups"


def _extract_py_func(source: Path, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", source.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from {source.name}"
    return m.group(0)


def _shutdown_ns(**over):
    import threading
    ns = {
        "os": os, "signal": signal, "subprocess": subprocess,
        "log": logging.getLogger("test"),
        "_bench_proc": None, "_bench_pgid": None,
        "_autotune_proc": None, "_autotune_pgid": None,
        "_bench_cancel_event": threading.Event(),
        "_autotune_cancel_event": threading.Event(),
    }
    ns.update(over)
    exec(compile(_extract_py_func(LLAMA_PY, "shutdown_children"),
                 str(LLAMA_PY), "exec"), ns)
    return ns


def test_shutdown_children_kills_tracked_process_group(tmp_path):
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    ns = _shutdown_ns(_bench_proc=proc, _bench_pgid=os.getpgid(proc.pid))
    try:
        ns["shutdown_children"]()
        deadline = time.monotonic() + 6
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None, "tracked bench child survived shutdown_children()"
        assert ns["_bench_cancel_event"].is_set()
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def test_shutdown_children_noop_without_tracked_procs():
    _shutdown_ns()["shutdown_children"]()  # must not raise


def test_stop_drain_flushes_multi_batch_backlog(tmp_path):
    import sys
    import types
    sys.path.insert(0, str(AGENT_DIR))
    import buffered_metric_client as bmc

    class _OkSession:
        def __init__(self):
            self.posted_ids = []

        def post(self, url, json=None, timeout=None, headers=None):
            self.posted_ids.extend(s["id"] for s in json["samples"])
            return types.SimpleNamespace(raise_for_status=lambda: None)

    session = _OkSession()
    client = bmc.BufferedMetricClient(
        endpoint_url="http://example.invalid/api/alarm/metrics/ingest",
        host="testhost",
        cache_dir=tmp_path,
        batch_limit=8,
        session=session,
    )
    for i in range(30):
        client.enqueue({"id": i})

    client.stop(drain=True)
    assert client.buffered_count() == 0
    assert sorted(session.posted_ids) == list(range(30))


def test_stop_drain_stops_on_unreachable_endpoint(tmp_path):
    import sys
    import types  # noqa: F401
    sys.path.insert(0, str(AGENT_DIR))
    import buffered_metric_client as bmc

    class _DeadSession:
        def __init__(self):
            self.calls = 0

        def post(self, *a, **k):
            self.calls += 1
            raise RuntimeError("connection refused")

    session = _DeadSession()
    client = bmc.BufferedMetricClient(
        endpoint_url="http://example.invalid/api/alarm/metrics/ingest",
        host="testhost",
        cache_dir=tmp_path,
        batch_limit=8,
        session=session,
    )
    for i in range(30):
        client.enqueue({"id": i})

    client.stop(drain=True)  # must terminate, not spin
    assert session.calls <= 2
    assert client.buffered_count() > 0  # backlog preserved for next start
