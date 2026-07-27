"""GPU Report Card (#468): standardized cross-provider bench, storage, routes."""
from __future__ import annotations

import json
import statistics

# ── Reference preset ─────────────────────────────────────────────────
# Frozen once merged; changes ship as preset_v2 so the leaderboard can
# partition by preset version.

PRESET_VERSION = "preset_v1"
GEN_TOKENS = 128
REPS = 3

# Non-gated official Qwen repos; 4-bit class on every provider. The 7B GGUF
# is sharded — the first shard is pinned and llama.cpp pulls the rest.
REFERENCE_MODELS = [
    {"key": "small", "label": "Qwen2.5-1.5B-Instruct (4-bit)", "sources": {
        "llama": {"repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
                  "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
                  "revision": "91cad51170dc346986eccefdc2dd33a9da36ead9"},
        "lms":   {"repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
                  "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
                  "revision": "91cad51170dc346986eccefdc2dd33a9da36ead9"},
        "vllm":  {"repo": "Qwen/Qwen2.5-1.5B-Instruct-AWQ",
                  "file": "",
                  "revision": "3ecffa0ceb27851800f45519bab9c457a04405e1"}}},
    {"key": "mid", "label": "Qwen2.5-7B-Instruct (4-bit)", "sources": {
        "llama": {"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                  "file": "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
                  "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1f"},
        "lms":   {"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                  "file": "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
                  "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1f"},
        "vllm":  {"repo": "Qwen/Qwen2.5-7B-Instruct-AWQ",
                  "file": "",
                  "revision": "b25037543e9394b818fdfca67ab2a00ecc7dd641"}}},
]


def preset_source(model_key: str, provider: str) -> "dict | None":
    for m in REFERENCE_MODELS:
        if m["key"] == model_key:
            return (m["sources"] or {}).get(provider)
    return None


# Fixed ~512-token prompt, versioned with the preset. Never edit in place —
# a changed corpus makes stored cards incomparable; add preset_v2 instead.
PROMPT_CORPUS = (
    "You are a meticulous archivist describing the history of computing. "
    "Write a detailed, factual account of the development of operating "
    "systems from the 1960s to the present day. Begin with mainframe batch "
    "processing, where operators queued punched-card decks and a resident "
    "monitor handed the processor from one job to the next without human "
    "intervention. Explain how the economics of expensive hardware and cheap "
    "waiting made better utilization the central design goal of the era. "
    "Describe the arrival of time-sharing, the Compatible Time-Sharing System "
    "at MIT, and the ambitious Multics project that followed it, noting which "
    "of its ideas survived and which proved too costly to build. Cover the "
    "birth of UNIX at Bell Labs, the decision to rewrite it in C, and the way "
    "portable source code let a single system spread across incompatible "
    "machines. Trace the divergence into Berkeley and System V lineages, the "
    "standardization effort that produced POSIX, and the licensing disputes "
    "that shaped which implementations universities and vendors could adopt. "
    "Turn next to the personal computer, the small single-user monitors that "
    "preceded it, and the gradual reintroduction of protected memory, "
    "preemptive scheduling, and virtual memory into desktop systems that had "
    "shipped without them. Discuss the microkernel argument, what its "
    "advocates promised, what the measured costs turned out to be, and how "
    "hybrid designs settled the question in practice. Describe the rise of "
    "free and open source kernels, the collaborative development model that "
    "sustained them, and their eventual dominance in server and embedded "
    "deployments. Explain virtualization, from early mainframe hypervisors "
    "through hardware-assisted extensions, and how it changed capacity "
    "planning. Then treat security: privilege separation, mandatory access "
    "control, sandboxing, and the mitigations added in response to "
    "speculative execution attacks. Throughout, "
    "identify the recurring tension between abstraction and control, and "
    "conclude with contemporary containerized and cloud-native designs."
)

# ── Timing / energy / aggregation math ───────────────────────────────


