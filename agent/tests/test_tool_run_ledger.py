"""#772: agent-side ledger helpers — series maxes and the manager POST.

_shared.py pulls fastapi/requests, which the CI agent env doesn't install, so
the pure helpers are extracted into a stub namespace instead of imported.
"""
from __future__ import annotations

import logging
import re
import types
from pathlib import Path

import pytest

_SHARED_PY = Path(__file__).resolve().parent.parent / "providers" / "_shared.py"
_WANTED = ("bench_row_series", "bench_maxes", "post_tool_run")


def _extract():
    src = _SHARED_PY.read_text()
    ns: dict = {"log": logging.getLogger("test"), "Optional": object}
    ns["BENCH_SERIES"] = ("ppt", "gen", "pg")
    for name in _WANTED:
        m = re.search(rf"^def {name}\(.*?(?=^\S|\Z)", src, re.S | re.M)
        assert m, f"{name} not found in _shared.py"
        exec(compile(m.group(0), "_shared", "exec"), ns)
    return types.SimpleNamespace(**{k: ns[k] for k in _WANTED})


S = _extract()


@pytest.mark.parametrize("row,expected", [
    ({"series": "gen", "avg_ts": 1}, "gen"),
    ({"series": "ppt"}, "ppt"),
    ({"series": "pg"}, "pg"),
    ({"n_prompt": 512, "n_gen": 0}, "ppt"),
    ({"n_prompt": 0, "n_gen": 128}, "gen"),
    ({"n_prompt": 512, "n_gen": 128}, "pg"),
    ({"n_prompt": 0, "n_gen": 0}, None),
    ({}, None),
    ({"series": "bogus", "n_gen": 8}, "gen"),
])
def test_row_series_matches_the_dashboard_rule(row, expected):
    assert S.bench_row_series(row) == expected


def test_maxes_take_the_best_row_per_series():
    rows = [{"n_prompt": 512, "n_gen": 0, "avg_ts": 900.0},
            {"n_prompt": 512, "n_gen": 0, "avg_ts": 1100.0},
            {"n_prompt": 0, "n_gen": 128, "avg_ts": 40.0},
            {"n_prompt": 0, "n_gen": 128, "avg_ts": 38.0}]
    assert S.bench_maxes(rows) == {"ppt": 1100.0, "gen": 40.0, "pg": None}


def test_maxes_of_no_rows_are_all_none():
    assert S.bench_maxes([]) == {"ppt": None, "gen": None, "pg": None}
    assert S.bench_maxes(None) == {"ppt": None, "gen": None, "pg": None}


def test_unmeasurable_rows_do_not_invent_a_zero():
    assert S.bench_maxes([{"n_prompt": 0, "n_gen": 0, "avg_ts": 5}])["gen"] is None


class _Ctx:
    def __init__(self, url="https://mgr:5000", token="tok", agent_id="a" * 32):
        self.config = types.SimpleNamespace(MANAGER_URL=url)
        self.state = {"token": token, "agent_id": agent_id}
        self.posted = []
        outer = self

        class _Sess:
            def post(self, url, json=None, timeout=None, headers=None):
                outer.posted.append((url, json, headers))
                return types.SimpleNamespace(status_code=200)

        self.post_session = _Sess()


def test_post_names_the_agent_run_and_summary():
    ctx = _Ctx()
    S.post_tool_run(ctx, "benchmark", "llama", "run1", "org/m", True,
                    {"gen_tps": 42.0, "ppt_tps": None, "bench_tool": "llama-bench"})
    url, body, headers = ctx.posted[0]
    assert url == "https://mgr:5000/api/tools/runs"
    assert headers["Authorization"] == "Bearer tok"
    assert body["tool"] == "benchmark" and body["run_id"] == "run1"
    assert body["model_id"] == "org/m" and body["ok"] is True
    assert body["agent_id"] == "a" * 32
    assert body["gen_tps"] == 42.0 and body["bench_tool"] == "llama-bench"
    # None metrics are dropped rather than posted as nulls.
    assert "ppt_tps" not in body


def test_post_is_skipped_without_a_manager_url_token_or_model():
    for kw in ({"url": ""}, {"token": ""}):
        ctx = _Ctx(**kw)
        S.post_tool_run(ctx, "benchmark", "llama", "r", "m", True, {})
        assert ctx.posted == []
    ctx = _Ctx()
    S.post_tool_run(ctx, "benchmark", "llama", "r", "", True, {})
    assert ctx.posted == []


def test_a_failing_post_never_reaches_the_caller():
    ctx = _Ctx()

    class _Boom:
        def post(self, *a, **kw):
            raise RuntimeError("manager down")

    ctx.post_session = _Boom()
    S.post_tool_run(ctx, "autotune", "llama", "r", "m", True, {"ctx_size": 4096})
