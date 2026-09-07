"""Author-recommended sampling for a model (#878): generation_config.json
sidecar → model card → base model, cached in SQLite."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Optional

HF_HOST = "https://huggingface.co"
BODY_CAP = 512 * 1024
TIMEOUT_S = 10
ERROR_TTL_S = 3600

KEY_MAP = {
    "temperature": "temperature",
    "top_p": "top-p",
    "top_k": "top-k",
    "min_p": "min-p",
    "repetition_penalty": "repeat-penalty",
    "presence_penalty": "presence-penalty",
}
KEY_ORDER = ["temperature", "top-p", "top-k", "min-p", "repeat-penalty", "presence-penalty"]

_ALIASES = {
    "temperature": ("temperature", "temp"),
    "top-p": ("top_p", "top-p", "topp"),
    "top-k": ("top_k", "top-k", "topk"),
    "min-p": ("min_p", "min-p", "minp"),
    "repeat-penalty": ("repetition_penalty", "repeat_penalty", "repeat-penalty"),
    "presence-penalty": ("presence_penalty", "presence-penalty"),
}
_NUM = r"(-?\d+(?:\.\d+)?)(?!\.?\d)"
_SEP = r"[`\"']?\s*(?:[=:|]\s*|\s+)[`\"']?"
_KEY_RE = {
    key: re.compile(r"(?<![\w-])(?:--)?(?:" + "|".join(re.escape(a) for a in aliases) + r")" + _SEP + _NUM,
                    re.IGNORECASE)
    for key, aliases in _ALIASES.items()
}
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,95}/[A-Za-z0-9][A-Za-z0-9._\-]{0,95}$")


def valid_repo(s: Any) -> bool:
    return isinstance(s, str) and bool(_REPO_RE.match(s))


def resolve_repo(model_id: str, section: Optional[dict]) -> Optional[str]:
    """Repo from the config.ini section's hf-repo, else the model id's repo part."""
    sec = section or {}
    repo = sec.get("hf-repo") or sec.get("--hf-repo")
    if not repo and "/" in (model_id or ""):
        repo = model_id.rsplit(":", 1)[0]
    repo = (repo or "").strip()
    return repo if valid_repo(repo) else None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def parse_generation_config(text: str) -> dict[str, float]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for src, dst in KEY_MAP.items():
        f = _num(data.get(src))
        if f is not None:
            out[dst] = f
    return out


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end < 0:
        return "", text
    return text[3:end], text[end + 4:]


def _frontmatter_base_model(fm: str) -> Optional[str]:
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^base_model\s*:\s*(.*)$", line.strip())
        if not m:
            continue
        val = m.group(1).strip().strip("'\"")
        if val.startswith("[") and val.endswith("]"):
            val = val[1:-1].split(",")[0].strip().strip("'\"")
        if not val:
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if s.startswith("- "):
                    val = s[2:].strip().strip("'\"")
                    break
                if s:
                    break
        return val if valid_repo(val) else None
    return None


def parse_card(text: str) -> dict:
    """values (config keys), the heading above the first hit, frontmatter base_model."""
    fm, body = _split_frontmatter(text or "")
    values: dict[str, float] = {}
    first_pos: Optional[int] = None
    for key in KEY_ORDER:
        m = _KEY_RE[key].search(body)
        if not m:
            continue
        values[key] = float(m.group(1))
        if first_pos is None or m.start() < first_pos:
            first_pos = m.start()
    context = None
    if first_pos is not None:
        for line in reversed(body[:first_pos].splitlines()):
            h = _HEADING_RE.match(line.strip())
            if h:
                context = h.group(1).strip()[:120]
                break
    return {"values": values, "context": context, "base_model": _frontmatter_base_model(fm)}


def pick_base_model(card_base: Optional[str], api_base: Any) -> Optional[str]:
    """First valid repo among the card's frontmatter and the API's cardData.base_model."""
    cands = [card_base]
    if isinstance(api_base, str):
        cands.append(api_base)
    elif isinstance(api_base, list):
        cands.extend(x for x in api_base if isinstance(x, str))
    for c in cands:
        if valid_repo(c):
            return c
    return None


class Fetcher:
    """GET against the fixed HF host: no redirects, 10 s timeout, 512 KB cap."""

    def __init__(self, token: str = ""):
        import requests
        self.token = (token or "").strip()
        self.session = requests.Session()
        self.session.trust_env = False

    @property
    def headers(self) -> dict:
        h = {"Accept": "application/json, text/plain, */*", "User-Agent": "llm-systems-manager"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def get(self, url: str) -> tuple[int, str]:
        if not url.startswith(HF_HOST + "/"):
            return 0, ""
        try:
            resp = self.session.get(url, headers=self.headers, timeout=TIMEOUT_S,
                                    allow_redirects=False, stream=True)
            try:
                if resp.status_code != 200:
                    return resp.status_code, ""
                raw = resp.raw.read(BODY_CAP + 1, decode_content=True)
            finally:
                resp.close()
        except Exception:
            return 0, ""
        if len(raw) > BODY_CAP:
            return 0, ""
        return 200, raw.decode("utf-8", "replace")


def _empty_sources() -> dict:
    return {
        "sidecar": {"found": False, "values": {}},
        "model_card": {"found": False, "values": {}, "context": None},
        "base_model": {"found": False, "repo": None, "values": {}, "context": None, "via": {}},
    }


def _listing(repo: str, fetch) -> Optional[dict]:
    status, body = fetch(f"{HF_HOST}/api/models/{repo}")
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _read_repo(repo: str, fetch) -> dict:
    """sidecar values, card parse, and the listing's base_model for one repo."""
    listing = _listing(repo, fetch)
    if listing is None:
        return {"ok": False}
    names = {s.get("rfilename") for s in listing.get("siblings") or [] if isinstance(s, dict)}
    sidecar: Optional[dict] = None
    if "generation_config.json" in names:
        st, body = fetch(f"{HF_HOST}/{repo}/raw/main/generation_config.json")
        if st == 200:
            sidecar = parse_generation_config(body)
    card = None
    if "README.md" in names:
        st, body = fetch(f"{HF_HOST}/{repo}/raw/main/README.md")
        if st == 200:
            card = parse_card(body)
    api_base = (listing.get("cardData") or {}).get("base_model") if isinstance(listing.get("cardData"), dict) else None
    return {"ok": True, "sidecar": sidecar, "card": card, "api_base": api_base,
            "gated": bool(listing.get("gated"))}


