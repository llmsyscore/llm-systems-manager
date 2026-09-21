"""Forecast Tower effort (#1031): the tier one run works at, from the Tower model's measured quality.
Pure — no I/O, no model call; the caller passes the signals in."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

TIERS = ("off", "light", "standard", "full")
LABELS = {"off": "Off", "light": "Light", "standard": "Standard", "full": "Full"}
PASSING_GRADES = ("native", "fenced")
STRONG_SCORE, MID_SCORE = 90.0, 75.0
STRONG_SIZE, MID_SIZE = 30.0, 12.0
FULL_TOK_S, STANDARD_TOK_S, FLOOR_TOK_S = 25.0, 12.0, 5.0
TIMEOUT_SUFFIX = " — lowered after a timeout"


@dataclass(frozen=True)
class Profile:
    """Everything one run's Tower part is allowed to do."""
    tier: str
    path: str                # "direct" | "conversation" | "none"
    thinking: str            # "off" | "low" | "medium"
    max_tokens: int
    timeout_s: int
    batch_size: int
    max_batches: int
    max_tool_calls: int
    per_finding_cause: bool
    investigate: bool
    model_only: bool


PROFILES = {
    "off": Profile("off", "none", "off", 0, 0, 0, 0, 0, False, False, False),
    "light": Profile("light", "direct", "off", 400, 60, 8, 1, 0, False, False, False),
    "standard": Profile("standard", "direct", "off", 1200, 90, 6, 2, 0, True, False, False),
    "full": Profile("full", "conversation", "medium", 2048, 120, 6, 2, 8, True, True, True),
}

# Every DIRECT call (digest, cause batches) forces thinking off and uses its own wall-clock cap by tier.
DIRECT_THINKING = "off"
DIRECT_TIMEOUT_S = {"light": 60, "standard": 90, "full": 90}


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _round(value: float) -> str:
    return f"{value:g}"


def _evidence(score: Optional[float], size: Optional[float], grade: Any, slow: bool) -> str:
    """The plain sentence an operator reads: what was known about the model and what held it back."""
    bits = []
    if size:
        bits.append(f"{_round(size)}B model")
    bits.append(f"evaluation {_round(score)} %" if score is not None else "no evaluation yet")
    name = str(grade or "").strip().lower()
    if name:
        bits.append("tool check passed" if name in PASSING_GRADES else f"tool check {name}")
    if slow:
        bits.append("measured speed is low")
    return ", ".join(bits)


def auto_tier(*, score_pct: Optional[float], size_b: Optional[float], tool_grade: Optional[str],
              tok_s: Optional[float]) -> "tuple[str, str]":
    """The tier the model's own numbers earn, and the reason to show the operator."""
    score, size, tok = _num(score_pct), _num(size_b) or 0.0, _num(tok_s)
    strong = (score is not None and score >= STRONG_SCORE) or size >= STRONG_SIZE
    mid = (score is not None and score >= MID_SCORE) or size >= MID_SIZE
    tool_ok = str(tool_grade or "").strip().lower() in PASSING_GRADES
    want = "full" if (strong and tool_ok) else ("standard" if (mid or strong) else "light")
    tier = want
    if tok is not None:
        if tok < FLOOR_TOK_S:
            tier = "light"
        elif tier == "full" and tok < FULL_TOK_S:
            tier = "standard"
        if tier == "standard" and tok < STANDARD_TOK_S:
            tier = "light"
    return tier, f"{LABELS[tier]} — {_evidence(score, size, tool_grade, tier != want)}"


def pick(setting: str, signals: Optional[dict], last_timeout_tier: Optional[str]) -> "tuple[Profile, str]":
    """The profile one run works at: a chosen level as it stands, else the model's own tier, dropped one
    step when the same tier timed out last time."""
    chosen = str(setting or "auto").strip().lower()
    if chosen in PROFILES:
        return PROFILES[chosen], f"Set to {LABELS[chosen]} in Settings"
    got = signals or {}
    tier, reason = auto_tier(score_pct=got.get("score_pct"), size_b=got.get("size_b"),
                             tool_grade=got.get("tool_grade"), tok_s=got.get("tok_s"))
    last = str(last_timeout_tier or "").strip().lower()
    if last in TIERS and TIERS.index(tier) >= TIERS.index(last):
        lowered = TIERS[max(1, TIERS.index(last) - 1)]
        reason = f"{LABELS[lowered]} — {reason.split(' — ', 1)[-1]}{TIMEOUT_SUFFIX}"
        tier = lowered
    return PROFILES[tier], reason
