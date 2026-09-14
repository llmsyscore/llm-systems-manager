"""#924 Tower playbooks: declarative fixes matched against an alert row; steps name act tools."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

_MODEL_IN_TEXT = re.compile(r"\bmodel\s*[:=]?\s*[`'\"]?([A-Za-z0-9][\w.\-:/]{2,})", re.I)


def _text(alert: dict) -> str:
    return " ".join(str(alert.get(k) or "") for k in ("rule", "message", "metric")).lower()


def _any(alert: dict, *words: str) -> bool:
    t = _text(alert)
    return any(w in t for w in words)


def _model_of(alert: dict) -> Optional[str]:
    m = _MODEL_IN_TEXT.search(str(alert.get("message") or ""))
    return m.group(1) if m else None


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_ENGINE_WORDS = ("threshold", "z-score", "deviat", "percentile", "outside range", "rate")


def _recovered(alert: dict) -> bool:
    """Threshold-evaluator alerts compare value to threshold; other engine alerts never match; the rest need recovery wording."""
    msg = str(alert.get("message") or "").lower()
    val, thr = _num(alert.get("value")), _num(alert.get("threshold"))
    if msg.startswith("value ") and " exceeds threshold " in msg:
        return val is not None and thr is not None and val <= thr
    if msg.startswith("value ") and " falls below threshold " in msg:
        return val is not None and thr is not None and val >= thr
    if any(w in msg for w in _ENGINE_WORDS):
        return False
    return _any(alert, "recovered", "back in range", "cleared", "back to normal")


@dataclass(frozen=True)
class Playbook:
    id: str
    title: str
    match: Callable[[dict], bool]
    steps: "tuple[tuple[str, dict], ...]"   # (act tool, args template; "{host}" "{model}" "{alert_id}" fill from the alert)
    safe: bool
    explain: str
    requires_pin: bool = False


PLAYBOOKS: "tuple[Playbook, ...]" = (
    Playbook("wake_llama", "Wake llama-server",
             lambda a: bool(a.get("host")) and _any(a, "llama") and _any(a, "asleep", "sleep", "idle"),
             (("wake_server", {"host": "{host}"}),), True,
             "llama-server went to idle sleep; a wake restores first-token latency."),
    Playbook("reload_lms_model", "Reload model",
             lambda a: bool(a.get("host")) and _any(a, "lms", "lm studio")
             and _any(a, "unload", "dropped", "not loaded", "missing") and _model_of(a) is not None,
             (("load_model", {"provider": "lms", "host": "{host}", "model": "{model}"}),), True,
             "LM Studio dropped a model that is pinned to this host; load it again.", requires_pin=True),
    Playbook("ack_after_recovery", "Acknowledge alert",
             lambda a: str(a.get("status") or "") == "active" and _recovered(a),
             (("ack_alert", {"alert_id": "{alert_id}"}),), True,
             "The metric is back in range; acknowledge the alert so it stops paging."),
    Playbook("restart_llama", "Restart llama-server",
             lambda a: bool(a.get("host")) and _any(a, "llama")
             and _any(a, "unhealthy", "not responding", "unreachable", "crash", "error", "timeout"),
             (("restart_provider", {"provider": "llama", "host": "{host}"}),), False,
             "llama-server is unhealthy; a restart drops in-flight requests but recovers the host."),
)
BY_ID: "dict[str, Playbook]" = {p.id: p for p in PLAYBOOKS}


def fields(alert: dict) -> dict:
    """Template values a step may use; a value that is unknown is absent, not empty."""
    out = {}
    for k, v in (("host", alert.get("host")), ("alert_id", alert.get("id")), ("model", _model_of(alert))):
        if v:
            out[k] = str(v)
    return out


def matching(alert: dict) -> "list[Playbook]":
    out = []
    for p in PLAYBOOKS:
        try:
            if p.match(alert):
                out.append(p)
        except Exception:  # noqa: BLE001 — one matcher never breaks the watcher
            continue
    return out


def render(pb: Playbook, alert: dict,
           pinned: "Optional[Callable[[str, str, str], bool]]" = None) -> "Optional[list[tuple[str, dict]]]":
    """Fills each step's template from the alert; None when a value is missing or a required pin is absent."""
    vals = fields(alert)
    steps = []
    for tool, tmpl in pb.steps:
        args = {}
        for k, v in tmpl.items():
            if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                key = v[1:-1]
                if key not in vals:
                    return None
                args[k] = vals[key]
            else:
                args[k] = v
        if pb.requires_pin and "model" in args:
            try:
                if pinned is None or not pinned(args.get("provider", ""), args.get("host", ""), args["model"]):
                    return None
            except Exception:  # noqa: BLE001 — an unreadable pin never permits a load
                return None
        steps.append((tool, args))
    return steps


def prompt_lines(pbs: "list[Playbook]") -> str:
    if not pbs:
        return "No playbook matches this alert."
    return "Playbooks that match (name one by id only if it fits):\n" + "\n".join(
        f"- {p.id}: {p.title} — {p.explain}" + ("" if p.safe else " (not safe: admins only)") for p in pbs)
