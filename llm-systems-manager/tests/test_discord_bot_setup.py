"""#471: command schemas, pending-action TTL store, and config guards."""
from __future__ import annotations

import discord_bot as db


def test_schemas_cover_the_command_surface():
    names = [c["name"] for c in db.command_schemas()]
    assert names == ["fleet", "host", "models", "load", "unload",
                     "alarms", "ack", "silence"]


def test_schema_required_options():
    by_name = {c["name"]: c for c in db.command_schemas()}
    host_opts = {o["name"]: o for o in by_name["host"]["options"]}
    assert host_opts["name"]["required"] is True
    load_opts = {o["name"]: o for o in by_name["load"]["options"]}
    assert load_opts["model"]["required"] is True
    assert load_opts["host"]["required"] is False
    choices = [c["value"] for c in load_opts["provider"]["choices"]]
    assert choices == ["llama", "lms"]


def test_schemas_are_json_serializable():
    import json
    json.dumps(db.command_schemas())


def test_pending_ttl_expiry_and_pop():
    p = db.PendingActions(ttl_s=10.0)
    p.put("n1", {"kind": "load"}, "u1", now=100.0)
    assert p.peek("n1", 105.0)["user_id"] == "u1"
    assert p.peek("n1", 111.0) is None
    p.put("n2", {"kind": "load"}, "u1", now=200.0)
    first = p.pop("n2", 205.0)
    second = p.pop("n2", 205.0)
    assert first is not None
    assert second is None


def test_pending_put_prunes_expired_entries():
    p = db.PendingActions(ttl_s=10.0)
    p.put("old", {"kind": "load"}, "u1", now=100.0)
    p.put("new", {"kind": "load"}, "u1", now=150.0)
    assert "old" not in p._items and "new" in p._items


def test_bot_config_defaults_without_ctx():
    cfg = db.bot_config(None)
    assert cfg == {"enabled": False, "bot_token": "", "guild_id": "",
                   "allowed_user_ids": [], "allow_model_control": False}


def test_bot_config_reads_ctx_and_stringifies_ids():
    class _Obj:
        pass

    ctx = _Obj(); ctx.settings = _Obj(); ctx.settings.manager = _Obj()
    d = _Obj()
    d.enabled = True
    d.bot_token = "tok"
    d.guild_id = 12345
    d.allowed_user_ids = [111, "222"]
    d.allow_model_control = True
    ctx.settings.manager.discord = d
    cfg = db.bot_config(ctx)
    assert cfg["enabled"] is True and cfg["guild_id"] == "12345"
    assert cfg["allowed_user_ids"] == ["111", "222"]
    assert cfg["allow_model_control"] is True


def test_start_thread_is_a_pytest_noop():
    assert db.start_thread(None) is None


def test_config_model_accepts_unquoted_snowflake_ids():
    # Operators paste Discord "Copy ID" values unquoted; ints must not
    # fail validation and take the whole config down with them.
    from config.unified_config import ManagerDiscord
    m = ManagerDiscord(enabled=True, guild_id=1486111775408656434,
                       allowed_user_ids=[1486111775408656435, "999"])
    assert m.guild_id == "1486111775408656434"
    assert m.allowed_user_ids == ["1486111775408656435", "999"]
