"""
Terminal create proxy forwards the client's fitted size to the agent (#573).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import Flask

import terminal


@pytest.fixture
def app_calls(monkeypatch):
    calls = []

    def fake_pick(kind):
        return {"agent_id": "a" * 32, "hostname": "h", "token": "t"}, None

    def fake_request(method, agent, path, **kwargs):
        calls.append({"method": method, "path": path, "kwargs": kwargs})
        return None, ["h"], "unreachable"

    monkeypatch.setattr(terminal, "_pick_agent", fake_pick)
    monkeypatch.setattr(terminal.agent_registry, "agent_request", fake_request)
    app = Flask(__name__)
    terminal.register_routes(app, SimpleNamespace())
    return app, calls


@pytest.mark.parametrize("route", ["/api/terminal/create",
                                   "/api/lms/terminal/create",
                                   "/api/vllm/terminal/create"])
def test_create_forwards_rows_cols_to_the_agent(app_calls, route):
    app, calls = app_calls
    app.test_client().post(route, json={"rows": 40, "cols": 190})
    assert calls and calls[0]["kwargs"].get("json") == {"rows": 40, "cols": 190}


def test_create_without_a_body_sends_none(app_calls):
    app, calls = app_calls
    app.test_client().post("/api/terminal/create")
    assert calls and calls[0]["kwargs"].get("json") is None