def build_meta(repo: str, fetch: Callable[[str], tuple[int, str]]) -> dict:
    doc = {"repo": repo, "base_model": None, "fetched_ts": int(time.time()),
           "error": None, "gated": False, "sources": _empty_sources(), "suggestions": []}
    top = _read_repo(repo, fetch)
    if not top["ok"]:
        doc["error"] = "repo listing unavailable"
        return doc
    doc["gated"] = top["gated"]
    src = doc["sources"]
    if top["sidecar"] is not None:
        src["sidecar"] = {"found": True, "values": top["sidecar"]}
    if top["card"] is not None:
        src["model_card"] = {"found": True, "values": top["card"]["values"], "context": top["card"]["context"]}
    base = pick_base_model((top["card"] or {}).get("base_model"), top["api_base"])
    if base and base != repo:
        doc["base_model"] = base
        low = _read_repo(base, fetch)
        if low["ok"]:
            values: dict[str, float] = {}
            via: dict[str, str] = {}
            for k, v in (low["sidecar"] or {}).items():
                values[k] = v
                via[k] = "sidecar"
            for k, v in ((low["card"] or {}).get("values") or {}).items():
                if k not in values:
                    values[k] = v
                    via[k] = "model_card"
            src["base_model"] = {"found": True, "repo": base, "values": values,
                                 "context": (low["card"] or {}).get("context"), "via": via}
    for key in KEY_ORDER:
        for name in ("sidecar", "model_card", "base_model"):
            vals = src[name]["values"]
            if key in vals:
                doc["suggestions"].append({"key": key, "value": vals[key], "source": name})
                break
    if doc["gated"] and not any(src[name]["found"] for name in ("sidecar", "model_card", "base_model")):
        doc["error"] = "repo is gated; set a Hugging Face token"
    return doc


def ttl_for(doc: dict, ok_ttl_s: int) -> int:
    return ERROR_TTL_S if doc.get("error") else int(ok_ttl_s)


class MetaCache:
    """model_meta rows: repo → JSON document + fetch time."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]):
        self._cf = conn_factory
        self._lock = threading.Lock()

    def init(self) -> None:
        with self._lock:
            conn = self._cf()
            conn.execute("CREATE TABLE IF NOT EXISTS model_meta ("
                         "repo TEXT PRIMARY KEY, fetched_ts INTEGER NOT NULL, json TEXT NOT NULL)")
            conn.commit()

    def get(self, repo: str, ttl_s: int) -> Optional[dict]:
        with self._lock:
            row = self._cf().execute("SELECT fetched_ts, json FROM model_meta WHERE repo = ?", (repo,)).fetchone()
        if not row or int(time.time()) - int(row[0]) >= int(ttl_s):
            return None
        try:
            doc = json.loads(row[1])
        except ValueError:
            return None
        doc["fetched_ts"] = int(row[0])
        return doc

    def put(self, repo: str, doc: dict) -> None:
        ts = int(doc.get("fetched_ts") or time.time())
        with self._lock:
            conn = self._cf()
            conn.execute("INSERT OR REPLACE INTO model_meta (repo, fetched_ts, json) VALUES (?, ?, ?)",
                         (repo, ts, json.dumps(doc)))
            conn.commit()


def _cfg(ctx) -> tuple[str, int]:
    """(hf_token, ttl_s) with getattr guards for older unified_config copies."""
    mm_cfg = getattr(getattr(getattr(ctx, "settings", None), "manager", None), "model_meta", None)
    token = str(getattr(mm_cfg, "hf_token", "") or "")
    try:
        days = int(getattr(mm_cfg, "ttl_days", 7) or 7)
    except (TypeError, ValueError):
        days = 7
    return token, max(1, days) * 86400


def register_routes(app, ctx, *, db_path: str, read_ini: Callable[[], Any],
                    fetcher_factory: Callable[[str], Any] = Fetcher) -> None:
    """Mount GET /api/llm/model-meta on the manager app."""
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

    @app.route("/api/llm/model-meta")
    def llm_model_meta():
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
        refresh = flask_request.args.get("refresh") in ("1", "true", "yes")
        doc = None if refresh else cache.get(repo, ttl_s)
        if doc is not None and int(time.time()) - int(doc["fetched_ts"]) >= ttl_for(doc, ttl_s):
            doc = None
        cached = doc is not None
        if doc is None:
            doc = build_meta(repo, fetcher_factory(token).get)
            cache.put(repo, doc)
        return jsonify({"ok": True, "cached": cached, **doc})
