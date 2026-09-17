"""Tower conversation eval (#1047): canned questions through the real Tower loop, scored per model,
plus the curated model list and the "Get a Tower model" job."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Optional

import jobs
import tower
import tower_tools

log = logging.getLogger("llm-systems-manager.tower")

KIND_EVAL = "tower_eval"
KIND_GET = "tower_get_model"
EVAL_KEEP = 20
EVAL_MAX_RUN_S = 1800.0
GET_MAX_RUN_S = 4 * 3600.0
LOAD_WAIT_S = 600.0
NEW_MODEL_INI = {"ctx-size": "32768", "n-gpu-layers": "99", "flash-attn": "on", "jinja": "on"}
CURATED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tower_models.json")

_QUANT = re.compile(r"(?<![A-Za-z0-9])((?:IQ|Q)\d(?:_[A-Za-z0-9]+)+|Q\d|F16|BF16|F32|FP16|FP8)(?![A-Za-z0-9])", re.I)
_PCT = re.compile(r"(\d{1,3})%")
_NOTE_CORRECTED = "prose tool call corrected"
_NOTE_RETRY = "retried after"


# ── cases ───────────────────────────────────────────────────────────

def quant_of(model_id: str) -> Optional[str]:
    """The quant token in a model id (Q4_K_M, IQ3_XS, F16…), else None."""
    m = _QUANT.search(str(model_id or ""))
    return m.group(1).upper() if m else None


def ambiguous_token(hosts: "list[str]") -> Optional[str]:
    """A hostname prefix shared by at least two hosts and equal to none, cut at a separator; None without one."""
    names = [str(h) for h in hosts if h]
    best = ""
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            n = 0
            while n < min(len(a), len(b)) and a[n].lower() == b[n].lower():
                n += 1
            cand = a[:n].rstrip("-_. ")
            if len(cand) > len(best):
                best = cand
    if len(best) < 3 or any(best.lower() == n.lower() for n in names):
        return None
    hits = [n for n in names if tower_tools._host_key(best) in tower_tools._host_key(n)]
    return best if len(hits) >= 2 else None


def build_cases(hosts: "list[str]", aliases: dict, ambiguous: Optional[str] = None) -> "list[dict]":
    """The canned conversations for one eval, worded for the fleet at hand."""
    host = hosts[0] if hosts else None
    alias = next((k for k in ("primary llama", "primary") if aliases.get(k)), None)
    cases = [
        {"id": "hosts", "title": "Plain read", "prompt": "Which hosts are online right now?",
         "tools": ["hosts_overview"], "answer": "text", "max_calls": 2},
        {"id": "models", "title": "Loaded models", "prompt": "Which models are loaded right now, and on which hosts?",
         "tools": ["models", "hosts_overview"], "answer": "text", "max_calls": 3},
        {"id": "alarms", "title": "Open alerts", "prompt": "Are there any open alerts right now? List them briefly.",
         "tools": ["alarms"], "answer": "text", "max_calls": 2},
        {"id": "energy", "title": "Energy read", "prompt": "How much energy did the fleet use today, and what did it cost?",
         "tools": ["energy_summary"], "answer": "text", "max_calls": 2},
    ]
    if host:
        cases += [
            {"id": "host", "title": "Host resolution",
             "prompt": f"How much RAM and GPU memory is {alias or host} using right now?",
             "tools": ["host_detail", "hosts_overview"], "answer": "text", "max_calls": 3, "arg": "host"},
            {"id": "rank", "title": "Two-step read",
             "prompt": "Which online host has the highest GPU temperature, and which model is loaded on it?",
             "tools": ["hosts_overview", "host_detail", "models"], "answer": "text", "max_calls": 4},
            {"id": "timer", "title": "Timer",
             "prompt": f"Check the GPU temperature on {host} every 5 minutes for the next 15 minutes and tell me what you see.",
             "tools": ["schedule"], "answer": "timer", "max_calls": 3},
            {"id": "proposal", "title": "Action proposal", "prompt": f"Wake the llama server on {host}.",
             "tools": ["wake_server"], "answer": "proposal", "max_calls": 3},
        ]
    if ambiguous:
        cases.append({"id": "question", "title": "Question card",
                      "prompt": f"What is the GPU temperature on {ambiguous} right now?",
                      "tools": ["host_detail", "hosts_overview"], "answer": "question", "max_calls": 4})
    return cases


# ── one case through the real loop ──────────────────────────────────

class EvalView:
    """The live Tower settings pinned to one model: no fallback, act tools present (every card is auto-denied),
    no violation reports, question cards and timers on."""
    fallback = False
    capabilities = "operate"
    report_violations = False
    questions = True
    timers = True

    def __init__(self, cfg, model_id: str):
        self._cfg, self.model = cfg, model_id

    def __getattr__(self, name):
        return getattr(self._cfg, name)


class AutoApprovals(tower.Approvals):
    """Answers every question card with its first choice and denies every action card at once."""

    def __init__(self, cards: dict):
        super().__init__()
        self._cards = cards

    def wait(self, action_id: str, deadline: float, cancelled: Callable[[], bool], tick: float = 0.25) -> Optional[dict]:
        ev = self._cards.get(action_id) or {}
        if ev.get("event") == "question":
            answers = [(q.get("choices") or ["yes"])[0] for q in (ev.get("questions") or [{}])]
            return {"status": "answered", "actor": "eval", "options": {"answers": answers or ["yes"]}}
        return {"status": "denied", "actor": "eval"}


class EvalTimers:
    """Records schedule calls instead of queueing them."""

    def __init__(self):
        self.calls: "list[dict]" = []

    def schedule(self, *, thread_id: str, user: str, role: str, args: dict) -> dict:
        self.calls.append(dict(args))
        return {"ok": True, "message": "timer recorded by the eval; nothing was scheduled", "job_id": "eval"}


def score_case(case: dict, events: "list[dict]", out: Optional[dict], ms: int) -> dict:
    """Grades one turn's events against the case: what was called, what came back, how it ended."""
    tools = [e for e in events if e.get("event") == "tool"]
    questions = [e for e in events if e.get("event") == "question"]
    confirms = [e for e in events if e.get("event") == "confirm"]
    done = next((e for e in events if e.get("event") == "done"), None)
    error = next((e for e in events if e.get("event") == "error"), None)
    text = "".join(str(e.get("text") or "") for e in events if e.get("event") == "delta").strip()
    note = str((done or {}).get("note") or "")
    calls = int((out or {}).get("calls") or 0) or len(tools)
    kind = case["answer"]
    if kind == "proposal":
        hit = [c for c in confirms if c.get("tool") in case["tools"]]
        ok_hit = bool(hit)
    else:
        hit = [e for e in tools if e.get("name") in case["tools"]]
        ok_hit = any(e.get("ok") for e in hit)
    answered = bool(done) and error is None and bool(text) and not tower._canned(text, False)
    reasons: "list[str]" = []
    if error is not None:
        reasons.append(f"error: {error.get('message') or 'unknown'}")
    if case["tools"] and not hit:
        called = list(dict.fromkeys([str(e.get("name")) for e in tools] + [str(c.get("tool")) for c in confirms]))
        reasons.append("no tool call" if not called else f"called {', '.join(called)} instead of {' / '.join(case['tools'])}")
    elif hit and not ok_hit:
        res = hit[0].get("result")
        why = res.get("error") or res.get("message") if isinstance(res, dict) else None
        reasons.append(f"{hit[0].get('name')} failed: {str(why or 'no result')[:80]}")
    primary = [e for e in hit if e.get("name") == case["tools"][0]] if case["tools"] else []
    if case.get("arg") and primary and not all(str((e.get("args") or {}).get(case["arg"]) or "").strip() for e in primary):
        reasons.append(f"{case['arg']} argument missing")
    if calls > int(case.get("max_calls") or 99):
        reasons.append(f"{calls} tool calls (at most {case['max_calls']})")
    if kind == "question":
        if not questions:
            reasons.append("no question card")
    elif kind != "proposal":
        if questions:
            reasons.append("asked a question instead")
        if confirms:
            reasons.append("proposed an action instead")
    if kind == "proposal":
        if done is None or error is not None:
            reasons.append("no answer after the denial")
    elif not answered:
        reasons.append("no answer" if not text else "canned answer")
    return {"id": case["id"], "title": case["title"], "prompt": case["prompt"], "passed": not reasons,
            "calls": calls, "corrections": note.count(_NOTE_CORRECTED), "retries": note.count(_NOTE_RETRY),
            "ms": int(ms), "tools": [e.get("name") for e in tools], "detail": "; ".join(reasons) or "ok",
            "answer": text[:400]}


