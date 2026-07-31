"""#471: pure interaction routing — auth gates, commands, confirmations."""
from __future__ import annotations

import discord_bot as db

CFG = {"enabled": True, "bot_token": "t", "guild_id": "g",
       "allowed_user_ids": ["111"], "allow_model_control": True}


def _ix(name, uid="111", opts=None, ix_type=2):
    return {"type": ix_type, "id": "ixid", "token": "ixtok",
            "member": {"user": {"id": uid}},
            "data": {"name": name,
                     "options": [{"name": k, "value": v}
                                 for k, v in (opts or {}).items()]}}


def _component(custom_id, uid="111"):
    return {"type": 3, "id": "ixid", "token": "ixtok",
            "member": {"user": {"id": uid}},
            "data": {"custom_id": custom_id}}


def test_ping_pongs_without_auth():
    r = db.route({"type": 1}, {"allowed_user_ids": []}, db.PendingActions())
    assert r["payload"] == {"type": 1}


def test_empty_allowlist_refuses_everyone():
    r = db.route(_ix("fleet"), dict(CFG, allowed_user_ids=[]),
                 db.PendingActions())
    assert r["kind"] == "respond"
    assert "allowed_user_ids" in r["payload"]["data"]["content"]
    assert r["payload"]["data"]["flags"] == db.EPHEMERAL


def test_unlisted_user_refused():
    r = db.route(_ix("fleet", uid="999"), CFG, db.PendingActions())
    assert "allowlist" in r["payload"]["data"]["content"]


def test_fleet_defers_ephemeral_by_default_public_on_request():
    r = db.route(_ix("fleet"), CFG, db.PendingActions())
    assert r == {"kind": "defer", "flags": db.EPHEMERAL, "update": False,
                 "job": {"kind": "fleet"}}
    r = db.route(_ix("fleet", opts={"public": True}), CFG,
                 db.PendingActions())
    assert r["flags"] == 0


def test_host_and_models_jobs_carry_args():
    r = db.route(_ix("host", opts={"name": "box"}), CFG, db.PendingActions())
    assert r["job"] == {"kind": "host", "name": "box"}
    r = db.route(_ix("models"), CFG, db.PendingActions())
    assert r["job"] == {"kind": "models", "host": None}
    r = db.route(_ix("models", opts={"host": "mac"}), CFG,
                 db.PendingActions())
    assert r["job"]["host"] == "mac"


def test_alarms_count_clamped_and_defaulted():
    r = db.route(_ix("alarms", opts={"count": 500}), CFG,
                 db.PendingActions())
    assert r["job"] == {"kind": "alarms", "count": 20}
    r = db.route(_ix("alarms", opts={"count": "x"}), CFG,
                 db.PendingActions())
    assert r["job"]["count"] == 5


def test_ack_and_silence_defer_with_alert_id():
    r = db.route(_ix("ack", opts={"alert_id": "a1"}), CFG,
                 db.PendingActions())
    assert r["job"] == {"kind": "ack", "alert_id": "a1"}
    r = db.route(_ix("silence", opts={"alert_id": "a2"}), CFG,
                 db.PendingActions())
    assert r["job"]["kind"] == "silence"


def test_control_gate_refuses_load_when_disabled():
    cfg = dict(CFG, allow_model_control=False)
    r = db.route(_ix("load", opts={"model": "m"}), cfg, db.PendingActions())
    assert "allow_model_control" in r["payload"]["data"]["content"]


def test_vllm_control_refused():
    r = db.route(_ix("load", opts={"model": "m", "provider": "vllm"}), CFG,
                 db.PendingActions())
    assert "read-only" in r["payload"]["data"]["content"]


def test_load_asks_for_confirmation_with_buttons():
    pending = db.PendingActions()
    r = db.route(_ix("load", opts={"model": "m", "host": "box"}), CFG,
                 pending, now=100.0, nonce_fn=lambda: "n1")
    data = r["payload"]["data"]
    assert "Load `m`" in data["content"] and "box" in data["content"]
    ids = [b["custom_id"] for b in data["components"][0]["components"]]
    assert ids == ["confirm:n1", "cancel:n1"]
    assert pending.peek("n1", 100.0)["job"]["model"] == "m"


def test_confirm_executes_the_stored_job():
    pending = db.PendingActions()
    db.route(_ix("unload", opts={"model": "m"}), CFG, pending,
             now=100.0, nonce_fn=lambda: "n1")
    r = db.route(_component("confirm:n1"), CFG, pending, now=110.0)
    assert r["kind"] == "defer" and r["update"] is True
    assert r["job"] == {"kind": "execute",
                        "job": {"kind": "unload", "provider": "llama",
                                "model": "m", "host": None}}
    assert pending.peek("n1", 110.0) is None


def test_cancel_clears_the_pending_action():
    pending = db.PendingActions()
    db.route(_ix("load", opts={"model": "m"}), CFG, pending,
             now=100.0, nonce_fn=lambda: "n1")
    r = db.route(_component("cancel:n1"), CFG, pending, now=110.0)
    assert r["payload"]["type"] == 7
    assert r["payload"]["data"]["content"] == "Cancelled."
    assert pending.peek("n1", 110.0) is None


def test_expired_confirmation_is_refused():
    pending = db.PendingActions()
    db.route(_ix("load", opts={"model": "m"}), CFG, pending,
             now=100.0, nonce_fn=lambda: "n1")
    r = db.route(_component("confirm:n1"), CFG, pending,
                 now=100.0 + db.CONFIRM_TTL_S + 1)
    assert "Expired" in r["payload"]["data"]["content"]


def test_other_users_click_is_refused_and_action_survives():
    cfg = dict(CFG, allowed_user_ids=["111", "222"])
    pending = db.PendingActions()
    db.route(_ix("load", opts={"model": "m"}), cfg, pending,
             now=100.0, nonce_fn=lambda: "n1")
    r = db.route(_component("confirm:n1", uid="222"), cfg, pending, now=110.0)
    assert "requesting user" in r["payload"]["data"]["content"]
    assert pending.peek("n1", 110.0) is not None


def test_unknown_command_refused():
    r = db.route(_ix("nope"), CFG, db.PendingActions())
    assert "Unknown command" in r["payload"]["data"]["content"]
