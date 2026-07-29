"""#472: executor behavior matrix + audit + routing side-effects."""
from __future__ import annotations
import pytest
import autopilot as ap
from autopilot_planner import Action

def _deps():
    log = {"proxy": [], "pin": [], "pool": [], "audit": [], "svc": []}
    return log, {
        "proxy": lambda p, m, path, j=None: (log["proxy"].append((p, path)), (True, {}))[1],
        "set_pin": lambda p, mdl, aid: log["pin"].append((p, mdl, aid)),
        "pool_update": lambda p, aid, inp: log["pool"].append((p, aid, inp)),
        "audit": lambda a, t, o: log["audit"].append((a, o)),
        "vllm_svc": lambda aid, mdl: (log["svc"].append((aid, mdl)), True)[1]}

def _act(kind="load", provider="llama", auto=False, replicas=1):
    a = Action(kind=kind, provider=provider, model="m1", agent_id="a" * 32,
               reason="r", auto=auto, entry_key="m1/" + provider)
    return a, {"m1/" + provider: {"min_replicas": replicas,
                                  "max_replicas": replicas}}

def test_llama_load_proxies_and_pins():
    log, deps = _deps()
    a, entries = _act()
    assert ap.make_executor(deps, entries)(a) is True
    assert ("llama", "/llama/load") in log["proxy"]
    assert log["pin"] == [("llama", "m1", "a" * 32)]
    assert ("autopilot:load", "ok") in log["audit"]

def test_multireplica_load_updates_pool_not_pin():
    log, deps = _deps()
    a, entries = _act(kind="scale_up", replicas=2)
    entries["m1/llama"] = {"min_replicas": 1, "max_replicas": 3}
    ap.make_executor(deps, entries)(a)
    assert log["pool"] == [("llama", "a" * 32, True)] and log["pin"] == []

def test_vllm_auto_refused():
    log, deps = _deps()
    a, entries = _act(provider="vllm", auto=True)
    assert ap.make_executor(deps, entries)(a) is False
    assert log["svc"] == [] and ("autopilot:load", "refused") in log["audit"]

def test_vllm_applied_proposal_uses_svc():
    log, deps = _deps()
    a, entries = _act(provider="vllm", auto=False)
    assert ap.make_executor(deps, entries)(a) is True
    assert log["svc"] == [("a" * 32, "m1")]

def test_download_always_refused():
    log, deps = _deps()
    a, entries = _act(kind="download")
    assert ap.make_executor(deps, entries)(a) is False
    assert ("autopilot:download", "refused") in log["audit"]

def test_scale_down_unloads_and_leaves_pool():
    log, deps = _deps()
    a, entries = _act(kind="scale_down", replicas=2)
    entries["m1/llama"] = {"min_replicas": 1, "max_replicas": 3}
    ap.make_executor(deps, entries)(a)
    assert ("llama", "/llama/unload") in log["proxy"]
    assert log["pool"] == [("llama", "a" * 32, False)]

def test_failed_proxy_audits_fail():
    log, deps = _deps()
    deps["proxy"] = lambda *a, **k: (False, {})
    act, entries = _act()
    assert ap.make_executor(deps, entries)(act) is False
    assert ("autopilot:load", "fail") in log["audit"]
