"""Forecast Tower pass (#1031): Tower's part at the run's effort tier — a digest and a likely cause through
direct calls, an investigation through the conversation. Every measured summary, figure and next step stays the code's."""
from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Callable, Optional

import forecast_checks as fc
import forecast_effort as fe
import lms_native
import tower
from tower_watch import _ReadOnly, _asleep

log = logging.getLogger("llm-systems-manager.forecast")

ACTOR = tower.FORECAST_ACTOR
BUDGET_S = 90.0
MAX_CALLS = 8
TOL_DATE = 0.25
TOL_DATE_MIN_S = 86400.0
TOL_RATE = 0.25
MODEL_MAX = 20
TEXT_MAX = 600
SUBJECT_MAX = 60
PARSE_MAX_CHARS = 200_000
PARSE_MAX_DEPTH = 32
DIFFERED = "Tower's estimate differed and was not used."
DISCARDED = "discarded"
_ROW_KEYS = ("check", "host", "subject", "severity", "summary", "detail", "since", "predicted_at", "rate", "unit",
             "confidence", "suggested_action", "graph")
_TEXT_KEYS = ("summary", "detail", "suggested_action")
_SEV = {s: i for i, s in enumerate(fc.SEVERITIES)}
CAUSE = "Likely cause:"
RESTATE_RATIO = 0.6
# Words that carry no diagnosis; a cause needs CAUSE_WORDS_MIN words beyond them.
FILLER = frozenset(("trend", "trends", "pattern", "patterns", "issue", "issues", "problem", "problems", "accumulation",
                    "increase", "increasing", "increased", "decrease", "decreasing", "declining", "decline", "rising",
                    "growth", "growing", "measured", "code", "observed", "utilization", "usage", "level", "levels",
                    "change", "changes", "likely", "cause", "caused", "causing", "high", "frequency", "indicating",
                    "instability", "unstable"))
CAUSE_WORDS_MIN = 3
STEM = 4
WORD_MIN = 4
SPEED_MIN_S = 0.05
SPEED_MIN_TOKENS = 20
SPEED_MIN, SPEED_MAX = 0.1, 500.0
DIRECT_READ_MAX = 30
REASONING_HEADROOM = 1024
_KWARGS_PROVIDERS = ("llama", "vllm")
_MODEL_EVENTS = ("delta", "tool")
_SHAPE = ('{"host": "...", "subject": "...", "cause": "...", "since": "YYYY-MM-DD", '
          '"predicted_at": "YYYY-MM-DD" or null, "rate": number or null, "unit": "..."}')
_JSON_FENCE = re.compile(r"```json\s*\n(.*?)\n\s*```", re.S)
_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_PUNCT = " \t\n\r.,;:!?()[]{}<>\"'`“”‘’/\\|*_-"
_QUOTED_MAX = 20
CAUSE_MIN = 20
_QUOTED = re.compile(r"[\"“”‘’']([^\"“”‘’']{1,120})[\"“”‘’']")
_NUM_WORDS = re.compile(
    r"\b(zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|"
    r"billion|half|quarter|double|twice|triple|dozen|tens|hundreds|thousands|millions|billions|dozens)\b", re.I)
_QTY_WORDS = re.compile(r"\b(?:one|a couple of)\s+(?:second|minute|hour|day|week|month|year|percent|time|gb|mb|tb)s?\b",
                        re.I)
DIRECT_SYSTEM = (
    "You are the monitoring assistant of an LLM serving manager. You are given findings that code computed from "
    "stored history. Be concrete and brief. Never write numbers, dates or percentages — the figures are already "
    "shown to the operator. Answer with JSON only."
)
_FINDINGS = "Findings: "
DIGEST_TAIL = (". In two to four sentences tell the operator which of these are probably related, the most likely "
               "common factor, and what to look at first. Refer to findings by what they are, never by a number "
               "or id. Answer as {\"digest\": \"...\"}.")
CAUSE_TAIL = (". For each one give a short likely cause that adds something the summary does not already say. "
              "If you cannot name a concrete, specific cause, leave \"cause\" empty. "
              "Answer as {\"findings\": [{\"id\": \"...\", \"cause\": \"...\"}]}.")


class _Capped(_ReadOnly):
    """Read-only cfg for a Forecast call: the profile's tool-call cap, Thinking level and time budget,
    never the forecast tool."""

    def __init__(self, cfg, *, max_calls: int = MAX_CALLS, budget_s: float = BUDGET_S, thinking: str = ""):
        super().__init__(cfg)
        self._max, self._budget, self._thinking = int(max_calls), float(budget_s or 0.0), str(thinking or "")

    @property
    def thinking(self) -> str:
        return self._thinking or str(getattr(self._cfg, "thinking", "medium") or "medium")

    @property
    def max_tool_calls(self) -> int:
        want = self._max or int(getattr(self._cfg, "max_tool_calls", MAX_CALLS) or MAX_CALLS)
        return max(1, min(MAX_CALLS, want))

    @property
    def request_timeout_s(self) -> int:
        cap = max(5, int(self._budget or BUDGET_S))
        return max(5, min(cap, int(getattr(self._cfg, "request_timeout_s", 0) or 0) or cap))

    @property
    def disabled_tools(self) -> "list[str]":
        off = {str(x).strip() for x in (getattr(self._cfg, "disabled_tools", None) or [])}
        return sorted(off | {"forecast"})


