"""#1041: a system-only agent pushes its live sample as a `system` provider-state envelope."""
from __future__ import annotations

import logging
import types

from tests.test_residency_wiring import _exec_fn


class _Resp:
    def __init__(self, status=200):
        self.status_code, self.ok = status, 200 <= status < 300


class _Session:
    def __init__(self, status=200):
        self.status, self.posts = status, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json))
        return _Resp(self.status)


def _ns(status=200, **cfg):
    conf = dict(LLAMA_ENABLED=False, LMS_ENABLED=False, VLLM_ENABLED=False, MANAGER_URL="https://mgr:5000/",
                PUSH_HOST_METRICS_ENABLED=True)
    conf.update(cfg)
    ns = {"CONFIG": types.SimpleNamespace(**conf), "_post_session": _Session(status), "_token_provider": lambda: "tok",
          "logger": logging.getLogger("test"), "Any": object, "_push_endpoint_state": "auto"}
    _exec_fn("_host_push_provider", ns)
    _exec_fn("_push_host_payload", ns)
    return ns


SAMPLE = {"ts": 1.0, "system": {"cpu_total": 3.0, "ram": {"percent": 50.0}}, "mac_power": {"soc_total_w": 9.0}}


def test_provider_choice_by_role():
    assert _ns()["_host_push_provider"]() == "system"
    assert _ns(LLAMA_ENABLED=True)["_host_push_provider"]() == "llama"
    assert _ns(LMS_ENABLED=True)["_host_push_provider"]() is None
    assert _ns(VLLM_ENABLED=True)["_host_push_provider"]() is None


def test_system_only_agent_pushes_a_system_envelope_with_mac_power():
    ns = _ns()
    ns["_push_host_payload"](SAMPLE)
    (url, body), = ns["_post_session"].posts
    assert url == "https://mgr:5000/api/remote/provider-state"
    assert body == {"provider": "system", "sample": {"cpu_total": 3.0, "ram": {"percent": 50.0}, "mac_power": {"soc_total_w": 9.0}}}
    assert ns["_push_endpoint_state"] == "envelope"


def test_lms_only_agent_pushes_nothing_here():
    ns = _ns(LMS_ENABLED=True)
    ns["_push_host_payload"](SAMPLE)
    assert ns["_post_session"].posts == []


def test_old_manager_gets_no_legacy_fallback_for_the_system_envelope():
    ns = _ns(status=404)
    ns["_push_host_payload"](SAMPLE)
    ns["_push_host_payload"](SAMPLE)
    assert [u for u, _ in ns["_post_session"].posts] == ["https://mgr:5000/api/remote/provider-state"]
    assert ns["_push_endpoint_state"] == "legacy"


def test_llama_agent_falls_back_to_host_metrics_on_404():
    ns = _ns(status=404, LLAMA_ENABLED=True)
    ns["_push_host_payload"]({**SAMPLE, "llama": {"state": "awake"}})
    urls = [u for u, _ in ns["_post_session"].posts]
    assert urls == ["https://mgr:5000/api/remote/provider-state", "https://mgr:5000/api/remote/host-metrics"]
    assert ns["_post_session"].posts[0][1]["sample"]["llama"] == {"state": "awake"}