def run_case(case: dict, *, cfg, registry: dict, complete_stream: Callable, model: dict, cancelled: Callable[[], bool],
             server_args_of=None, checks=None, actor: str = "eval") -> dict:
    """One case as one Tower turn on a scratch store; the scored result."""
    store = tower.Store(":memory:")
    tid = store.create_thread("eval", case["title"], None)
    events: "list[dict]" = []
    cards: dict = {}

    def emit(ev: dict) -> None:
        events.append(ev)
        if ev.get("event") in ("question", "confirm") and ev.get("action_id"):
            cards[ev["action_id"]] = ev

    t0 = time.monotonic()
    out = tower.run_turn(thread_id=tid, user_text=case["prompt"], page=None, cfg=cfg, role="admin", registry=registry,
                         complete_stream=complete_stream, store=store, emit=emit, model=model, cancelled=cancelled,
                         server_args_of=server_args_of, approvals=AutoApprovals(cards), run_id=f"eval-{case['id']}",
                         actor=actor, timers=EvalTimers(), checks=checks, user="eval")
    return score_case(case, events, out, int((time.monotonic() - t0) * 1000))


def summarize(model: dict, cases: "list[dict]", *, quant: Optional[str], server: Optional[str], tool_mode: str,
              grade: Optional[str], ms: int, actor: str, at: float) -> dict:
    total = len(cases)
    passed = sum(1 for c in cases if c.get("passed"))
    calls = sum(int(c.get("calls") or 0) for c in cases)
    return {"id": uuid.uuid4().hex[:16], "model": model["model"], "provider": model.get("provider") or "llama",
            "hosts": list(model.get("hosts") or []), "quant": quant, "server": server, "tool_mode": tool_mode,
            "grade": grade, "at": float(at), "ms": int(ms), "passed": passed, "total": total, "calls": calls,
            "calls_per_case": round(calls / total, 2) if total else 0.0,
            "corrections": sum(int(c.get("corrections") or 0) for c in cases),
            "retries": sum(int(c.get("retries") or 0) for c in cases),
            "score_pct": round(100.0 * passed / total, 1) if total else 0.0, "cases": cases, "actor": actor}