def _live(cfg):
    """cfg may be the settings object itself or a callable returning the current one."""
    return cfg() if callable(cfg) else cfg


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _text(v) -> str:
    return "" if v is None else str(v)[:TEXT_MAX]


def _epoch(v, tz_offset_s: float = 0.0) -> Optional[float]:
    """A model-written "YYYY-MM-DD" at local noon, or a plain epoch number; None when unreadable."""
    n = _num(v)
    if n is not None:
        return n
    m = _DATE.match(str(v or "").strip())
    if not m:
        return None
    try:
        noon = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, tzinfo=timezone.utc)
    except ValueError:
        return None
    return noon.timestamp() - float(tz_offset_s or 0.0)


def _date(ts, tz_offset_s: float = 0.0) -> Optional[str]:
    n = _num(ts)
    return datetime.fromtimestamp(n + float(tz_offset_s or 0.0), tz=timezone.utc).strftime("%Y-%m-%d") if n is not None else None


def _fingerprint(row: dict) -> str:
    return f"{row.get('check') or ''}|{row.get('host') or '-'}|{row.get('subject') or '-'}"


def _key(row: dict, aliases: Callable[[str], str]) -> tuple:
    host = str(aliases(str(row.get("host") or "")) or "")
    return (str(row.get("check") or ""), host.lower(), fc.slug(row.get("subject")))


def agrees(model: dict, code: dict, now: float) -> bool:
    """True when the model's date and rate both sit inside the tolerance around the measured ones."""
    mp, cp = _num(model.get("predicted_at")), _num(code.get("predicted_at"))
    mr, cr = _num(model.get("rate")), _num(code.get("rate"))
    if cp is not None or mp is not None:
        if cp is None or mp is None:
            return False
        if abs(mp - cp) > max(TOL_DATE * (cp - float(now)), TOL_DATE_MIN_S):
            return False
    return cr is None or mr is None or abs(mr - cr) <= TOL_RATE * abs(cr)


def known_hosts(data) -> set:
    """Lower-cased host names the run measured; empty when they cannot be read, which stores no model host."""
    try:
        rows = data.hosts() if data is not None else None
    except Exception as e:  # noqa: BLE001 — an unreadable registry stores no model-written host
        log.debug("forecast tower host list read failed: %s", type(e).__name__)
        rows = None
    return {str(h).strip().lower() for h in (rows or []) if str(h or "").strip()}


def _model_row(m: dict, aliases: Callable[[str], str] = str, hosts: Optional[set] = None) -> Optional[dict]:
    """A finding only Tower saw: no measured row backs it, so every field it could hide a figure in is
    dropped and it is kept only for a known host with figure-free wording. `hosts=None` skips the host
    check; an empty set keeps no model-written host at all."""
    summary = str(m.get("summary") or m.get("cause") or "")
    action = str(m.get("suggested_action") or "").strip()
    subject = str(m.get("subject") or "").strip()[:SUBJECT_MAX]
    host = str(aliases(str(m.get("host") or "")) or "").strip()
    if host and hosts is not None and host.lower() not in hosts:
        log.debug("forecast tower model-only row dropped: its host is not one of the measured ones")
        return None
    if not number_free(summary, {}) or (action and not number_free(action, {})) or (subject and not number_free(subject, {})):
        log.debug("forecast tower model-only row dropped: its wording carries a figure")
        return None
    row = {k: m.get(k) for k in _ROW_KEYS}
    row.update({"check": str(row.get("check") or ""), "host": host or None,
                "subject": subject or None, "severity": "info", "confidence": "low", "graph": None,
                "summary": _text(summary), "detail": "", "suggested_action": _text(action), "unit": "",
                "since": None, "predicted_at": None, "rate": None})
    row["fingerprint"] = _fingerprint(row)
    return row


def with_cause(detail, cause: str) -> Optional[str]:
    """The measured detail plus Tower's cause within TEXT_MAX: the cause is cut at a space and ellipsed, and
    skipped altogether when fewer than CAUSE_MIN characters would be left for it."""
    head = f"{str(detail or '').strip()} {CAUSE}".strip()
    room = TEXT_MAX - len(head) - 1
    if room >= len(cause):
        return f"{head} {cause}"
    if room < CAUSE_MIN:
        return None
    cut = cause[:room - 1]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return f"{head} {cut}…" if cut.strip() else None


def _words(text) -> "list[str]":
    """The content words of a text: normalised tokens of at least WORD_MIN letters."""
    return [w for w in (_norm_token(t) for t in str(text or "").split()) if len(w) >= WORD_MIN]


