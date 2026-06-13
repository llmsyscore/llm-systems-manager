"""OpenClaw analytics for the LLM Systems Manager.

Owns the single `/api/openclaw/analytics` endpoint and every helper that
feeds it — cross-agent fan-out, cross-host merges, anomaly + trend
detection. The frontend Dashboard's OpenClaw card polls this endpoint
every 2 seconds when the OpenClaw sub-tab is visible.

Data source: fan out to every approved agent that advertises the
openclaw capability, fetch each host's `/openclaw/aggregate`, then merge
the per-host sessions / flows / tasks / delivery summaries into the
shape the frontend expects.

Cache:
  _openclaw_agg_cache    — full aggregation payload (5 s TTL)

Wired by main via `openclaw.register_routes(app, ctx)`. The only cross-
module dep beyond ctx is `agent_registry` (used by the fan-out helpers
for `load_agents` and `agent_request`). No `current_app` needed — the
route just `jsonify`s a dict.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from flask import jsonify

import agent_registry  # type: ignore[import-not-found]  # sibling

log = logging.getLogger("llm-systems-manager.openclaw")

__all__ = ["register_routes"]


# ── In-process cache (TTL-invalidated on every read) ────────────────

_openclaw_agg_cache: dict  = {"ts": 0, "payload": None}


# ── Dep namespace ───────────────────────────────────────────────────

_deps = SimpleNamespace()


def _analyze_usage_trends(daily_tokens):
    """Rolling 7-day trend + monthly prediction. daily_tokens: {YYYY-MM-DD: int}."""
    if not daily_tokens or len(daily_tokens) < 3:
        return {"trend": "insufficient_data", "dailyAvg": 0, "monthlyPrediction": 0}
    recent_days = sorted(daily_tokens.items())[-7:]
    if len(recent_days) < 3:
        return {"trend": "insufficient_data", "dailyAvg": 0, "monthlyPrediction": 0}
    series = [v for _, v in recent_days]
    recent_avg = sum(series[-3:]) / 3
    older_avg = (sum(series[:-3]) / max(1, len(series) - 3)) if len(series) > 3 else recent_avg
    if recent_avg > older_avg * 1.2:
        trend = "increasing"
    elif recent_avg < older_avg * 0.8:
        trend = "decreasing"
    else:
        trend = "stable"
    daily_avg = sum(series) / len(series)
    return {"trend": trend, "dailyAvg": int(daily_avg), "monthlyPrediction": int(daily_avg * 30)}
# ----------------------------------------------------------------------------


def _project_monthly_cost(daily_cost: dict):
    """7-day linear extrapolation of agent daily cost to a 30-day projection.

    Returns None when fewer than 3 of the last 7 days have non-zero cost,
    to avoid misleading projections from sparse data.
    Adapted from clawmetry dashboard.py _get_cost_summary projected field.
    """
    today = datetime.now(timezone.utc).date()
    last7 = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    vals = [daily_cost.get(d, 0.0) for d in last7]
    if sum(1 for v in vals if v > 0) < 3:
        return None  # not enough data for a meaningful projection
    return round(sum(vals) / 7 * 30, 2)


def _detect_cost_anomalies(agents: list) -> list:
    """Flag sessions whose cost is more than 2x the rolling prior-session average.

    Collects all recent sessions across agents, sorts by timestamp, then for each
    session computes the rolling average of the 20 sessions before it. Sessions
    that exceed 2x that average AND cost more than $0.05 are flagged.

    Minimum prior window of 3 sessions required to avoid false positives on
    early sessions with no baseline. Returns top 10 by ratio, descending.
    Ported from clawmetry dashboard.py _compute_session_cost_anomalies (line 10316).
    """
    # Flatten all recent sessions across agents, tag with agent name
    all_sessions = sorted(
        [dict(s, agent=a["name"]) for a in agents for s in a.get("recent", [])],
        key=lambda x: x.get("last_ts") or "",
    )
    anomalies = []
    for i, sess in enumerate(all_sessions):
        cost = sess.get("cost", 0)
        if cost <= 0:
            continue
        # Rolling window: up to 20 sessions before this one that have a cost
        prior = [s["cost"] for s in all_sessions[max(0, i - 20):i]
                 if s.get("cost", 0) > 0]
        if len(prior) < 3:
            continue  # not enough baseline data
        avg = sum(prior) / len(prior)
        if cost > avg * 2.0 and cost > 0.05:
            anomalies.append({
                "session_id": sess["id"],
                "agent":      sess["agent"],
                "cost":       round(cost, 4),
                "avg":        round(avg, 4),
                "ratio":      round(cost / avg, 1),
                "ts":         sess.get("last_ts"),
            })
    return sorted(anomalies, key=lambda x: x["ratio"], reverse=True)[:10]


def _compute_tool_trends(agents: list) -> dict:
    """Compare per-tool call counts in the last 7 days vs the prior 7 days.

    Uses each agent's daily_tools dict (date-bucketed tool counts accumulated
    during session parsing) so attribution is accurate — not approximated from
    all-time totals. Returns a dict of {tool_name: direction} where direction
    is one of: 'up', 'down', 'stable', 'new' (no prior activity), 'gone'
    (no recent activity). Thresholds: >20% change = up/down, else stable.
    Ported from clawmetry dashboard.py _compute_plugin_trend (line 10616).
    """
    today = datetime.now(timezone.utc).date()
    recent_cut = (today - timedelta(days=7)).isoformat()   # last 7 days
    older_cut  = (today - timedelta(days=14)).isoformat()  # prior 7 days

    rc: dict = {}  # recent-period tool counts
    oc: dict = {}  # older-period tool counts

    for ag in agents:
        for day, tools in ag.get("daily_tools", {}).items():
            for tool, count in tools.items():
                if day >= recent_cut:
                    rc[tool] = rc.get(tool, 0) + count
                elif day >= older_cut:
                    oc[tool] = oc.get(tool, 0) + count

    trends: dict = {}
    for tool in set(list(rc) + list(oc)):
        r, o = rc.get(tool, 0), oc.get(tool, 0)
        if o == 0:
            trends[tool] = "new"      # appeared only in the recent window
        elif r == 0:
            trends[tool] = "gone"     # used before but not in last 7 days
        elif r > o * 1.2:
            trends[tool] = "up"       # ≥20% more calls in recent vs prior
        elif r < o * 0.8:
            trends[tool] = "down"     # ≥20% fewer calls
        else:
            trends[tool] = "stable"
    return trends


def _oc_merge_flows(per_host: list[dict]) -> dict:
    """Sum flows summaries across hosts. by_status counts add; recent
    items are concatenated then re-sorted/truncated."""
    by_status: dict[str, int] = {}
    recent: list[dict] = []
    total = 0
    for h in per_host:
        for k, v in (h.get("by_status") or {}).items():
            by_status[k] = by_status.get(k, 0) + (v or 0)
        recent.extend(h.get("recent") or [])
        total += int(h.get("total") or 0)
    recent.sort(key=lambda r: r.get("created_iso") or "", reverse=True)
    return {"total": total, "by_status": by_status, "recent": recent[:20]}


def _oc_merge_tasks(per_host: list[dict]) -> dict:
    """Sum task summaries across hosts. avg_duration_s becomes a
    weighted average — we lack per-host sample counts, so we approximate
    via the unweighted mean of non-null entries, accepting some
    imprecision for the convenience of a single number."""
    by_status: dict[str, int] = {}
    by_runtime: dict[str, int] = {}
    fails: list[dict] = []
    durs: list[float] = []
    total = 0
    for h in per_host:
        for k, v in (h.get("by_status") or {}).items():
            by_status[k] = by_status.get(k, 0) + (v or 0)
        for k, v in (h.get("by_runtime") or {}).items():
            by_runtime[k] = by_runtime.get(k, 0) + (v or 0)
        fails.extend(h.get("recent_failures") or [])
        d = h.get("avg_duration_s")
        if isinstance(d, (int, float)):
            durs.append(float(d))
        total += int(h.get("total") or 0)
    fails.sort(key=lambda r: r.get("created_iso") or "", reverse=True)
    avg_dur = round(sum(durs) / len(durs), 2) if durs else None
    return {"total": total, "by_status": by_status, "by_runtime": by_runtime,
            "avg_duration_s": avg_dur, "recent_failures": fails[:10]}


def _oc_merge_delivery(per_host: list[dict]) -> dict:
    """Sum delivery-queue summaries. Common errors concatenated then
    re-aggregated. Oldest enqueue across all hosts wins."""
    by_channel: dict[str, int] = {}
    total_retries = 0
    err_counts: dict[str, int] = {}
    oldest_iso: "str | None" = None
    total = 0
    for h in per_host:
        for k, v in (h.get("by_channel") or {}).items():
            by_channel[k] = by_channel.get(k, 0) + (v or 0)
        total_retries += int(h.get("total_retries") or 0)
        for err in h.get("common_errors") or []:
            err_counts[err["error"]] = err_counts.get(err["error"], 0) + int(err.get("count") or 0)
        oe = h.get("oldest_enqueue_iso")
        if oe and (oldest_iso is None or oe < oldest_iso):
            oldest_iso = oe
        total += int(h.get("total") or 0)
    common = sorted(err_counts.items(), key=lambda x: -x[1])[:3]
    return {"total": total, "by_channel": by_channel, "total_retries": total_retries,
            "common_errors": [{"error": k, "count": v} for k, v in common],
            "oldest_enqueue_iso": oldest_iso}


def _oc_velocity_from_agents(sessions_pre: "list[tuple[str, dict]]") -> dict:
    """Velocity calculation from already-parsed session aggregates: 1-hour
    window over per-session last_ts. Active-session count = sessions whose
    last_ts is within the last hour."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    tokens_1h = 0
    sessions_active = 0
    for _ad, parsed in sessions_pre:
        lt = parsed.get("last_ts") or ""
        if lt < cutoff:
            continue
        sessions_active += 1
        # Daily buckets include per-day input+output; sum the most
        # recent day's tokens as a 1h approximation (close enough).
        days = parsed.get("daily") or {}
        if days:
            last_day = sorted(days.keys())[-1]
            d = days[last_day]
            tokens_1h += (d.get("input", 0) or 0) + (d.get("output", 0) or 0)
    return {"tokens_1h": tokens_1h, "active_sessions": sessions_active,
            "tokens_per_min": round(tokens_1h / 60, 1)}