def rep_metrics(rep: dict) -> dict:
    """Add prefill_tps + gen_tps to one repetition's raw timings."""
    out = dict(rep)
    out["prefill_tps"] = (rep["prompt_tokens"] / rep["ttft_s"]
                          if rep.get("ttft_s") else 0.0)
    out["gen_tps"] = (rep["gen_tokens"] / rep["gen_duration_s"]
                      if rep.get("gen_duration_s") else 0.0)
    return out


def run_metrics(reps: "list[dict]") -> dict:
    """Median TTFT / prefill / generation throughput across repetitions."""
    ms = [rep_metrics(r) for r in reps]
    if not ms:
        return {"ttft_s": 0.0, "prefill_tps": 0.0, "gen_tps": 0.0}
    return {k: statistics.median(m[k] for m in ms)
            for k in ("ttft_s", "prefill_tps", "gen_tps")}


def energy_metrics(avg_watts, gen_tps: float, price_kwh: float) -> dict:
    """Tokens/joule + $/Mtok from generation throughput; None without power."""
    if not avg_watts or avg_watts <= 0 or not gen_tps or gen_tps <= 0:
        return {"tokens_per_joule": None, "usd_per_mtok": None,
                "avg_watts": avg_watts}
    return {"tokens_per_joule": gen_tps / avg_watts,
            "usd_per_mtok": (avg_watts / 1000.0 * price_kwh)
                            / (gen_tps * 3600.0) * 1e6,
            "avg_watts": avg_watts}


def aggregate_gpus(gpus: "list[dict]") -> dict:
    """Roll per-GPU entries into one card row: summed VRAM/watts, one label."""
    if not gpus:
        return {"gpu_config": None, "vram_total_mb": None,
                "vram_used_mb": None, "power_w": None}
    names = [g.get("name") or "?" for g in gpus]
    uniq = set(names)
    if len(uniq) == 1:
        label = f"{len(gpus)}× {names[0]}" if len(gpus) > 1 else names[0]
    else:
        label = " + ".join(sorted(uniq))
    powers = [g.get("power_w") for g in gpus if g.get("power_w") is not None]
    return {"gpu_config": label,
            "vram_total_mb": sum(g.get("vram_total_mb") or 0 for g in gpus),
            "vram_used_mb": sum(g.get("vram_used_mb") or 0 for g in gpus),
            "power_w": sum(powers) if powers else None}


# ── Storage ──────────────────────────────────────────────────────────
# One row per completed run; all runs retained so the table backs trending.

_COLS = "ts, agent_id, provider, mode, preset_version, eligible, result"


def init_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            mode TEXT NOT NULL,
            preset_version TEXT NOT NULL,
            eligible INTEGER NOT NULL,
            result TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_cards_lookup "
                 "ON report_cards(agent_id, provider, ts)")
    conn.commit()


def insert_card(conn, card: dict) -> int:
    cur = conn.execute(
        "INSERT INTO report_cards (ts, agent_id, provider, mode, preset_version,"
        " eligible, result) VALUES (?,?,?,?,?,?,?)",
        (int(card["ts"]), card["agent_id"], card["provider"], card["mode"],
         card["preset_version"], 1 if card["eligible"] else 0,
         json.dumps(card["result"])))
    conn.commit()
    return cur.lastrowid


def _row_to_card(row) -> dict:
    return {"ts": row[0], "agent_id": row[1], "provider": row[2], "mode": row[3],
            "preset_version": row[4], "eligible": bool(row[5]),
            "result": json.loads(row[6])}


def latest_card(conn, agent_id: str, provider: str) -> "dict | None":
    row = conn.execute(
        f"SELECT {_COLS} FROM report_cards WHERE agent_id=? AND provider=?"
        " ORDER BY ts DESC, id DESC LIMIT 1", (agent_id, provider)).fetchone()
    return _row_to_card(row) if row else None


def history(conn, agent_id: str, provider: str, model: str) -> "list[dict]":
    rows = conn.execute(
        f"SELECT {_COLS} FROM report_cards WHERE agent_id=? AND provider=?"
        " ORDER BY ts ASC, id ASC", (agent_id, provider)).fetchall()
    return [c for c in map(_row_to_card, rows)
            if c["result"].get("model") == model]