def restates(cause: str, code: dict) -> bool:
    """True when the cause mostly repeats words the finding's summary and detail already carry."""
    words = [w for w in _words(cause) if w not in FILLER]
    if not words:
        return False
    have = {w[:STEM] for w in _words(f"{(code or {}).get('summary') or ''} {(code or {}).get('detail') or ''}")}
    return sum(1 for w in words if w[:STEM] in have) >= RESTATE_RATIO * len(words)


def thin(cause: str) -> bool:
    """True when fewer than CAUSE_WORDS_MIN content words are left once the filler words are dropped."""
    return len([w for w in _words(cause) if w not in FILLER]) < CAUSE_WORDS_MIN


def wording(code: dict, m: dict) -> dict:
    """The one column Tower may add to a measured finding: its likely cause appended to the detail, only when
    that text carries no figure and does not just restate the finding."""
    cause = str(m.get("cause") or "").strip()
    if not cause or not number_free(cause, code) or thin(cause) or restates(cause, code):
        return {}
    detail = with_cause(code.get("detail"), cause)
    return {"detail": detail} if detail else {}


def _merged(code: dict, m: dict) -> "tuple[dict, bool]":
    """The measured row with Tower's wording where it passed the guard, and whether any was taken;
    a cause that was written but refused leaves its mark in tower_note."""
    add = wording(code, m)
    if add:
        return {**code, **add}, True
    return ({**code, "tower_note": DISCARDED} if str(m.get("cause") or "").strip() else dict(code)), False


def _differed(code: dict) -> dict:
    """The measured row with the differed sentence appended once."""
    row = dict(code)
    detail = str(row.get("detail") or "").strip()
    row["detail"] = detail if detail.endswith(DIFFERED) else f"{detail} {DIFFERED}".strip()
    return row


def model_rows(rows, check_id: str, *, tz_offset_s: float = 0.0) -> "list[dict]":
    """Model findings as finding rows: check, severity, confidence and graph stay the code's, text and figures are capped."""
    out: "list[dict]" = []
    for r in list(rows or [])[:MODEL_MAX * 5]:
        if not isinstance(r, dict):
            continue
        host, subject = _text(r.get("host"))[:120].strip(), _text(r.get("subject"))[:120].strip()
        if not host and not subject:
            continue
        row = {"check": str(check_id), "host": host or None, "subject": subject or None, "severity": "info",
               "summary": _text(r.get("summary") or r.get("cause")), "detail": _text(r.get("detail")),
               "since": _epoch(r.get("since"), tz_offset_s), "predicted_at": _epoch(r.get("predicted_at"), tz_offset_s),
               "rate": _num(r.get("rate")), "unit": _text(r.get("unit"))[:40], "confidence": "low",
               "suggested_action": _text(r.get("suggested_action")), "graph": None, "cause": _text(r.get("cause"))}
        row["fingerprint"] = _fingerprint(row)
        out.append(row)
        if len(out) >= MODEL_MAX:
            break
    return out


def _verify_indexed(model_findings: "list[dict]", code_findings: "list[dict]", now: float,
                    aliases: Callable[[str], str] = str,
                    hosts: Optional[set] = None) -> "list[tuple[Optional[int], dict, str, bool, bool]]":
    """verify() carrying each row's index into code_findings (None for a model-only row) and whether the model
    agreed on the figures; one row per code finding in code order, then the model-only rows."""
    code = list(code_findings or [])
    keys = [_key(c, aliases) for c in code]
    matched: "list[Optional[tuple[dict, str, bool, bool]]]" = [None] * len(code)
    extra: "list[tuple[Optional[int], dict, str, bool, bool]]" = []
    seen: set = set()
    for m in model_findings or []:
        if not isinstance(m, dict):
            continue
        k = _key(m, aliases)
        i = next((j for j, ck in enumerate(keys) if ck == k and matched[j] is None), None)
        if i is None:
            if k in seen or k in keys:
                continue
            seen.add(k)
            only = _model_row(m, aliases, hosts)
            if only is not None:
                extra.append((None, only, "model", False, False))
            continue
        if not agrees(m, code[i], now):
            matched[i] = (_differed(code[i]), "code", True, False)
            continue
        row, took = _merged(code[i], m)
        matched[i] = (row, "tower+code" if took else "code", False, True)
    return [(j, *(matched[j] or (dict(c), "code", False, False))) for j, c in enumerate(code)] + extra


def verify(model_findings: "list[dict]", code_findings: "list[dict]", now: float,
           aliases: Callable[[str], str] = str) -> "list[tuple[dict, str, bool]]":
    """Pairs each model finding with the first unmatched measured one; every code finding is returned, and a
    model finding whose measured rows are all already matched is dropped as a repeat of a reported trend."""
    return [(r, v, d) for _i, r, v, d, _a in _verify_indexed(model_findings, code_findings, now, aliases)]


