#!/usr/bin/env python3
"""
gen_screenshot_gallery.py — regenerate the README screenshot viewer (#558).

Writes docs/gallery/NN-slug.md, one screenshot per page, each linking to the
previous and next page so the reader steps through them with the arrows.
Also writes docs/gallery/README.md as the index. Edit SHOTS/SHORT and re-run;
the README's own frame (slide 1) is maintained by hand.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "gallery"

# (slug, file, width, height, title, caption)
SHOTS = [
    ("login", "login.webp", 2560, 1440, "Sign-in screen",
     "Session sign-in. Named accounts with Admin / Operator roles, plus username + source-IP lockout after repeated failures."),
    ("dashboard-llama", "dashboard-llama.webp", 2560, 1440, "Llama dashboard",
     "Live metrics from the llama.cpp server and its host."),
    ("dashboard-lmstudio", "dashboard-lmstudio.webp", 2560, 1440, "LM Studio dashboard",
     "The server card, loaded models, and host metrics, plus live Apple-silicon powermetrics (SoC / CPU / GPU / ANE watts, thermal pressure, GPU busy). Token counts are measured at the manager gateway."),
    ("model-control", "model-control.webp", 2560, 1440, "Model control",
     "Start/stop inference servers, change models, control the provider, manage the model library, run benchmarks, auto tune models."),
    ("model-control-detail", "model-control-2.webp", 2668, 1265, "Model control — detail",
     "Per-model configuration and provider controls."),
    ("model-control-cards", "model-control-cards.webp", 2767, 1049, "Model control — cards",
     "The model library as cards, with named config profiles (chat / code / general) that swap and reload in one click."),
    ("autopilot", "autopilot.webp", 2560, 1440, "Routing & Model Autopilot",
     "Per-provider pool order and model pins, and the Autopilot editor: one row per model with its placement, failover mode, replica range, and size, each showing whether it is currently placed. Pending proposals are listed below for approval."),
    ("dashboard-manager", "dashboard-manager.webp", 2560, 1440, "Manager dashboard",
     "Overall manager and agent health."),
    ("alarm-console", "alarm-console.webp", 2560, 1440, "Alarm engine",
     "Live alerts, anomalies, trend graphs, rule and notification editor, alert timeline."),
    ("admin-console", "admin-console.webp", 2560, 1440, "Admin console — agents",
     "System health plus sub-tabs for access control, agents, the audit log, backup/restore, and routing. The agents view lists every registered host with its capabilities, pool membership, TLS state, and version."),
    ("dashboard-energy", "dashboard-energy.webp", 2560, 1440, "Energy & cost dashboard",
     "Measured $/Mtok against your electricity price, savings versus hosted-API pricing, and hourly active-vs-idle energy. The per-host table marks which hosts report power and token telemetry, so the totals say what they're based on."),
    ("dashboard-openclaw", "dashboard-openclaw.webp", 2560, 1440, "OpenClaw dashboard",
     "OpenClaw session metrics, cost analytics, and tool attribution."),
    ("autotune", "autotune.webp", 1170, 1057, "Autotune wizard",
     "Search for the fastest context/slot settings per model."),
    ("benchmark", "benchmark.webp", 1164, 1161, "Benchmark results",
     "Throughput benchmarks across your whole model library."),
]

SHORT = [
    "roles, lockout, and session sign-in.",
    "live metrics from the `llama.cpp` server and its host.",
    "loaded models, host metrics, and Apple-silicon powermetrics.",
    "run the servers, swap models, and manage the library.",
    "per-model configuration and provider controls.",
    "the library as cards, with named config profiles.",
    "pool order, model pins, and the Autopilot editor.",
    "manager and agent health at a glance.",
    "alerts, anomalies, trend graphs, and the rule editor.",
    "system health, access control, agents, and the audit log.",
    "measured $/Mtok, savings, and active-vs-idle energy.",
    "OpenClaw session cost analytics and tool attribution.",
    "search for the fastest context/slot settings per model.",
    "throughput benchmarks across your whole model library.",
]

N = len(SHOTS)


def page_name(i: int) -> str:
    return f"{i + 1:02d}-{SHOTS[i][0]}.md"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for i, (slug, fname, w, h, title, caption) in enumerate(SHOTS):
        prev_p = page_name((i - 1) % N)
        next_p = page_name((i + 1) % N)
        body = f"""# {title}

<img width="{w}" height="{h}" alt="{title}" src="../screenshots/{fname}" />

{caption}

**{i + 1} / {N}** &nbsp;&nbsp; [**&lsaquo; Prev**]({prev_p}) &nbsp;&middot;&nbsp; [**Index**](README.md) &nbsp;&middot;&nbsp; [**Next &rsaquo;**]({next_p})

[&larr; Back to the project README](../../README.md)
"""
        (OUT / page_name(i)).write_text(body, encoding="utf-8")

    rows = "\n".join(
        f"{i + 1}. [{t}]({page_name(i)}) — {SHORT[i]}"
        for i, (s, f, w, h, t, c) in enumerate(SHOTS)
    )
    index = f"""# Screenshot gallery

Every screen in the LLM Systems Manager, one per page. Open the first one and
use **Next &rsaquo;** to step through all {N}, or jump straight to any of them.

[**Start the tour &rsaquo;**]({page_name(0)})

{rows}

[&larr; Back to the project README](../../README.md)
"""
    (OUT / "README.md").write_text(index, encoding="utf-8")
    print(f"wrote {N} pages + index to {OUT}")


if __name__ == "__main__":
    main()
