"""Draft-model discovery (#889): the smallest same-family instruct GGUF on Hugging Face,
picked for speculative decoding and cached like model_meta."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.parse
from typing import Any, Callable, Optional

from model_meta import HF_HOST, Fetcher, MetaCache, resolve_repo, ttl_for, valid_repo, _cfg

MAX_DRAFT_RATIO = 0.25
SEARCH_LIMIT = 50
_SIZE_TOKEN_RE = re.compile(r"[-_](\d+(?:\.\d+)?)[bB](?=[-_.]|$)")
_QUANT_RE = re.compile(r"[-_.](Q4_K_M|Q4_K_S|Q4_0|Q4_1|Q5_K_M|Q5_K_S|Q5_0|Q5_1|IQ4_NL|IQ4_XS)\.gguf$", re.IGNORECASE)
_INSTRUCT_RE = re.compile(r"instruct|chat|[-_]it(?:[-_]|$)", re.IGNORECASE)
_BASE_RE = re.compile(r"[-_](base|pt|pretrain(?:ed)?)(?:[-_]|$)", re.IGNORECASE)
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.gguf$")


def family_prefix(name: str) -> str:
    base = (name or "").split("/")[-1]
    m = _SIZE_TOKEN_RE.search(base)
    return (base[:m.start()] if m else base).lower()


def family_search_term(name: str) -> str:
    base = (name or "").split("/")[-1]
    m = _SIZE_TOKEN_RE.search(base)
    return base[:m.start()] if m else base


def params_b(name: str) -> Optional[float]:
    m = _SIZE_TOKEN_RE.search((name or "").split("/")[-1])
    return float(m.group(1)) if m else None


def is_instruct(row: dict) -> bool:
    text = " ".join([str(row.get("id") or "")] + [str(t) for t in (row.get("tags") or [])])
    return bool(_INSTRUCT_RE.search(text))


def _is_base(name: str) -> bool:
    return bool(_BASE_RE.search((name or "").split("/")[-1]))


def pick_repo(target_repo: str, rows: list) -> Optional[dict]:
    """Same family, not a base model, ≤ 25 % of the target's parameters; instruct-tagged first, then smallest."""
    fam, tp = family_prefix(target_repo), params_b(target_repo)
    if not tp:
        return None
    cands = []
    for r in rows or []:
        rid = str(r.get("id") or "")
        p = params_b(rid)
        if not valid_repo(rid) or family_prefix(rid) != fam or _is_base(rid) or p is None or p > tp * MAX_DRAFT_RATIO:
            continue
        cands.append((0 if is_instruct(r) else 1, p, -int(r.get("downloads") or 0), r))
    if not cands:
        return None
    cands.sort(key=lambda c: c[:3])
    return cands[0][3]


def pick_file(siblings: list, max_bytes: Optional[int] = None) -> Optional[dict]:
    """Q4_K_M when present, else the smallest Q4 / Q5 / IQ4 file within max_bytes; other files ignored."""
    rows = []
    for s in siblings or []:
        name = str(s.get("rfilename") or "")
        if name.lower().startswith("mmproj") or not _FILE_RE.match(name) or not _QUANT_RE.search(name):
            continue
        size = int(s.get("size") or 0)
        if max_bytes is not None and size > int(max_bytes):
            continue
        rows.append({"file": name, "size_bytes": size})
    if not rows:
        return None
    q4km = next((r for r in rows if r["file"].upper().endswith("Q4_K_M.GGUF")), None)
    return q4km or min(rows, key=lambda r: r["size_bytes"] or float("inf"))


def _search(term: str, fetch) -> tuple[Optional[list], bool]:
    url = (f"{HF_HOST}/api/models?search={urllib.parse.quote(term, safe='')}"
           f"&filter=gguf&sort=downloads&direction=-1&limit={SEARCH_LIMIT}")
    status, body = fetch(url)
    if status != 200:
        return None, False
    try:
        data = json.loads(body)
    except ValueError:
        return None, False
    return (data if isinstance(data, list) else []), True