def _too_deep(chunk: str) -> bool:
    """True when bracket nesting goes past PARSE_MAX_DEPTH; one linear scan, quotes not tracked."""
    depth = 0
    for ch in chunk:
        if ch in "[{":
            depth += 1
            if depth > PARSE_MAX_DEPTH:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def _parse_json(text: str, opener: str, closer: str, want: type):
    """The first `want` value in a ```json fence or a bracketed block; None past the size or nesting caps."""
    body = str(text or "")
    if len(body) > PARSE_MAX_CHARS:
        log.debug("forecast parse skipped chars=%d", len(body))
        return None
    candidates = [m.group(1) for m in _JSON_FENCE.finditer(body)]
    start, end = body.find(opener), body.rfind(closer)
    if 0 <= start < end:
        candidates.append(body[start:end + 1])
    for chunk in candidates:
        if _too_deep(chunk):
            log.debug("forecast parse skipped nesting chars=%d", len(chunk))
            continue
        try:
            obj = json.loads(chunk)
        except (ValueError, RecursionError, MemoryError):
            continue
        if isinstance(obj, want):
            return obj
    return None


def parse_findings(text: str) -> "list[dict]":
    """The JSON array from a ```json fence, else the first bracketed block; [] when nothing parses."""
    return [o for o in (_parse_json(text, "[", "]", list) or []) if isinstance(o, dict)]


def parse_object(text: str) -> dict:
    """The JSON object from a ```json fence, else the first braced block; {} when nothing parses."""
    return _parse_json(text, "{", "}", dict) or {}


def _drop(body: str, needle: str) -> str:
    """Every case-insensitive occurrence of needle replaced by a space."""
    if not needle:
        return body
    low, n = body.lower(), needle.lower()
    out, i, j = [], 0, low.find(n)
    while j >= 0:
        out.append(body[i:j])
        out.append(" ")
        i = j + len(n)
        j = low.find(n, i)
    out.append(body[i:])
    return "".join(out)


def idents_row(rows: "list[dict]") -> dict:
    """One row carrying every given row's names and text, for guarding wording that spans them all."""
    rows = list(rows or [])
    return {"host": " ".join(str(r.get("host") or "") for r in rows),
            "subject": " ".join(str(r.get("subject") or "") for r in rows),
            "summary": " ".join(" ".join(str(r.get(k) or "") for k in _TEXT_KEYS) for r in rows)}


def _norm_token(tok: str) -> str:
    """One token without its surrounding punctuation or a trailing possessive, casefolded."""
    t = tok.strip(_PUNCT)
    for poss in ("'s", "’s"):
        if len(t) > len(poss) and t[-len(poss):].casefold() == poss:
            t = t[:-len(poss)]
            break
    return t.strip(_PUNCT).casefold()


def _numeric(text: str) -> bool:
    """True when any character is a Unicode number — digits, fractions and roman numerals alike."""
    return any(unicodedata.category(ch)[0] == "N" for ch in text)


def row_tokens(row: dict) -> set:
    """The row's own identifiers: whole tokens of its host, subject and text that carry at least one letter."""
    out = set()
    for k in ("host", "subject", *_TEXT_KEYS):
        for tok in str(row.get(k) or "").split():
            bare = _norm_token(tok)
            if bare and any(ch.isalpha() for ch in bare):
                out.add(bare)
    return out


def number_free(text, row) -> bool:
    """True when the wording carries no figure at all. A token is exempt only when it equals a whole token of the
    row that carries a letter; anything else numeric, a number word or a quantity of a unit is a refusal."""
    body = str(text or "").strip()
    if not body:
        return False
    row = row or {}
    names = row_tokens(row)
    fields = [str(row.get(k) or "").casefold() for k in _TEXT_KEYS]
    for m in list(_QUOTED.finditer(body))[:_QUOTED_MAX]:
        seg = m.group(1).strip().casefold()
        if seg and any(seg in f for f in fields):
            body = _drop(body, m.group(0))
    kept = []
    for tok in body.split():
        bare = _norm_token(tok)
        if not bare or bare in names:
            continue
        if _numeric(bare) and any(ch.isalpha() for ch in bare):
            return False
        kept.append(bare)
    rest = " ".join(kept)
    return not (_numeric(rest) or _NUM_WORDS.search(rest) or _QTY_WORDS.search(rest))


def _bypassing(row: dict) -> bool:
    """True when a finding's own text reads like a rule-bypass attempt; such a row is never sent to a model."""
    body = " ".join(str(row.get(k) or "") for k in ("check", "check_id", "title", "host", "subject", *_TEXT_KEYS))
    return bool(tower._BYPASS.search(tower._norm(body)))


def ordered_rows(rows: "list[dict]") -> "list[dict]":
    """Findings worst severity first, without any whose own text would carry a rule-bypass attempt."""
    kept = [r for r in list(rows or []) if not _bypassing(r)]
    return sorted(kept, key=lambda r: -_SEV.get(str(r.get("severity") or ""), 0))


def _letter_id(i: int) -> str:
    """The i-th (1-based) id as lower-case letters only: a, b, … z, aa, ab, … — never a digit."""
    out = []
    n = i
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(ord("a") + rem))
    return "".join(reversed(out))