def _openclaw_capable_agents() -> list[dict]:
    """Approved+live agents that advertise the openclaw capability."""
    data = agent_registry.load_agents()
    out = []
    for a in (data.get("agents") or {}).values():
        if a.get("status") != "approved":
            continue
        caps = a.get("capabilities") or {}
        if caps.get("openclaw"):
            out.append(a)
    return out


def _openclaw_fetch_from_agents() -> list[dict]:
    """Hit each openclaw-capable agent's /openclaw/aggregate and return
    the list of per-host aggregate payloads. The caller pulls each host's
    ``sessions`` / ``flows`` / ``tasks`` / ``delivery`` sub-dicts out as
    it needs them. Failed hosts are silently dropped (they just don't
    contribute).
    """
    hosts_data: list[dict] = []
    for agent in _openclaw_capable_agents():
        resp, _tried, _err = agent_registry.agent_request(
            "GET", agent, "/openclaw/aggregate",
            headers={"Authorization": f"Bearer {agent['token']}"},
            timeout=20,  # large jsonl-bearing hosts can take a moment
        )
        if resp is None or not resp.ok:
            # Parens needed: `if-else` binds looser than `or`, so unparenthesized
            # the expression dropped `_err` whenever resp was None.
            log.warning("openclaw aggregate fetch failed for %s: %s",
                        agent.get("hostname"),
                        _err or (resp.status_code if resp else "no-response"))
            continue
        try:
            hosts_data.append(resp.json())
        except Exception as e:
            log.warning("openclaw aggregate decode failed for %s: %s", agent.get("hostname"), e)
    return hosts_data