# ── store ───────────────────────────────────────────────────────────

_COLS = ("id", "model", "provider", "quant", "server", "tool_mode", "grade", "at", "ms", "passed", "total", "calls",
         "corrections", "retries", "score_pct", "actor", "hosts", "cases")
_JSON = ("hosts", "cases")


class EvalStore:
    """tower_evals in the manager SQLite (per-thread conn; ':memory:' for tests)."""

    def __init__(self, db_path: str):
        self._path = db_path
        self._tls = threading.local()
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if db_path == ":memory:" else None
        if self._mem is not None:
            self._mem.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.init_tables()

    def _conn(self):
        if self._mem is not None:
            return self._mem
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._tls.conn = conn
        return conn

    def init_tables(self) -> None:
        c = self._conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tower_evals (
            id TEXT PRIMARY KEY, model TEXT NOT NULL, provider TEXT, quant TEXT, server TEXT, tool_mode TEXT, grade TEXT,
            at REAL NOT NULL, ms INTEGER, passed INTEGER, total INTEGER, calls INTEGER, corrections INTEGER, retries INTEGER,
            score_pct REAL, actor TEXT, hosts TEXT, cases TEXT);
        CREATE INDEX IF NOT EXISTS idx_tower_evals_model ON tower_evals(model, at);
        """)
        c.commit()

    @staticmethod
    def _row(r) -> dict:
        d = {k: r[k] for k in _COLS}
        for k in _JSON:
            try:
                d[k] = json.loads(d[k]) if d[k] else []
            except ValueError:
                d[k] = []
        d["passed"], d["total"] = int(d["passed"] or 0), int(d["total"] or 0)
        return d

    def save(self, result: dict) -> dict:
        row = {k: result.get(k) for k in _COLS}
        for k in _JSON:
            row[k] = json.dumps(row[k] or [], default=str)
        with self._lock:
            c = self._conn()
            c.execute(f"INSERT INTO tower_evals ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
                      tuple(row[k] for k in _COLS))
            c.execute("DELETE FROM tower_evals WHERE model = ? AND id NOT IN "
                      "(SELECT id FROM tower_evals WHERE model = ? ORDER BY at DESC LIMIT ?)",
                      (result["model"], result["model"], EVAL_KEEP))
            c.commit()
        return result

    def get(self, eval_id: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM tower_evals WHERE id = ?", (eval_id,)).fetchone()
        return self._row(r) if r else None

    def latest(self) -> "list[dict]":
        """The newest result per model, newest first."""
        rows = self._conn().execute(
            "SELECT * FROM tower_evals WHERE id IN (SELECT id FROM tower_evals t WHERE at = "
            "(SELECT MAX(at) FROM tower_evals WHERE model = t.model)) ORDER BY at DESC").fetchall()
        return [self._row(r) for r in rows]

    def latest_for(self, model: str) -> Optional[dict]:
        r = self._conn().execute("SELECT * FROM tower_evals WHERE model = ? ORDER BY at DESC LIMIT 1", (model,)).fetchone()
        return self._row(r) if r else None

    def history(self, model: str, limit: int = EVAL_KEEP) -> "list[dict]":
        rows = self._conn().execute("SELECT * FROM tower_evals WHERE model = ? ORDER BY at DESC LIMIT ?",
                                    (model, int(limit))).fetchall()
        return [self._row(r) for r in rows]


def brief(row: Optional[dict]) -> Optional[dict]:
    """The result without its per-case answers, for lists and the settings row."""
    if not row:
        return None
    out = {k: row.get(k) for k in _COLS if k != "cases"}
    out["cases"] = [{k: c.get(k) for k in ("id", "title", "passed", "calls", "corrections", "retries", "ms", "detail")}
                    for c in (row.get("cases") or [])]
    return out


# ── curated list ────────────────────────────────────────────────────

def load_curated(path: str = CURATED_PATH) -> "list[dict]":
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for m in data.get("models") or []:
        if not all(m.get(k) for k in ("key", "name", "repo", "file", "quant", "tier_gb")):
            continue
        out.append({**m, "model_id": f"{m['repo']}:{m['quant']}"})
    return out


# ── the eval + get-model jobs ───────────────────────────────────────

class Evaluator:
    """Runs evals as `tower_eval` jobs, keeps results in `EvalStore`, and drives the `tower_get_model` job."""

    def __init__(self, *, service, store: EvalStore, cfg: Callable[[], Any], registry_factory: Callable[[], dict],
                 complete_stream: Callable, entries: Callable[[], list], server_args_of=None, server_of=None,
                 checks=None, record_run: Optional[Callable[[dict], None]] = None,
                 agent_request=None, primary_agent: Optional[Callable[[], Optional[dict]]] = None,
                 refresh_index: Optional[Callable[[float], None]] = None, curated: Optional[Callable[[], list]] = None,
                 now: Callable[[], float] = time.time):
        self._svc, self.store = service, store
        self._cfg, self._registry_factory, self._cs, self._entries = cfg, registry_factory, complete_stream, entries
        self._server_args_of, self._server_of, self._checks, self._record_run = server_args_of, server_of, checks, record_run
        self._agent_request, self._primary_agent, self._refresh_index = agent_request, primary_agent, refresh_index
        self._curated = curated or load_curated
        self._now = now
        if service is not None:
            service.register(jobs.Kind(KIND_EVAL, "Tower eval", run=self._run_eval_job, api=True, resume="fail",
                                       max_run_s=EVAL_MAX_RUN_S, exclusive=lambda spec: [KIND_EVAL],
                                       label=lambda spec: f"Tower eval · {spec.get('model') or 'model'}"[:80]))
            service.register(jobs.Kind(KIND_GET, "Get a Tower model", run=self._run_get_job, api=True, resume="fail",
                                       max_run_s=GET_MAX_RUN_S, exclusive=lambda spec: [KIND_EVAL, KIND_GET],
                                       label=lambda spec: f"Get Tower model · {spec.get('name') or spec.get('key') or ''}"[:80]))

    # ── model lookup ──
    def resident(self, model_id: Optional[str]) -> Optional[dict]:
        """The pick for `model_id` when it is resident (or the resolved model when blank), else None."""
        entries = self._entries() or []
        if not model_id:
            return tower.resolve_model(self._cfg(), entries)
        e = next((x for x in entries if x.get("id") == model_id and tower._resident(x)), None)
        return tower._pick(e) if e else None

    def live(self, kind: Optional[str] = None) -> Optional[dict]:
        if self._svc is None:
            return None
        for k in ([kind] if kind else (KIND_EVAL, KIND_GET)):
            rows = self._svc.list("live", kind=k, limit=5)
            if rows:
                return rows[0]
        return None

    def view(self, row: Optional[dict]) -> Optional[dict]:
        return self._svc.view(row, role="admin", user="", detail=True) if (row and self._svc is not None) else None

    # ── eval ──
    def start(self, model_id: Optional[str], actor: str) -> "tuple[Optional[dict], Optional[str]]":
        if self.live() is not None:
            return None, "busy"
        m = self.resident(model_id)
        if m is None:
            return None, "no_model"
        row = self._svc.submit(KIND_EVAL, {"model": m["model"], "actor": actor}, user=actor, role="admin", source="api")
        return row, None

    def _run_eval_job(self, job) -> jobs.Outcome:
        spec = job.spec or {}
        m = self.resident(spec.get("model"))
        if m is None:
            return jobs.fail(f"{spec.get('model') or 'the model'} is not loaded", alert=False)

        def progress(state: dict) -> None:
            self._svc.set_state(job.id, state)

        result = self.run_eval(m, cancelled=job.cancelled, progress=progress, actor=str(spec.get("actor") or job.user or "eval"))
        if result is None:
            return jobs.fail("stopped", alert=False)
        return jobs.finish(result=brief(result), message=f"{result['passed']}/{result['total']} passed")

    def run_eval(self, model: dict, *, cancelled: Callable[[], bool] = lambda: False,
                 progress: Optional[Callable[[dict], None]] = None, actor: str = "eval") -> Optional[dict]:
        """Every case through the loop against `model`; the stored result, or None when cancelled."""
        cfg = EvalView(self._cfg(), model["model"])
        registry = self._registry_factory()
        hosts, aliases = tower_tools.fleet(registry)
        cases = build_cases(hosts, aliases, ambiguous_token(hosts))
        chk = None
        if self._checks is not None:
            try:
                chk = self._checks.run(model)
            except Exception as e:  # noqa: BLE001 — the eval still runs without a grade
                log.warning("tower eval check failed: %s: %s", type(e).__name__, e)
        checks = (lambda mid: chk) if chk else None
        t0 = time.monotonic()
        scored: "list[dict]" = []
        for i, case in enumerate(cases):
            if cancelled():
                return None
            if progress:
                progress({"phase": "eval", "case": i + 1, "total": len(cases), "id": case["id"], "title": case["title"],
                          "passed": sum(1 for c in scored if c["passed"])})
            try:
                scored.append(run_case(case, cfg=cfg, registry=registry, complete_stream=self._cs, model=model,
                                       cancelled=cancelled, server_args_of=self._server_args_of, checks=checks, actor=actor))
            except Exception as e:  # noqa: BLE001 — one broken case scores as failed
                log.warning("tower eval case %s failed: %s: %s", case["id"], type(e).__name__, e)
                scored.append({"id": case["id"], "title": case["title"], "prompt": case["prompt"], "passed": False,
                               "calls": 0, "corrections": 0, "retries": 0, "ms": 0, "tools": [],
                               "detail": f"error: {type(e).__name__}", "answer": ""})
        if cancelled():
            return None
        server = None
        if self._server_of is not None:
            try:
                server = self._server_of(model)
            except Exception:  # noqa: BLE001
                server = None
        result = summarize(model, scored, quant=quant_of(model["model"]), server=server,
                           tool_mode=str(getattr(cfg, "tool_mode", "auto") or "auto"),
                           grade=(chk or {}).get("grade"), ms=int((time.monotonic() - t0) * 1000), actor=actor, at=self._now())
        self.store.save(result)
        if self._record_run is not None:
            try:
                self._record_run(result)
            except Exception as e:  # noqa: BLE001 — the ledger row is a courtesy
                log.warning("tower eval ledger row failed: %s: %s", type(e).__name__, e)
        log.info("tower eval model=%s passed=%d/%d calls=%d corrections=%d retries=%d ms=%d", model["model"],
                 result["passed"], result["total"], result["calls"], result["corrections"], result["retries"], result["ms"])
        return result

    # ── curated models ──
    def curated(self) -> "list[dict]":
        try:
            return self._curated()
        except Exception as e:  # noqa: BLE001
            log.warning("tower curated list unreadable: %s: %s", type(e).__name__, e)
            return []

    def curated_view(self) -> dict:
        """The list with what the fleet already has: present in a catalog, resident, and the newest eval."""
        entries = self._entries() or []
        by_id = {e.get("id"): e for e in entries}
        agent = self._primary_agent() if self._primary_agent else None
        host = str((agent or {}).get("hostname") or "")
        out = []
        for m in self.curated():
            e = by_id.get(m["model_id"])
            out.append({**m, "present": e is not None, "loaded": bool(e and tower._resident(e)),
                        "eval": brief(self.store.latest_for(m["model_id"]))})
        last = self._svc.list("all", kind=KIND_GET, limit=1) if self._svc is not None else []
        return {"models": out, "host": host, "live": self.view(self.live(KIND_GET)), "last": self.view(last[0] if last else None)}

    def start_get(self, key: str, actor: str) -> "tuple[Optional[dict], Optional[str]]":
        m = next((x for x in self.curated() if x["key"] == key), None)
        if m is None:
            return None, "unknown model"
        if self.live() is not None:
            return None, "busy"
        agent = self._primary_agent() if self._primary_agent else None
        if not agent or not agent.get("token"):
            return None, "no primary llama host"
        spec = {"key": m["key"], "name": m["name"], "repo": m["repo"], "file": m["file"], "quant": m["quant"],
                "model_id": m["model_id"], "agent_id": agent.get("agent_id"), "host": agent.get("hostname"), "actor": actor}
        row = self._svc.submit(KIND_GET, spec, user=actor, role="admin", source="api")
        return row, None

    # ── the get-model job ──
    def _agent(self, spec: dict) -> Optional[dict]:
        import agent_registry
        agent = agent_registry.resolve_agent_by_id(str(spec.get("agent_id") or ""))
        return agent if agent and agent.get("token") else None

    def _call(self, agent: dict, method: str, path: str, **kw):
        req = self._agent_request
        if req is None:
            import agent_registry
            req = agent_registry.agent_request
        return req(method, agent, path, headers={"Authorization": f"Bearer {agent.get('token') or ''}"}, **kw)

    def _json(self, r) -> Optional[dict]:
        if r is None or not getattr(r, "ok", False):
            return None
        try:
            body = r.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _run_get_job(self, job) -> jobs.Outcome:
        spec = job.spec or {}
        agent = self._agent(spec)
        if agent is None:
            return jobs.fail("the primary llama host is no longer registered", alert=False)
        host, model_id = str(spec.get("host") or agent.get("hostname") or ""), str(spec.get("model_id") or "")

        def progress(**state) -> None:
            self._svc.set_state(job.id, {"host": host, "model_id": model_id, **state})

        # 1. download through the host's own download path
        progress(phase="download", pct=0)
        err = self._download(agent, spec, job.cancelled, progress)
        if err == "cancelled":
            return jobs.fail("stopped", alert=False)
        if err:
            return jobs.fail(f"download failed: {err}")
        # 2. a config.ini section for the new quant, then a server restart so the router sees it
        progress(phase="config")
        err = self._register(agent, model_id)
        if err:
            return jobs.fail(f"configuration failed: {err}")
        if job.cancelled():
            return jobs.fail("stopped", alert=False)
        # 3. load and wait until the gateway sees it resident
        progress(phase="load")
        r, _tried, rerr = self._call(agent, "POST", "/llama/load", json={"model": model_id}, timeout=180)
        body = self._json(r)
        if body is None or body.get("ok") is False:
            return jobs.fail(f"load failed: {(body or {}).get('error') or rerr or getattr(r, 'status_code', 'no response')}")
        m = self._wait_resident(model_id, job.cancelled, progress)
        if m is None:
            return jobs.fail("stopped" if job.cancelled() else f"{model_id} did not become resident within {int(LOAD_WAIT_S)} s",
                             alert=not job.cancelled())
        # 4. check + eval
        progress(phase="check")
        result = self.run_eval(m, cancelled=job.cancelled, progress=lambda st: progress(**st), actor=str(spec.get("actor") or job.user or "eval"))
        if result is None:
            return jobs.fail("stopped", alert=False)
        chk = self._checks.get(model_id) if self._checks is not None else None
        return jobs.finish(result={"model": model_id, "host": host, "check": chk, "eval": brief(result), "pin_offer": True},
                           message=f"{spec.get('name') or model_id} ready · {result['passed']}/{result['total']} passed")

    def _download(self, agent: dict, spec: dict, cancelled: Callable[[], bool], progress: Callable) -> Optional[str]:
        r, _tried, err = self._call(agent, "POST", "/llama/download",
                                    json={"repo": spec["repo"], "patterns": [spec["file"]]}, timeout=30)
        body = self._json(r)
        if body is None or body.get("ok") is False:
            return str((body or {}).get("error") or err or getattr(r, "status_code", "no response"))
        r, _tried, err = self._call(agent, "GET", "/llama/download/stream", stream=True, timeout=(5, 120))
        if r is None or not getattr(r, "ok", False):
            return f"progress stream unavailable: {err or getattr(r, 'status_code', 'no response')}"
        last_pct, last_line = 0, ""
        try:
            for raw in r.iter_lines():
                if cancelled():
                    self._call(agent, "POST", "/llama/download/cancel", timeout=15)
                    return "cancelled"
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                if not line.startswith("data:"):
                    continue
                try:
                    msg = json.loads(line[5:].strip() or "{}")
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "line":
                    last_line = str(msg.get("text") or "")[:160]
                    pm = _PCT.findall(last_line)
                    if pm:
                        last_pct = max(last_pct, min(100, int(pm[-1])))
                    progress(phase="download", pct=last_pct, line=last_line)
                elif kind == "done":
                    if msg.get("cancelled"):
                        return "cancelled"
                    return None if msg.get("ok") else str(msg.get("error") or f"hf exited {msg.get('rc')}")
        finally:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass
        return "progress stream ended early"

    def _register(self, agent: dict, model_id: str) -> Optional[str]:
        r, _tried, err = self._call(agent, "GET", "/llama/config", timeout=30)
        sections = self._json(r)
        if sections is None:
            return f"could not read config.ini: {err or getattr(r, 'status_code', 'no response')}"
        sections = {k: v for k, v in sections.items() if k != "__DEFAULTS__" and isinstance(v, dict)}
        if model_id in sections:
            return None
        sections[model_id] = dict(NEW_MODEL_INI)
        r, _tried, err = self._call(agent, "POST", "/llama/config", json=sections, timeout=30)
        body = self._json(r)
        if body is None or body.get("ok") is False:
            return str((body or {}).get("error") or err or getattr(r, "status_code", "no response"))
        r, _tried, err = self._call(agent, "POST", "/llama/server/restart", timeout=120)
        body = self._json(r)
        if body is None or body.get("ok") is False:
            return f"server restart failed: {(body or {}).get('error') or err or getattr(r, 'status_code', 'no response')}"
        return None

    def _wait_resident(self, model_id: str, cancelled: Callable[[], bool], progress: Callable) -> Optional[dict]:
        deadline = time.monotonic() + LOAD_WAIT_S
        while time.monotonic() < deadline:
            if cancelled():
                return None
            if self._refresh_index is not None:
                try:
                    self._refresh_index(5.0)
                except Exception:  # noqa: BLE001
                    pass
            m = self.resident(model_id)
            if m is not None:
                return m
            progress(phase="load", waited_s=int(LOAD_WAIT_S - (deadline - time.monotonic())))
            time.sleep(5.0)
        return None


# ── routes ──────────────────────────────────────────────────────────

def register_routes(app, ctx, *, evaluator: Evaluator, write_setting=None) -> None:
    from flask import Response, jsonify, request as flask_request, session
    import auth

    def _cfg():
        return getattr(ctx.settings.manager, "tower", None)

    def _gate():
        if not bool(getattr(_cfg(), "enabled", False)):
            return jsonify({"ok": False, "error": "tower disabled"}), 404
        if auth.effective_role() is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return None

    def _admin():
        deny = _gate()
        if deny:
            return deny
        return ctx.require_admin()

    def _actor() -> str:
        return tower.session_user(session)

    @app.route("/api/tower/eval", methods=["GET"])
    def tower_eval_list():
        deny = _gate()
        if deny:
            return deny
        model = str(flask_request.args.get("model") or "").strip()
        rows = evaluator.store.history(model) if model else evaluator.store.latest()
        current = tower.resolve_model(_cfg(), evaluator._entries() or []) or {}
        return jsonify({"ok": True, "results": [brief(r) for r in rows], "live": evaluator.view(evaluator.live()),
                        "model": current.get("model"), "admin": auth.effective_role() == "admin"})

    @app.route("/api/tower/eval", methods=["POST"])
    def tower_eval_start():
        deny = _admin()
        if deny is not None:
            return deny
        body = flask_request.get_json(silent=True) or {}
        model = tower._MODEL_NAME_SAFE.sub("", str(body.get("model") or ""))[:200]
        row, err = evaluator.start(model or None, _actor())
        if err == "busy":
            return jsonify({"ok": False, "error": "an eval or model download is already running"}), 409
        if err:
            return jsonify({"ok": False, "error": err}), 503
        return jsonify({"ok": True, "job": evaluator.view(row)})

    @app.route("/api/tower/eval/<eval_id>", methods=["GET"])
    def tower_eval_get(eval_id):
        deny = _gate()
        if deny:
            return deny
        row = evaluator.store.get(str(eval_id)[:32])
        if row is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        if str(flask_request.args.get("export") or "") in ("1", "true"):
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"tower-eval-{row['model']}-{int(row['at'])}.json")
            return Response(json.dumps(row, indent=2, default=str), mimetype="application/json",
                            headers={"Content-Disposition": f"attachment; filename={name}"})
        return jsonify({"ok": True, "result": row})

    @app.route("/api/tower/models", methods=["GET"])
    def tower_models():
        deny = _gate()
        if deny:
            return deny
        return jsonify({"ok": True, **evaluator.curated_view(), "admin": auth.effective_role() == "admin"})

    @app.route("/api/tower/models/get", methods=["POST"])
    def tower_models_get():
        deny = _admin()
        if deny is not None:
            return deny
        body = flask_request.get_json(silent=True) or {}
        row, err = evaluator.start_get(str(body.get("key") or "")[:64], _actor())
        if err == "busy":
            return jsonify({"ok": False, "error": "an eval or model download is already running"}), 409
        if err == "unknown model":
            return jsonify({"ok": False, "error": err}), 404
        if err:
            return jsonify({"ok": False, "error": err}), 503
        return jsonify({"ok": True, "job": evaluator.view(row)})