def batch_input(rows: "list[dict]") -> "tuple[list[dict], dict[str, dict]]":
    """One batch for a direct call: letter ids, the check's title, the host and the measured summary."""
    payload, by_id = [], {}
    for i, r in enumerate(list(rows or []), 1):
        fid = _letter_id(i)
        by_id[fid] = r
        payload.append({"id": fid, "check": _text(r.get("title") or r.get("check_id") or r.get("check")),
                        "host": _text(r.get("host") or ""), "summary": _text(r.get("summary"))})
    return payload, by_id


def _digest_input(rows: "list[dict]") -> "list[dict]":
    """The digest call's rows: check, host and summary only — no id, since the digest never cites one."""
    return [{"check": _text(r.get("title") or r.get("check_id") or r.get("check")),
             "host": _text(r.get("host") or ""), "summary": _text(r.get("summary"))} for r in list(rows or [])]


def digest_text(obj: dict, rows: "list[dict]") -> Optional[str]:
    """The digest sentence, kept only while it carries no figure and stays within the text cap."""
    digest = str((obj or {}).get("digest") or "").strip()
    if not digest or len(digest) > TEXT_MAX or not number_free(digest, idents_row(list(rows or []))):
        return None
    return digest


def cause_texts(obj: dict, by_id: "dict[str, dict]") -> "dict[str, dict]":
    """Accepted detail columns keyed by finding id; an unknown id is ignored and the first answer for an id wins."""
    rows: "dict[str, dict]" = {}
    for item in list((obj or {}).get("findings") or [])[:MODEL_MAX]:
        if not isinstance(item, dict):
            continue
        code = by_id.get(str(item.get("id") or ""))
        if code is None or code.get("id") is None or str(code["id"]) in rows:
            continue
        add = wording(code, item)
        if add:
            rows[str(code["id"])] = add
    return rows


def refused_ids(obj: dict, by_id: "dict[str, dict]", accepted: "dict[str, dict]") -> "list[str]":
    """Finding ids whose answer carried a cause that the guard refused."""
    out: "list[str]" = []
    for item in list((obj or {}).get("findings") or [])[:MODEL_MAX]:
        if not isinstance(item, dict) or not str(item.get("cause") or "").strip():
            continue
        code = by_id.get(str(item.get("id") or ""))
        fid = None if code is None or code.get("id") is None else str(code["id"])
        if fid is not None and fid not in accepted and fid not in out:
            out.append(fid)
    return out


def prompt(check, window_days: int, code_findings: "Optional[list[dict]]" = None, *,
           measured: "Optional[list[dict]]" = None, tz_offset_s: float = 0.0) -> str:
    """The Forecast question for one check; the retry appends the measured figures."""
    text = (f"Forecast check: {check.title}. Look at the last {int(window_days)} days using only read tools "
            "(host_history, energy_summary, alarm_history, recent_runs, service_health, gateway_flow). Find trends "
            "that will become a problem: what, where, since when, the predicted outcome and date, and one suggested "
            "action. Answer with a JSON array only, one object per finding: " + _SHAPE +
            ". An empty array means nothing found. Do not change anything.")
    if not code_findings:
        if not measured:
            return text
        return (text + " Code measured: " + json.dumps(_figure_rows(measured, tz_offset_s), default=str)
                + ". Start from these figures instead of rediscovering them.")
    return (text + " Measured figures for the same trend: "
            + json.dumps(_figure_rows(code_findings, tz_offset_s), default=str)
            + ". Re-check and answer again in the same JSON shape.")


def _figure_rows(rows: "list[dict]", tz_offset_s: float) -> "list[dict]":
    return [{"host": r.get("host"), "subject": r.get("subject"), "predicted_at": _date(r.get("predicted_at"), tz_offset_s),
             "rate": r.get("rate"), "unit": r.get("unit")} for r in rows]


def native_body(body: dict, user_text: str) -> dict:
    """The LM Studio native chat request for a direct call: one input turn, the short system prompt, and the
    reasoning setting the model accepts for the call's thinking level."""
    level = "off" if str(body.get("reasoning_effort") or "") in ("", "none") else str(body["reasoning_effort"])
    out = {"model": body["model"], "system_prompt": DIRECT_SYSTEM, "input": user_text, "store": False,
           "temperature": max(0.0, min(1.0, float(body.get("temperature") or 0.0))),
           "max_output_tokens": int(body.get("max_tokens") or 0)}
    setting = lms_native.reasoning_setting(level, lms_native.thinking_options(body["model"]))
    if setting is not None:
        out["reasoning"] = setting
    return out


