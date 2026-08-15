# agent/tests/test_download_pty_winsize.py
"""#560: the download PTY carries a real winsize so hf progress bars render,
and \r progress frames are throttled to one per interval on the console stream."""
from __future__ import annotations

import contextlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]

_STUBBED = (
    "requests", "fastapi", "fastapi.responses", "starlette",
    "starlette.concurrency", "stream_pool", "_best_effort", "_bench_replay",
    "collectors", "collectors.gpu", "providers", "providers.llama_install",
    "providers.llama_sse", "providers.llama_upgrade", "providers.llama",
)


def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def _load_llama():
    # Stub the heavy third-party / sibling deps so llama.py imports without a venv.
    _stub("requests")
    _stub("fastapi", Header=lambda **k: None, HTTPException=Exception,
          Query=lambda *a, **k: None, Request=object)
    _stub("fastapi.responses", Response=object, StreamingResponse=object)
    _stub("starlette.concurrency", run_in_threadpool=None)
    _stub("starlette")
    _stub("stream_pool")

    @contextlib.contextmanager
    def _be(*a, **k):
        yield
    _stub("_best_effort", best_effort=_be)
    _stub("_bench_replay", BenchReplayBuffer=lambda *a, **k: object())
    _stub("collectors")
    _stub("collectors.gpu", collect_gpu=lambda *a, **k: {})

    pkg = types.ModuleType("providers")
    pkg.__path__ = [str(_AGENT_ROOT / "providers")]
    sys.modules["providers"] = pkg
    for sub in ("llama_install", "llama_sse", "llama_upgrade"):
        sys.modules[f"providers.{sub}"] = types.ModuleType(f"providers.{sub}")
    sys.modules["providers.llama_install"].strip_ansi = lambda s: s

    spec = importlib.util.spec_from_file_location(
        "providers.llama", _AGENT_ROOT / "providers" / "llama.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["providers.llama"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def llama():
    saved = {n: sys.modules.get(n) for n in _STUBBED}
    yield _load_llama()
    for n, m in saved.items():
        if m is None:
            sys.modules.pop(n, None)
        else:
            sys.modules[n] = m


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except Exception:
            return out


def _lines(msgs):
    return [m["text"] for m in msgs if m.get("type") == "line"]


def test_pty_child_sees_configured_terminal_size(llama):
    llama._llama_run_command([
        sys.executable, "-c",
        "import os; ws = os.get_terminal_size(1); print('WS', ws.columns, ws.lines)",
    ])
    ws = [t for t in _lines(_drain(llama._dl_queue)) if t.startswith("WS ")]
    assert ws, "no WS line in stream"
    _, cols, rows = ws[0].split()
    assert int(cols) == llama._DL_PTY_COLS and int(rows) == llama._DL_PTY_ROWS


def test_pty_child_sees_matching_columns_env(llama):
    llama._llama_run_command([
        sys.executable, "-c",
        "import os; print('ENV', os.environ.get('COLUMNS'), os.environ.get('LINES'))",
    ])
    env = [t for t in _lines(_drain(llama._dl_queue)) if t.startswith("ENV")]
    assert env, "no ENV line in stream"
    parts = env[0].split()
    assert parts[1] == str(llama._DL_PTY_COLS) and parts[2] == str(llama._DL_PTY_ROWS)


def test_cr_progress_frames_are_throttled(llama):
    # 60 rapid \r frames then a final \n line: only the first frame passes the
    # throttle window; the newest held-back frame flushes before "done".
    llama._llama_run_command([
        sys.executable, "-c",
        "import sys\n"
        "w = sys.stdout.write\n"
        "[w(f'PROG {i}\\r') or sys.stdout.flush() for i in range(60)]\n"
        "w('done\\n')",
    ])
    lines = _lines(_drain(llama._dl_queue))
    prog = [t for t in lines if t.startswith("PROG")]
    assert "done" in lines
    assert lines.index("done") > lines.index(prog[-1])
    assert 1 <= len(prog) <= 5, f"expected throttled frames, got {len(prog)}: {prog}"
    assert prog[-1] == "PROG 59"