def _siblings(repo: str, fetch) -> Optional[list]:
    status, body = fetch(f"{HF_HOST}/api/models/{repo}?blobs=true")
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    sibs = data.get("siblings") if isinstance(data, dict) else None
    return sibs if isinstance(sibs, list) else None


def build_candidate(target_repo: str, fetch: Callable[[str], tuple[int, str]],
                    max_bytes: Optional[int] = None) -> dict:
    doc: dict = {"target": target_repo, "candidate": None, "reason": "", "error": None,
                 "fetched_ts": int(time.time())}
    if not params_b(target_repo):
        doc["reason"] = "target parameter count unknown"
        return doc
    rows, ok = _search(family_search_term(target_repo), fetch)
    if not ok:
        doc["error"], doc["reason"] = "search unavailable", "Hugging Face search failed"
        return doc
    fam = family_prefix(target_repo)
    tried, over = 0, False
    remaining = list(rows or [])
    while remaining and tried < 3:
        repo_row = pick_repo(target_repo, remaining)
        if repo_row is None:
            break
        tried += 1
        remaining = [r for r in remaining if r is not repo_row]
        rid = str(repo_row["id"])
        sibs = _siblings(rid, fetch) or []
        f = pick_file(sibs, max_bytes)
        if f is None:
            over = over or (max_bytes is not None and pick_file(sibs) is not None)
            continue
        doc["candidate"] = {"repo": rid, "file": f["file"], "size_bytes": f["size_bytes"], "params_b": params_b(rid)}
        doc["reason"] = f"smallest {'instruct' if is_instruct(repo_row) else 'same-family'} GGUF at ≤ 25 % of the target"
        return doc
    doc["reason"] = (f"no draft small enough for auto-detect (≤ {int(max_bytes) / 1e9:.1f} GB)" if over
                     else f"no smaller GGUF of the {fam} family on Hugging Face")
    return doc


def register_routes(app, ctx, *, db_path: str, read_ini: Callable[[], Any],
                    fetcher_factory: Callable[[str], Any] = Fetcher) -> None:
    """Mount GET /api/llm/draft-candidates on the manager app."""
    from flask import jsonify, request as flask_request

    tls = threading.local()

    def conn_factory():
        conn = getattr(tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(db_path, timeout=30.0)
            conn.execute("PRAGMA busy_timeout=5000")
            tls.conn = conn
        return conn

    cache = MetaCache(conn_factory)
    cache.init()

    @app.route("/api/llm/draft-candidates")
    def llm_draft_candidates():
        model_id = (flask_request.args.get("model_id") or "").strip()
        if not model_id:
            return jsonify({"ok": False, "error": "model_id required"}), 400
        repo = resolve_repo(model_id, None)
        if repo is None and "/" not in model_id:
            section = None
            try:
                cp = read_ini()
                if cp.has_section(model_id):
                    section = dict(cp[model_id])
            except Exception:
                section = None
            repo = resolve_repo(model_id, section)
        if not repo:
            return jsonify({"ok": False, "error": "repo missing or malformed"}), 400
        token, ttl_s = _cfg(ctx)
        max_bytes = None
        try:
            raw = int(flask_request.args.get("max_bytes") or 0)
            max_bytes = raw if raw >= 1 else None
        except ValueError:
            max_bytes = None
        key = f"draft:{repo}:{max_bytes or 0}"
        refresh = flask_request.args.get("refresh") in ("1", "true", "yes")
        doc = None if refresh else cache.get(key, ttl_s)
        if doc is not None and int(time.time()) - int(doc["fetched_ts"]) >= ttl_for(doc, ttl_s):
            doc = None
        cached = doc is not None
        if doc is None:
            doc = build_candidate(repo, fetcher_factory(token).get, max_bytes)
            cache.put(key, doc)
        return jsonify({"ok": True, "cached": cached, **doc})
