"""#1031 Tower effort: the tier picked from the Tower model's measured quality."""
from __future__ import annotations

import pytest

import forecast_effort as fe

# score_pct, size_b, tool_grade, tok_s, tier
AUTO = [
    (None, 9.0, "native", None, "light"),              # a 9B model with no evaluation
    (82.0, 9.0, "native", None, "standard"),           # evaluated well enough
    (None, 14.0, "native", None, "standard"),          # big enough on size alone
    (None, 32.0, "native", None, "full"),
    (None, 32.0, "fenced", None, "full"),              # fenced tool calling still passes
    (None, 32.0, "pending", None, "standard"),         # tool check not passed yet
    (None, 32.0, "failed", None, "standard"),
    (None, 32.0, "unknown", None, "standard"),
    (None, 32.0, None, None, "standard"),
    (93.0, 9.0, "native", 8.0, "light"),               # too slow for standard
    (93.0, 32.0, "native", 4.0, "light"),              # far too slow for anything
    (93.0, 32.0, "native", 20.0, "standard"),          # fast enough for standard, not full
    (93.0, 32.0, "native", 40.0, "full"),
    (None, None, None, None, "light"),                 # nothing measured at all
    (74.9, 11.9, "native", None, "light"),             # just under both thresholds
    (75.0, None, "native", None, "standard"),
    (90.0, None, "native", None, "full"),
]


@pytest.mark.parametrize("score,size,grade,tok,tier", AUTO)
def test_auto_tier_table(score, size, grade, tok, tier):
    got, reason = fe.auto_tier(score_pct=score, size_b=size, tool_grade=grade, tok_s=tok)
    assert got == tier
    assert reason.startswith(fe.LABELS[tier] + " — ")


def test_auto_tier_reason_is_plain_and_names_what_it_used():
    _t, reason = fe.auto_tier(score_pct=93.0, size_b=32.0, tool_grade="native", tok_s=None)
    assert reason == "Full — 32B model, evaluation 93 %, tool check passed"
    _t, reason = fe.auto_tier(score_pct=None, size_b=9.0, tool_grade=None, tok_s=None)
    assert reason == "Light — 9B model, no evaluation yet"
    _t, reason = fe.auto_tier(score_pct=93.0, size_b=32.0, tool_grade="native", tok_s=8.0)
    assert reason.endswith("measured speed is low")
    _t, reason = fe.auto_tier(score_pct=None, size_b=32.0, tool_grade="pending", tok_s=None)
    assert "tool check pending" in reason


def test_auto_tier_ignores_unreadable_signals():
    assert fe.auto_tier(score_pct="high", size_b="big", tool_grade=7, tok_s="fast")[0] == "light"
    assert fe.auto_tier(score_pct=float("nan"), size_b=float("inf"), tool_grade=None, tok_s=None)[0] == "light"


def test_profiles_cover_every_tier_and_carry_the_briefed_parameters():
    assert tuple(fe.PROFILES) == fe.TIERS == ("off", "light", "standard", "full")
    off, light, std, full = (fe.PROFILES[t] for t in fe.TIERS)
    assert (off.path, off.thinking, off.max_tokens, off.timeout_s) == ("none", "off", 0, 0)
    assert (light.path, light.thinking, light.max_tokens, light.timeout_s) == ("direct", "off", 400, 60)
    assert (light.batch_size, light.max_batches, light.per_finding_cause) == (8, 1, False)
    assert (std.path, std.thinking, std.max_tokens, std.timeout_s) == ("direct", "off", 1200, 90)
    assert (std.batch_size, std.max_batches, std.per_finding_cause) == (6, 2, True)
    assert (full.path, full.thinking, full.max_tokens, full.timeout_s) == ("conversation", "medium", 2048, 120)
    assert (full.max_tool_calls, full.investigate, full.model_only) == (8, True, True)
    assert [p.tier for p in fe.PROFILES.values()] == list(fe.TIERS)
    assert not (off.investigate or off.per_finding_cause or off.model_only)
    assert not (light.investigate or light.model_only or std.investigate or std.model_only)


def test_direct_thinking_and_timeouts_are_fixed_per_tier():
    assert fe.DIRECT_THINKING == "off"
    assert fe.DIRECT_TIMEOUT_S == {"light": 60, "standard": 90, "full": 90}


@pytest.mark.parametrize("setting,tier,reason", [("off", "off", "Set to Off in Settings"),
                                                 ("light", "light", "Set to Light in Settings"),
                                                 ("standard", "standard", "Set to Standard in Settings"),
                                                 ("full", "full", "Set to Full in Settings")])
def test_pick_takes_a_manual_setting_as_it_is(setting, tier, reason):
    strong = {"score_pct": 95.0, "size_b": 70.0, "tool_grade": "native", "tok_s": 100.0}
    prof, got = fe.pick(setting, strong, None)
    assert prof is fe.PROFILES[tier] and got == reason
    # a manual setting is never lowered by an earlier timeout
    assert fe.pick(setting, strong, "full")[0] is fe.PROFILES[tier]


def test_pick_auto_uses_the_signals():
    prof, reason = fe.pick("auto", {"score_pct": None, "size_b": 9.0, "tool_grade": "native", "tok_s": None}, None)
    assert prof is fe.PROFILES["light"] and reason.startswith("Light — ")
    prof, _r = fe.pick("auto", {"score_pct": 93.0, "size_b": 32.0, "tool_grade": "native", "tok_s": 40.0}, None)
    assert prof is fe.PROFILES["full"]


def test_pick_falls_back_to_auto_for_an_unknown_or_missing_setting():
    signals = {"score_pct": 82.0, "size_b": 9.0, "tool_grade": "native", "tok_s": None}
    for setting in ("auto", "", None, "sideways", "AUTO"):
        assert fe.pick(setting, signals, None)[0] is fe.PROFILES["standard"]
    assert fe.pick("auto", None, None)[0] is fe.PROFILES["light"]


def test_pick_drops_one_tier_below_the_tier_that_timed_out():
    strong = {"score_pct": 93.0, "size_b": 32.0, "tool_grade": "native", "tok_s": 40.0}
    prof, reason = fe.pick("auto", strong, "full")
    assert prof is fe.PROFILES["standard"] and reason.startswith("Standard — ")
    assert reason.endswith(" — lowered after a timeout")
    mid = {"score_pct": 82.0, "size_b": 9.0, "tool_grade": "native", "tok_s": None}
    prof, reason = fe.pick("auto", mid, "standard")
    assert prof is fe.PROFILES["light"] and reason.endswith(" — lowered after a timeout")
    weak = {"score_pct": None, "size_b": 9.0, "tool_grade": None, "tok_s": None}
    prof, reason = fe.pick("auto", weak, "light")
    assert prof is fe.PROFILES["light"] and reason.endswith(" — lowered after a timeout")


def test_pick_keeps_an_auto_tier_already_below_the_one_that_timed_out():
    weak = {"score_pct": None, "size_b": 9.0, "tool_grade": None, "tok_s": None}
    prof, reason = fe.pick("auto", weak, "full")
    assert prof is fe.PROFILES["light"] and "lowered after a timeout" not in reason


def test_pick_ignores_an_unreadable_timeout_tier():
    strong = {"score_pct": 93.0, "size_b": 32.0, "tool_grade": "native", "tok_s": 40.0}
    for last in (None, "", "sideways", 7):
        assert fe.pick("auto", strong, last)[0] is fe.PROFILES["full"]