class TowerPass:
    """Tower's optional part in a Forecast run: a digest and a likely cause through direct calls, and at the Full
    tier an investigation of each flagged check; measured summaries, figures and advice always stay the code's."""

    def __init__(self, store, *, registry_factory, complete_stream, entries, server_args_of, tower_cfg, forecast_cfg,
                 report_violation: Optional[Callable[[dict], None]] = None, host_alias: Callable[[str], str] = str,
                 now: Callable[[], float] = time.time, tz_offset_s: Callable[[], float] = lambda: 0,
                 native_stream: Optional[Callable] = None):
        self._store, self._registry_factory, self._cs = store, registry_factory, complete_stream
        self._native = native_stream
        self._entries, self._server_args_of = entries, server_args_of
        self._tower_cfg, self._forecast_cfg = tower_cfg, forecast_cfg
        self._report_violation, self._alias = report_violation, host_alias
        self._now, self._tz = now, tz_offset_s
        self._run_keys: set = set()
        self._profile = fe.PROFILES["off"]
        self._speeds: "list[float]" = []
        self._timed_out, self._calls = False, 0
        self._digest_discarded = False

    def _tcfg(self):
        return _live(self._tower_cfg)

    def _fcfg(self):
        return _live(self._forecast_cfg)

    def _model(self) -> Optional[dict]:
        """The chat model this pass would use; sleeping copies never count, so no pass wakes a host."""
        return tower.resolve_model(self._tcfg(), [e for e in (self._entries() or []) if not _asleep(e)])

    def model_id(self) -> Optional[str]:
        """The id of the model this pass would use, for the effort signals; None when there is none."""
        try:
            model = self._model()
        except Exception as e:  # noqa: BLE001 — an unreadable registry means no Tower part
            log.debug("forecast tower model lookup failed: %s", type(e).__name__)
            return None
        return None if model is None else str(model.get("model") or "") or None

    def profile(self):
        """The profile pinned for this run; the off profile until begin_run pins one."""
        return self._profile

    def eligible(self) -> bool:
        """Tower on and one awake chat model."""
        if not bool(getattr(self._tcfg(), "enabled", False)):
            return False
        return self._model() is not None

    def model_calls(self) -> int:
        """How many of this run's calls reached the model: the digest, each cause batch and each conversation turn."""
        return self._calls

    def digest_discarded(self) -> bool:
        """True when Tower wrote a digest this run and the guard refused it."""
        return self._digest_discarded

    def measured_tok_s(self) -> Optional[float]:
        """The run's own generation speed, averaged over its direct calls; None when nothing was measured."""
        return round(sum(self._speeds) / len(self._speeds), 1) if self._speeds else None

    def timed_out(self) -> bool:
        return self._timed_out

    def begin_run(self, profile=None) -> None:
        """Starts a run: the echo filter, the speed samples, the call count and the timeout flag are reset and
        the run's profile is pinned for every call in it."""
        self._run_keys = set()
        self._speeds, self._timed_out, self._calls = [], False, 0
        self._digest_discarded = False
        self._profile = profile if isinstance(profile, fe.Profile) else fe.PROFILES["off"]

    def start_thread(self, label: str) -> Optional[str]:
        """One private thread for a Forecast turn; None when the store refuses it."""
        try:
            return self._store.create_thread(ACTOR, str(label)[:60], {"tab": "forecast"})
        except Exception as e:  # noqa: BLE001 — a pass runs fine without its transcript
            log.debug("forecast thread create failed: %s", type(e).__name__)
            return None

    def end_thread(self, tid) -> bool:
        """Drops the pass's thread unless Forecast keeps its Tower history; True when the thread is kept."""
        if not tid:
            return False
        if bool(getattr(self._fcfg(), "tower_history", False)):
            return True
        try:
            self._store.delete_thread(ACTOR, str(tid))
        except Exception as e:  # noqa: BLE001 — an undeleted thread is swept with the rest
            log.debug("forecast thread delete failed: %s", type(e).__name__)
        return False

    def _echo_key(self, row: dict) -> tuple:
        host = str(self._alias(str(row.get("host") or "")) or "").strip().lower()
        return (host, " ".join(str(row.get("summary") or "").split()).lower())

    def __call__(self, check, data, code_findings: "list[dict]", thread_id) -> "Optional[list[tuple[dict, str]]]":
        """One verified Tower pass over a check; None unless the run's tier investigates and Tower is eligible."""
        code = [dict(r) for r in code_findings or []]
        if not bool(getattr(check, "tower", False)) or not self._profile.investigate or not self.eligible():
            return None
        model = self._model()
        if model is None:
            return None
        for r in code:
            self._run_keys.add(self._echo_key(r))
        hosts = known_hosts(data)
        window = int(getattr(self._fcfg(), "window_days", 14) or 14)
        context = [r for r in code if not _bypassing(r)]
        text = self._turn(check.id, thread_id, prompt(check, window, measured=context, tz_offset_s=self._tz()), model)
        if text is None:
            return [(r, "code") for r in code]
        try:
            rows = self._verify(check, text, code, hosts)
            again = [i for i, _r, _v, dis, _a in rows if dis and i is not None]
            if again:
                rows = self._retry(check, thread_id, model, rows, code, again, window, hosts)
            rows = [t for t in rows if t[0] is not None or self._echo_key(t[1]) not in self._run_keys]
        except Exception as e:  # noqa: BLE001 — untrusted model text never fails the run
            log.debug("forecast tower verify failed check=%s: %s", check.id, type(e).__name__)
            return [(r, "code") for r in code]
        counts: "dict[str, int]" = {}
        for _i, _r, v, _d, _a in rows:
            counts[v] = counts.get(v, 0) + 1
        log.debug("forecast tower check=%s code=%d rows=%d verified=%s", check.id, len(code), len(rows), counts)
        return [(r, v) for _i, r, v, _d, _a in rows]

    def digest(self, rows: "list[dict]") -> Optional[str]:
        """One direct call tying the run's findings together; None when the tier has no digest, the call failed
        or the answer carried a figure. Nothing is stored in Tower's History."""
        if self._profile.tier == "off" or not self.eligible():
            return None
        batch = ordered_rows(rows)[:self._profile.batch_size]
        payload = _digest_input(batch)
        if not payload:
            return None
        text = self._direct(_FINDINGS + json.dumps(payload, default=str) + DIGEST_TAIL)
        if text is None:
            return None
        try:
            answer = parse_object(text)
            out = digest_text(answer, batch)
        except Exception as e:  # noqa: BLE001 — untrusted model text never fails the run
            log.debug("forecast tower digest parse failed: %s", type(e).__name__)
            return None
        self._digest_discarded = out is None and bool(str((answer or {}).get("digest") or "").strip())
        log.debug("forecast tower digest sent=%d kept=%s", len(payload), bool(out))
        return out

    def causes(self, rows: "list[dict]") -> "dict[str, dict]":
        """A likely cause per finding through direct calls, in batches: {"detail": …} for an accepted cause and
        {"tower_note": DISCARDED} for a refused one; {} when the tier has none. A failed batch stops the rest."""
        if not self._profile.per_finding_cause or not self.eligible():
            return {}
        ordered = ordered_rows(rows)
        out: "dict[str, dict]" = {}
        size = max(1, int(self._profile.batch_size))
        for n in range(max(0, int(self._profile.max_batches))):
            batch = ordered[n * size:(n + 1) * size]
            if not batch:
                break
            payload, by_id = batch_input(batch)
            text = self._direct(_FINDINGS + json.dumps(payload, default=str) + CAUSE_TAIL)
            try:
                answer = parse_object(text) if text is not None else {}
            except Exception as e:  # noqa: BLE001 — untrusted model text never fails the run
                log.debug("forecast tower cause parse failed: %s", type(e).__name__)
                answer = {}
            if not isinstance(answer.get("findings"), list):
                log.debug("forecast tower cause batch %d answered nothing usable; later batches skipped", n + 1)
                break
            kept = cause_texts(answer, by_id)
            out.update(kept)
            out.update({fid: {"tower_note": DISCARDED} for fid in refused_ids(answer, by_id, kept) if fid not in out})
        log.debug("forecast tower causes sent=%d kept=%d", len(ordered), sum(1 for v in out.values() if "detail" in v))
        return out

    def _direct(self, user_text: str) -> Optional[str]:
        """One streamed completion outside the conversation loop: the short system prompt, no tools, no history.
        None when it failed or ran out of time; the generation speed is recorded."""
        model = self._model()
        if model is None:
            return None
        profile = self._profile
        provider = str(model.get("provider") or "llama")
        timeout_s = fe.DIRECT_TIMEOUT_S.get(profile.tier, profile.timeout_s)
        cfg = _Capped(self._tcfg(), max_calls=0, budget_s=timeout_s, thinking=fe.DIRECT_THINKING)
        body = {"model": model["model"], "temperature": float(getattr(cfg, "temperature", 0.2) or 0.0),
                "max_tokens": int(profile.max_tokens),
                "messages": [{"role": "system", "content": DIRECT_SYSTEM}, {"role": "user", "content": user_text}]}
        try:
            body.update(tower.thinking_params(cfg, provider, int(profile.max_tokens)))
        except Exception as e:  # noqa: BLE001 — a missing thinking field never stops the call
            log.debug("forecast tower thinking params failed: %s", type(e).__name__)
        # Providers with no thinking switch get extra tokens to answer in.
        if provider not in _KWARGS_PROVIDERS:
            body["max_tokens"] = int(body["max_tokens"]) + REASONING_HEADROOM
        native = self._native is not None and provider == "lms" and lms_native.available(model["model"])
        t0 = time.monotonic()
        deadline = t0 + float(timeout_s)
        parts: "list[str]" = []
        tokens, first, thought, reached = None, None, 0, False
        try:
            read_timeout = min(DIRECT_READ_MAX, int(timeout_s))
            if native:
                gen = lms_native.as_chunks(self._native(native_body(body, user_text), label="forecast",
                                                        read_timeout=read_timeout))
            else:
                gen = self._cs(body, label="forecast", read_timeout=read_timeout)
            with contextlib.closing(gen):
                for chunk in gen:
                    if not reached:
                        reached = True
                        self._calls += 1
                    delta = ((chunk.get("choices") or [{}])[0].get("delta") or {}) if isinstance(chunk, dict) else {}
                    if first is None and (delta.get("content") or delta.get("reasoning_content")):
                        first = time.monotonic()
                    parts.append(str(delta.get("content") or ""))
                    thought += len(str(delta.get("reasoning_content") or ""))
                    usage = chunk.get("usage") if isinstance(chunk, dict) else None
                    if isinstance(usage, dict) and usage.get("completion_tokens"):
                        tokens = _num(usage.get("completion_tokens"))
                    if time.monotonic() > deadline:
                        self._timed_out = True
                        log.debug("forecast tower direct call ran out of time tier=%s", profile.tier)
                        return None
        except Exception as e:  # noqa: BLE001 — the measured findings stand without Tower
            self._timed_out = self._timed_out or str(getattr(e, "err_type", "")) == "timeout"
            log.warning("forecast tower direct call failed: %s", type(e).__name__)
            return None
        self._record_speed(tokens, first, time.monotonic())
        text = "".join(parts)
        log.debug("forecast tower direct call tier=%s provider=%s native=%s ms=%d chars=%d thought=%d reasoning_only=%s",
                  profile.tier, provider, native, int((time.monotonic() - t0) * 1000), len(text), thought,
                  bool(thought and not text))
        return text or None

    def _record_speed(self, tokens: Optional[float], first: Optional[float], end: float) -> None:
        """One generation-speed sample: the completion tokens over the seconds spent generating them.
        Too few tokens to be worth measuring, or an impossible rate, is no sample at all."""
        if not tokens or first is None or float(tokens) < SPEED_MIN_TOKENS:
            return
        seconds = end - first
        if seconds < SPEED_MIN_S:
            return
        rate = float(tokens) / seconds
        if SPEED_MIN <= rate <= SPEED_MAX:
            self._speeds.append(rate)

    def _verify(self, check, text: str, code: "list[dict]",
                hosts: Optional[set] = None) -> "list[tuple[Optional[int], dict, str, bool, bool]]":
        rows = model_rows(parse_findings(text), check.id, tz_offset_s=self._tz())
        return _verify_indexed(rows, code, self._now(), self._alias, hosts)

    def _retry(self, check, thread_id, model, rows, code, again: "list[int]", window,
               hosts: Optional[set] = None) -> "list[tuple[Optional[int], dict, str, bool, bool]]":
        """One re-ask with the measured figures; each answer replaces its own code row, by position."""
        subset = [code[i] for i in again]
        text = self._turn(check.id, thread_id, prompt(check, window, subset, tz_offset_s=self._tz()), model)
        if text is None:
            return rows
        try:
            fresh = self._verify(check, text, subset, hosts)
        except Exception as e:  # noqa: BLE001 — a bad retry answer leaves the first pass standing
            log.debug("forecast tower retry verify failed check=%s: %s", check.id, type(e).__name__)
            return [(i, r, v, False, a) for i, r, v, _d, a in rows]
        # Only a retry answer that agrees on the figures may replace a row; anything else stays measured, marked.
        agreed = {again[j]: (r, v) for j, r, v, _d, ag in fresh if j is not None and ag}
        out = []
        for i, r, v, dis, a in rows:
            if not (dis and i is not None):
                out.append((i, r, v, False, a))
            elif i in agreed:
                out.append((i, *agreed[i], False, True))
            else:
                out.append((i, _differed(code[i]), "code", False, False))
        return out

    def _turn(self, label: str, thread_id, user_text: str, model: dict, *, cfg=None, registry=None) -> Optional[str]:
        """One read-only turn at the run's tier; None when it was refused, errored or ran out of time."""
        events: list = []
        profile = self._profile
        budget = float(profile.timeout_s or BUDGET_S)
        t0 = time.monotonic()
        deadline = t0 + budget
        try:
            out = tower.run_turn(thread_id=thread_id, user_text=user_text, page={"tab": "forecast", "check": label},
                                 cfg=cfg if cfg is not None else _Capped(self._tcfg(), max_calls=profile.max_tool_calls,
                                                                         budget_s=budget, thinking=profile.thinking),
                                 role="operator",
                                 registry=self._registry_factory() if registry is None else registry,
                                 complete_stream=self._cs, store=self._store, emit=events.append, model=model,
                                 cancelled=lambda: time.monotonic() > deadline, server_args_of=self._server_args_of,
                                 run_id=f"forecast-{label}", actor=ACTOR, report_violation=self._report_violation)
        except Exception as e:  # noqa: BLE001 — the measured findings stand without Tower
            self._timed_out = self._timed_out or str(getattr(e, "err_type", "")) == "timeout"
            log.warning("forecast tower turn failed check=%s: %s", label, type(e).__name__)
            return None
        if any(e.get("event") in _MODEL_EVENTS for e in events):
            self._calls += 1
        err = next((e.get("message") for e in events if e.get("event") == "error"), None)
        if time.monotonic() > deadline or (err and tower._TIMEOUT_MARK in str(err)):
            self._timed_out = True
        bypass = "rule-bypass attempt" in str((out or {}).get("note") or "")
        log.debug("forecast tower turn check=%s ms=%d calls=%s ok=%s", label, int((time.monotonic() - t0) * 1000),
                  (out or {}).get("calls"), not (err or bypass))
        return None if (err or bypass) else "".join(e.get("text") or "" for e in events if e.get("event") == "delta")