def _collect_openclaw_analytics() -> dict:
    """Fan out to every approved openclaw-capable agent, fetch each host's
    /openclaw/aggregate, then merge per-host sessions / flows / tasks /
    delivery summaries into the dashboard payload shape.
    """
    now = time.time()
    if _openclaw_agg_cache["payload"] and (now - _openclaw_agg_cache["ts"]) < 5:
        return _openclaw_agg_cache["payload"]

    agents_out = []
    totals = {"sessions": 0, "messages": 0, "input": 0, "output": 0,
              "cacheRead": 0, "cacheWrite": 0, "cost": 0.0, "tool_uses": 0}

    tool_totals:  dict = {}
    daily_cost:   dict = {}
    daily_tokens: dict = {}

    # Each entry is a tuple of (agent_dir_name, parsed_session_dict).
    sessions_pre: list[tuple[str, dict]] = []

    hosts = _openclaw_fetch_from_agents()
    for hp in hosts:
        for sess in hp.get("sessions") or []:
            ad = sess.get("agent_dir") or hp.get("host", "unknown")
            sessions_pre.append((ad, sess))
    merged_flows    = _oc_merge_flows([hp.get("flows", {})    for hp in hosts])
    merged_tasks    = _oc_merge_tasks([hp.get("tasks", {})    for hp in hosts])
    merged_delivery = _oc_merge_delivery([hp.get("delivery", {}) for hp in hosts])

    # Group parsed sessions by agent_dir name and run the aggregation loop.
    # Sessions are stored as (agent_dir_name, parsed_dict). We process them
    # in batches per agent_dir so the per-agent stats accumulator works.
    sessions_by_agent: dict[str, list[dict]] = {}
    for ad_name, parsed in sessions_pre:
        sessions_by_agent.setdefault(ad_name, []).append(parsed)

    for agent_dir_name in sorted(sessions_by_agent.keys()):
        parsed_list = sessions_by_agent[agent_dir_name]
        a = {
            "name": agent_dir_name,
            "sessions": 0, "messages": 0,
            "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
            "cost": 0.0, "tool_uses": 0,
            "models": set(),
            "last_ts": None,
            "recent": [],
            "tools": {},
            "daily_cost": {},
            "models_cost": {},
            # --- new fields for enhanced analytics ---
            "thinking_events": 0,  # total extended-thinking blocks across all sessions
            "thinking_chars":  0,  # total thinking text chars (token volume proxy)
            "daily_input":  {},    # {YYYY-MM-DD: int} input tokens per day
            "daily_output": {},    # {YYYY-MM-DD: int} output tokens per day
            "daily_tools":  {},    # {YYYY-MM-DD: {tool: count}} for trend attribution
        }
        session_summaries = []

        for parsed in parsed_list:
            a["sessions"]   += 1
            a["messages"]   += parsed["messages"]
            a["input"]      += parsed["input"]
            a["output"]     += parsed["output"]
            a["cacheRead"]  += parsed["cacheRead"]
            a["cacheWrite"] += parsed["cacheWrite"]
            a["cost"]       += parsed["cost"]
            a["tool_uses"]  += parsed["tool_uses"]
            for m in parsed["models"]:
                a["models"].add(m)
            lt = parsed["last_ts"]
            if lt and (not a["last_ts"] or lt > a["last_ts"]):
                a["last_ts"] = lt

            # Merge tool attribution
            for tn, tc in (parsed.get("tools") or {}).items():
                a["tools"][tn] = a["tools"].get(tn, 0) + tc
                tool_totals[tn] = tool_totals.get(tn, 0) + tc

            # Merge daily buckets (cost + tokens)
            for day, db in (parsed.get("daily") or {}).items():
                ac = a["daily_cost"].get(day, 0.0) + db.get("cost", 0.0)
                a["daily_cost"][day] = ac
                daily_cost[day]   = daily_cost.get(day, 0.0) + db.get("cost", 0.0)
                daily_tokens[day] = daily_tokens.get(day, 0) + db.get("tokens", 0)

            # Merge per-model cost
            for mn, mc in (parsed.get("models_cost") or {}).items():
                a["models_cost"][mn] = a["models_cost"].get(mn, 0.0) + mc

            # --- new: thinking flow accumulation ---
            # Sum thinking_events and thinking_chars from each parsed session
            # so we can rank agents by extended-thinking activity.
            a["thinking_events"] += parsed.get("thinking_events", 0)
            a["thinking_chars"]  += parsed.get("thinking_chars", 0)

            # --- new: daily input/output split ---
            # Needed to feed _analyze_usage_trends with real token counts
            # (not cost-proxies) when computing per-agent trend below.
            for day, db in (parsed.get("daily") or {}).items():
                a["daily_input"][day]  = a["daily_input"].get(day, 0)  + db.get("input", 0)
                a["daily_output"][day] = a["daily_output"].get(day, 0) + db.get("output", 0)

            # --- new: date-bucketed tool counts ---
            # tool_breakdown is the raw {tool: count} dict from the agent's
            # per-session aggregate. Bucket it by the session's last_ts date so
            # _compute_tool_trends compares recent-7d vs prior-7d accurately.
            sess_day = (parsed.get("last_ts") or "")[:10]
            if sess_day:
                dt = a["daily_tools"].setdefault(sess_day, {})
                for tool, cnt in (parsed.get("tool_breakdown") or {}).items():
                    dt[tool] = dt.get(tool, 0) + cnt

            # Include thinking fields in session summaries so thinking_sessions
            # filter in the assembly block below finds them correctly.
            session_summaries.append({
                "id": parsed["session_id"],
                "messages": parsed["messages"],
                "input": parsed["input"],
                "output": parsed["output"],
                "cacheRead": parsed["cacheRead"],
                "cacheWrite": parsed["cacheWrite"],
                "cost": round(parsed["cost"], 6),
                "tool_uses": parsed["tool_uses"],
                "first_ts": parsed["first_ts"],
                "last_ts": parsed["last_ts"],
                "models": parsed["models"],
                "thinking_events": parsed.get("thinking_events", 0),
                "thinking_chars":  parsed.get("thinking_chars", 0),
            })

        session_summaries.sort(key=lambda s: s["last_ts"] or "", reverse=True)
        a["recent"]  = session_summaries[:10]
        a["models"]  = sorted(a["models"])
        a["cost"]    = round(a["cost"], 6)
        # Sort and cap per-agent tools to top 10
        a["tools"]   = sorted(
            [{"tool": k, "count": v} for k, v in a["tools"].items()],
            key=lambda x: -x["count"])[:10]
        # Round daily costs
        a["daily_cost"]  = {k: round(v, 6) for k, v in a["daily_cost"].items()}
        a["models_cost"] = {k: round(v, 6) for k, v in a["models_cost"].items()}
        agents_out.append(a)

        totals["sessions"]   += a["sessions"]
        totals["messages"]   += a["messages"]
        totals["input"]      += a["input"]
        totals["output"]     += a["output"]
        totals["cacheRead"]  += a["cacheRead"]
        totals["cacheWrite"] += a["cacheWrite"]
        totals["cost"]       += a["cost"]
        totals["tool_uses"]  += a["tool_uses"]

    totals["cost"] = round(totals["cost"], 6)

    # Top-N cross-agent tool attribution
    tool_attribution = sorted(
        [{"tool": k, "count": v} for k, v in tool_totals.items()],
        key=lambda x: -x["count"],
    )[:20]
    total_tool_calls = sum(t["count"] for t in tool_attribution) or 1
    for t in tool_attribution:
        t["pct"] = round(100.0 * t["count"] / total_tool_calls, 1)

    # Last 14 days of daily cost (chronological)
    sorted_days = sorted(daily_cost.keys())[-14:]
    daily_cost_14 = {d: round(daily_cost[d], 6) for d in sorted_days}

    # ---- new: per-agent trend + monthly projection -------------------------
    # _analyze_usage_trends expects integer token counts. We use daily_input +
    # daily_output (real tokens) when computing per-agent trend below.
    for ag in agents_out:
        daily_tok = {
            d: ag["daily_input"].get(d, 0) + ag["daily_output"].get(d, 0)
            for d in set(list(ag["daily_input"]) + list(ag["daily_output"]))
        }
        ag["trend"] = _analyze_usage_trends(daily_tok)
        ag["monthly_projection_usd"] = _project_monthly_cost(ag["daily_cost"])

    # ---- new: thinking flow summary ---------------------------------------
    # Rank agents by total thinking events; collect top sessions with thinking.
    thinking_agents = sorted(
        [{"name": ag["name"], "thinking_events": ag["thinking_events"],
          "thinking_chars": ag["thinking_chars"], "cost": ag["cost"]}
         for ag in agents_out if ag["thinking_events"] > 0],
        key=lambda x: x["thinking_events"], reverse=True,
    )[:10]
    thinking_sessions = sorted(
        [dict(s, agent=ag["name"])
         for ag in agents_out for s in ag["recent"]
         if s.get("thinking_events", 0) > 0],
        key=lambda x: x["thinking_events"], reverse=True,
    )[:10]

    # ---- new: token distribution breakdown --------------------------------
    total_in  = totals["input"]
    total_out = totals["output"]
    total_cr  = totals["cacheRead"]
    total_cw  = totals["cacheWrite"]
    total_all = total_in + total_out + total_cr + total_cw
    token_dist = {
        "input_pct":     round(total_in  / max(total_all, 1) * 100, 1),
        "output_pct":    round(total_out / max(total_all, 1) * 100, 1),
        "cache_r_pct":   round(total_cr  / max(total_all, 1) * 100, 1),
        "cache_w_pct":   round(total_cw  / max(total_all, 1) * 100, 1),
        "cache_hit_pct": round(total_cr  / max(total_in + total_cr, 1) * 100, 1),
        "total": total_all,
    }

    velocity    = _oc_velocity_from_agents(sessions_pre)
    anomalies   = _detect_cost_anomalies(agents_out)
    tool_trends = _compute_tool_trends(agents_out)

    flows_out    = merged_flows
    tasks_out    = merged_tasks
    delivery_out = merged_delivery

    payload = {
        "agents":           agents_out,
        "totals":           totals,
        "tool_attribution": tool_attribution,
        "daily_cost":       daily_cost_14,
        "trend":            _analyze_usage_trends(daily_tokens),
        "flows":            flows_out,
        "tasks":            tasks_out,
        "delivery":         delivery_out,
        "ts":               int(now),
        # --- new keys ---
        "thinking":         {"agents": thinking_agents, "sessions": thinking_sessions},
        "token_dist":       token_dist,
        "velocity":         velocity,
        "anomalies":        anomalies,
        "tool_trends":      tool_trends,
    }
    _openclaw_agg_cache.update({"ts": now, "payload": payload})
    return payload


# ── Route registration ───────────────────────────────────────────────

def register_routes(app, ctx) -> None:
    """Wire the single /api/openclaw/analytics route into ``app``. Fans
    out to every approved openclaw-capable agent. No other cross-module
    deps."""
    _deps.ctx = ctx

    @app.route("/api/openclaw/analytics")
    def openclaw_analytics():
        return jsonify(_collect_openclaw_analytics())
