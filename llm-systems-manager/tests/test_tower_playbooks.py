"""#924 Tower playbooks: declarative match + step templates over alert rows."""
from __future__ import annotations

import tower_playbooks as pb


def test_wake_llama_matches_a_sleeping_llama_alert_and_renders_the_host():
    a = {"id": "a1", "rule": "llama-server asleep", "message": "idle sleep since 21:25", "host": "box", "status": "active"}
    assert [p.id for p in pb.matching(a)] == ["wake_llama"]
    assert pb.render(pb.BY_ID["wake_llama"], a) == [("wake_server", {"host": "box"})]
    assert pb.matching({**a, "host": None}) == []
    assert pb.BY_ID["wake_llama"].safe is True


def test_reload_lms_needs_a_pinned_model_named_in_the_message():
    a = {"id": "a2", "rule": "Model dropped", "message": "LM Studio unloaded model gemma-3-12b-it on mac", "host": "mac", "status": "active"}
    pins = lambda prov, host, model: (prov, host, model) == ("lms", "mac", "gemma-3-12b-it")
    reload_ = pb.BY_ID["reload_lms_model"]
    assert [p.id for p in pb.matching(a)] == ["reload_lms_model"] and reload_.requires_pin is True
    assert pb.render(reload_, a, pins) == [("load_model", {"provider": "lms", "host": "mac", "model": "gemma-3-12b-it"})]
    assert pb.render(reload_, a) is None                                                     # no pin source
    assert pb.render(reload_, {**a, "message": "LM Studio unloaded model some-405b-q8 on mac"}, pins) is None
    assert pb.render(reload_, a, lambda *_: (_ for _ in ()).throw(RuntimeError("registry"))) is None
    assert pb.matching({**a, "message": "LM Studio dropped something"}) == []
    assert pb.fields(a) == {"host": "mac", "alert_id": "a2", "model": "gemma-3-12b-it"}


def test_restart_llama_is_not_safe_and_recovery_reads_the_numbers():
    bad = {"id": "a3", "rule": "llama-server unhealthy", "message": "not responding", "host": "box", "status": "active"}
    assert [p.id for p in pb.matching(bad)] == ["restart_llama"] and pb.BY_ID["restart_llama"].safe is False
    assert pb.render(pb.BY_ID["restart_llama"], bad) == [("restart_provider", {"provider": "llama", "host": "box"})]
    up = {"id": "a4", "rule": "GPU hot", "message": "Value 91.0 °C exceeds threshold 85.0 °C", "host": "box",
          "status": "active", "value": 72.0, "threshold": 85.0}
    assert [p.id for p in pb.matching(up)] == ["ack_after_recovery"]
    assert pb.matching({**up, "value": 91.0}) == []
    down = {**up, "message": "Value 2 falls below threshold 10", "value": 12.0, "threshold": 10.0}
    assert [p.id for p in pb.matching(down)] == ["ack_after_recovery"] and pb.matching({**down, "value": 3.0}) == []
    assert pb.matching({**up, "message": "Value 91 is outside range [10, 85]", "value": 50.0}) == []
    assert pb.matching({**up, "message": "Value 91 exceeds threshold 85, recovered", "value": 91.0}) == []   # numbers win
    z = {**up, "message": "Z-score 4.12 exceeds threshold 3.0 (mean=1.00, std=0.50)", "value": 0.4, "threshold": 4.12}
    assert pb.matching(z) == []                                                              # anomaly still firing
    ext = {"id": "a6", "rule": "UPS", "message": "power back to normal", "status": "active"}
    assert [p.id for p in pb.matching(ext)] == ["ack_after_recovery"]
    assert pb.matching({**up, "status": "closed"}) == []
    assert pb.render(pb.BY_ID["ack_after_recovery"], up) == [("ack_alert", {"alert_id": "a4"})]
    assert pb.render(pb.BY_ID["wake_llama"], {"id": "a5"}) is None


def test_prompt_lines_and_a_broken_matcher_never_raise(monkeypatch):
    assert pb.prompt_lines([]) == "No playbook matches this alert."
    txt = pb.prompt_lines([pb.BY_ID["wake_llama"], pb.BY_ID["restart_llama"]])
    assert txt.splitlines()[1].startswith("- wake_llama: Wake llama-server") and "not safe" in txt.splitlines()[2]
    broken = pb.Playbook("x", "X", lambda a: a["missing"], (), True, "")
    monkeypatch.setattr(pb, "PLAYBOOKS", (broken, pb.BY_ID["wake_llama"]))
    a = {"id": "a1", "rule": "llama-server asleep", "message": "", "host": "box"}
    assert [p.id for p in pb.matching(a)] == ["wake_llama"]
