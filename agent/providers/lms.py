"""LM Studio provider — 10 routes + the three public lms_get_* helpers."""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import Header, HTTPException, Request

from . import _shared

# PR2: minimal spec the agent's heartbeat body emits — see providers/llama.py.
PROVIDER_SPEC = {
    "name": "lms",
    "capability_key": "lms",
    "push_endpoint": "/api/remote/provider-state",
}

log = logging.getLogger("llm-systems-agent.providers.lms")

_ctx = None

_lms_session: Optional[requests.Session] = None
_lms_session_lock = threading.Lock()

_LMS_CLI_TIMEOUT_S = int(os.environ.get("LSA_LMS_CLI_TIMEOUT_S", "15"))
_LMS_TIMEOUT_LOG_BURST = 12
_LMS_LOAD_TIMEOUT_DEFAULT_S = 180
_LMS_UNLOAD_TIMEOUT_DEFAULT_S = 60
_lms_counter_lock = threading.Lock()
_lms_ps_timeout_count = 0
_lms_status_timeout_count = 0

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._@/:\-]{1,200}$")
_LMS_TIMESTAMP_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2}')
_LMS_LOG_IGNORE = (
    "[Client=lms-cli][Endpoint=listLoaded]",
    "[Client=lms-cli][Endpoint=getLoadConfig]",
    "[Client=lms-cli][Endpoint=getModelInfo]",
    "Listing loaded models",
    "[INFO] Returning",
)


def set_context(ctx) -> None:
    global _ctx
    _ctx = ctx


def _require_ctx():
    if _ctx is None:
        raise RuntimeError("providers.lms.set_context() not called")
    return _ctx


def _get_session() -> requests.Session:
    # Double-checked init so non-LMS hosts don't carry a Session.
    global _lms_session
    if _lms_session is None:
        with _lms_session_lock:
            if _lms_session is None:
                _lms_session = requests.Session()
    return _lms_session


# ── Public collectors (called from _build_metric_sample in main) ────────

def lms_get_models() -> list[dict[str, Any]]:
    ctx = _require_ctx()
    try:
        r = _get_session().get(f"{ctx.config.LMS_API_URL}/v1/models", timeout=3)
        if r.ok:
            return r.json().get("data", []) or []
    except Exception as e:
        log.debug("LMS /v1/models unreachable: %s", e)
    return []


def _fmt_size_bytes(n: Any) -> str:
    """Human size string from a byte count ("3.94 GB"); "" when absent."""
    if not isinstance(n, (int, float)) or n <= 0:
        return ""
    if n >= 1e9:
        return f"{n / 1e9:.2f} GB"
    return f"{n / 1e6:.0f} MB"


def _quant_name(q: Any) -> str:
    if isinstance(q, dict):
        return str(q.get("name") or "")
    return str(q or "")


def lms_get_ps() -> "list[dict[str, Any]] | None":
    """Loaded-instance rows from `lms ps --json`; None when the read failed."""
    return _lms_read_ps()[0]


def _lms_read_ps() -> "tuple[list[dict[str, Any]] | None, str | None]":
    """(rows, None) on a good read; (None, reason) when `lms ps --json` failed."""
    global _lms_ps_timeout_count
    ctx = _require_ctx()
    if not os.path.exists(ctx.config.LMS_CMD):
        return None, "LMS_CMD not found"
    try:
        out = subprocess.check_output(
            [ctx.config.LMS_CMD, "ps", "--json"],
            text=True, timeout=_LMS_CLI_TIMEOUT_S, stderr=subprocess.DEVNULL,
        )
        with _lms_counter_lock:
            _lms_ps_timeout_count = 0
        data = json.loads(out.strip())
        if isinstance(data, list):
            return [
                {
                    "identifier": item.get("identifier", ""),
                    "model": item.get("model") or item.get("modelKey") or item.get("identifier") or "",
                    "status": str(item.get("status", "IDLE")).upper(),
                    "size": item.get("size") or _fmt_size_bytes(item.get("sizeBytes")),
                    "context": item.get("context", item.get("contextLength")),
                    "parallel": item.get("parallel"),
                    "device": item.get("device") or item.get("deviceIdentifier") or "",
                    "quant": _quant_name(item.get("quantization")),
                    "params": item.get("paramsString") or "",
                }
                for item in data
            ], None
        return None, "lms ps returned a non-list payload"
    except subprocess.TimeoutExpired:
        with _lms_counter_lock:
            _lms_ps_timeout_count += 1
            cur = _lms_ps_timeout_count
        if cur == 1 or cur % _LMS_TIMEOUT_LOG_BURST == 0:
            log.warning(
                "lms ps --json timed out after %ss (%d cycles)",
                _LMS_CLI_TIMEOUT_S, cur,
            )
        return None, f"lms ps timed out after {_LMS_CLI_TIMEOUT_S}s"
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        log.debug("lms ps --json parse failure: %s", e)
        return None, f"lms ps parse failure: {e}"
    except Exception as e:
        log.warning("lms ps --json failed: %s", e, exc_info=True)
        return None, f"lms ps failed: {e}"


def lms_get_status() -> dict[str, Any]:
    """{on: bool|None, port, raw, error?}; on=None means the read failed."""
    global _lms_status_timeout_count
    ctx = _require_ctx()
    if not os.path.exists(ctx.config.LMS_CMD):
        return {"on": None, "port": None, "raw": "", "error": "LMS_CMD not found"}
    try:
        out = subprocess.check_output(
            [ctx.config.LMS_CMD, "server", "status", "--json"],
            text=True, timeout=_LMS_CLI_TIMEOUT_S, stderr=subprocess.DEVNULL,
        )
        with _lms_counter_lock:
            _lms_status_timeout_count = 0
        data = json.loads(out.strip())
        on = data.get("running", data.get("on", data.get("status") == "running"))
        port = int(data.get("port", 1235))
        return {"on": bool(on), "port": port, "raw": out.strip()}
    except subprocess.TimeoutExpired:
        with _lms_counter_lock:
            _lms_status_timeout_count += 1
            cur = _lms_status_timeout_count
        if cur == 1 or cur % _LMS_TIMEOUT_LOG_BURST == 0:
            log.warning(
                "lms server status --json timed out after %ss (%d cycles)",
                _LMS_CLI_TIMEOUT_S, cur,
            )
        return {"on": None, "port": None, "raw": "",
                "error": f"lms server status timed out after {_LMS_CLI_TIMEOUT_S}s"}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as e:
        log.debug("lms server status --json parse failure: %s", e)
        err = f"lms server status parse failure: {e}"
    except Exception as e:
        log.warning("lms server status --json failed: %s", e, exc_info=True)
        err = f"lms server status failed: {e}"
    return {"on": None, "port": None, "raw": "", "error": err}


def lms_sample_block() -> dict[str, Any]:
    """The `lms` block of a metric sample; ps_ok/ps_error say whether `ps` was read."""
    server = lms_get_status()
    models = lms_get_models()
    ps, err = _lms_read_ps()
    return {"server": server, "models": models, "ps": ps or [],
            "ps_ok": ps is not None, "ps_error": err}


# ── Private helpers ────────────────────────────────────────────────────

def _cfg_timeout(name: str, default: int) -> int:
    """Integer seconds from ctx.config.<name>; `default` when unset or not a positive number."""
    try:
        val = int(getattr(_require_ctx().config, name, default))
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _lms_resident_instances(model_id: str) -> "list[str] | None":
    """Instance ids of `model_id` loaded per /api/v1/models; None when unreadable."""
    ctx = _require_ctx()
    try:
        r = _get_session().get(f"{ctx.config.LMS_API_URL.rstrip('/')}/api/v1/models",
                               timeout=5)
        if not r.ok:
            return None
        models = (r.json() or {}).get("models") or []
    except Exception as e:
        log.debug("lms /api/v1/models unreadable: %s", e)
        return None
    out: list[str] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        for inst in m.get("loaded_instances") or []:
            iid = (inst or {}).get("id") if isinstance(inst, dict) else None
            if m.get("key") == model_id or iid == model_id:
                out.append(str(iid or m.get("key")))
    return out


def _valid_model_id(s: Any) -> bool:
    return isinstance(s, str) and bool(_MODEL_ID_RE.match(s))


def _filter_lms_log(lines: list[str]) -> list[str]:
    """Drop ignore-pattern lines and their multi-line JSON continuations via brace tracking."""
    out: list[str] = []
    in_block = False
    depth = 0
    for line in lines:
        if not _LMS_TIMESTAMP_RE.match(line):
            if in_block:
                depth += line.count('{') - line.count('}')
                if depth <= 0:
                    in_block = False
            continue
        in_block = False
        depth = 0
        if any(p in line for p in _LMS_LOG_IGNORE):
            depth = line.count('{') - line.count('}')
            if depth > 0:
                in_block = True
            continue
        out.append(line)
    return out


def _lms_check_enabled() -> None:
    if not _require_ctx().config.LMS_ENABLED:
        raise HTTPException(status_code=503, detail="LMS not enabled on this agent")


def _lms_run_cli(args: list[str], timeout: int = 20) -> "tuple[int, str]":
    """Run `lms <args>`; returns (rc, combined_output). Never raises."""
    ctx = _require_ctx()
    if not ctx.config.LMS_CMD:
        return 1, "LMS_CMD not configured"
    if not os.path.isfile(ctx.config.LMS_CMD):
        return 1, f"LMS_CMD not found at {ctx.config.LMS_CMD}"
    try:
        r = subprocess.run(
            [ctx.config.LMS_CMD] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


# ── Route handlers (module top-level so __qualname__ is stable) ────────

def lms_status_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    _lms_check_enabled()
    s = lms_get_status()
    # Wrapper preserves legacy {ok, output, data} shape for the dashboard.
    return {
        "ok": True,
        "output": s.get("raw") or "",
        "data": {"running": bool(s.get("on")), "port": s.get("port")},
        "on": s.get("on"),
        "port": s.get("port"),
        "raw": s.get("raw"),
        "error": s.get("error"),
    }


def lms_models_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    _lms_check_enabled()
    # Wrap in {data: [...]} for legacy /api/lmstudio/models compatibility.
    return {"data": lms_get_models()}


def lms_ps_endpoint(authorization: Optional[str] = Header(default=None)) -> list[dict[str, Any]]:
    _require_ctx().check_bearer(authorization)
    _lms_check_enabled()
    return lms_get_ps() or []


async def _lms_openai_forward(sub: str, request: Request,
                              authorization: "Optional[str]"):
    """Narrow OpenAI passthrough to LM Studio /v1/<sub> (#493)."""
    ctx = _require_ctx()
    ctx.check_bearer(authorization)
    _lms_check_enabled()
    return await _shared.openai_forward(sub, request, ctx.config.LMS_API_URL)


async def lms_openai_chat(request: Request,
                          authorization: Optional[str] = Header(default=None)):
    return await _lms_openai_forward("chat/completions", request, authorization)


async def lms_openai_completions(request: Request,
                                 authorization: Optional[str] = Header(default=None)):
    return await _lms_openai_forward("completions", request, authorization)


def lms_server_start_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _lms_check_enabled()
    rc, out = _lms_run_cli(["server", "start"], timeout=20)
    log.info("lms server start: rc=%s %s", rc, out[:200])
    return {"ok": rc == 0, "output": out}


def lms_server_stop_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _lms_check_enabled()
    rc, out = _lms_run_cli(["server", "stop"], timeout=20)
    log.info("lms server stop: rc=%s %s", rc, out[:200])
    return {"ok": rc == 0, "output": out}


def lms_server_restart_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _lms_check_enabled()
    rc1, out1 = _lms_run_cli(["server", "stop"], timeout=20)
    time.sleep(2)
    rc2, out2 = _lms_run_cli(["server", "start"], timeout=20)
    combined = (out1 + "\n" + out2).strip()
    log.info("lms server restart: stop rc=%s start rc=%s", rc1, rc2)
    return {"ok": (rc2 == 0), "output": combined}


def lms_server_log_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Last ~300 filtered lines from ~/.lmstudio/server-logs/YYYY-MM/*.log."""
    ctx = _require_ctx()
    ctx.check_bearer(authorization); _lms_check_enabled()
    home = os.path.expanduser("~")
    if ctx.config.AGENT_USER:
        try:
            home = pwd.getpwnam(ctx.config.AGENT_USER).pw_dir
        except KeyError:
            pass
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    log_dir = Path(home) / ".lmstudio" / "server-logs" / month
    if not log_dir.is_dir():
        return {"ok": True, "lines": [], "note": f"no log dir at {log_dir}"}
    log_files = sorted(log_dir.glob("*.log"))
    if not log_files:
        return {"ok": True, "lines": [], "note": f"no .log files in {log_dir}"}
    target = log_files[-1]
    try:
        TAIL_BYTES = 256 * 1024
        size = target.stat().st_size
        offset = max(0, size - TAIL_BYTES)
        with target.open("rb") as f:
            if offset:
                f.seek(offset)
                f.readline()
            data = f.read()
        raw = [line.rstrip() for line in data.decode("utf-8", errors="replace").splitlines()][-300:]
        filtered = _filter_lms_log(raw)
        if not filtered and raw:
            fallback = [l for l in raw if _LMS_TIMESTAMP_RE.match(l)][-15:]
            if fallback:
                filtered = ["# (idle — showing last unfiltered lines)"] + fallback
        return {"ok": True, "lines": filtered, "source": str(target)}
    except Exception as e:
        return {"ok": False, "lines": [], "error": str(e)}


def lms_load_endpoint(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    ctx = _require_ctx()
    ctx.check_bearer(authorization); _lms_check_enabled()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="model required")
    if not _valid_model_id(model_id):
        raise HTTPException(status_code=400, detail="invalid model id")
    resident = _lms_resident_instances(model_id)
    if resident:
        log.info("lms load %s: already resident as %s", model_id, resident)
        return {"ok": True, "already_loaded": True, "instances": resident}
    timeout = _cfg_timeout("LMS_LOAD_TIMEOUT_S", _LMS_LOAD_TIMEOUT_DEFAULT_S)
    try:
        resp = _get_session().post(
            f"{ctx.config.LMS_API_URL.rstrip('/')}/api/v1/models/load",
            json={"model": model_id}, timeout=timeout,
        )
    except requests.exceptions.Timeout:
        log.warning("lms load %s: timed out after %ss", model_id, timeout)
        return {"ok": False, "timeout": True,
                "error": f"lms load timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    log.info("lms load %s: %s", model_id, resp.status_code)
    try:
        body_resp = resp.json()
    except Exception:
        body_resp = {"raw": resp.text[:500]}
    if not resp.ok:
        return {"ok": False, "response": body_resp}
    resident = _lms_resident_instances(model_id)
    if resident is None:
        return {"ok": True, "already_loaded": False, "verified": False,
                "response": body_resp}
    if not resident:
        return {"ok": False, "already_loaded": False, "verified": True,
                "error": "lms load returned ok but the model is not resident",
                "response": body_resp}
    return {"ok": True, "already_loaded": False, "verified": True,
            "instances": resident, "response": body_resp}


def lms_download_endpoint(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Forward a model-download request to the local LMS API."""
    ctx = _require_ctx()
    ctx.check_bearer(authorization); _lms_check_enabled()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="model required")
    payload = {"model": model_id}
    # Quant pin for Hugging Face URLs; LMS ignores it for catalog names.
    if body.get("quantization"):
        payload["quantization"] = str(body["quantization"])
    try:
        resp = _get_session().post(
            f"{ctx.config.LMS_API_URL.rstrip('/')}/api/v1/models/download",
            json=payload, timeout=60,
        )
        log.info("lms download %s: %s", model_id, resp.status_code)
        try:
            body_resp = resp.json()
        except Exception:
            body_resp = {"raw": resp.text[:500]}
        return {"ok": resp.ok, "response": body_resp}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _lms_home() -> Path:
    ctx = _require_ctx()
    if ctx.config.AGENT_USER:
        try:
            return Path(pwd.getpwnam(ctx.config.AGENT_USER).pw_dir)
        except KeyError:
            log.debug("lms home: AGENT_USER %r not in passwd db",
                      ctx.config.AGENT_USER)
    return Path(os.path.expanduser("~"))


def _lms_models_root() -> Path:
    """LM Studio's models dir: the .internal pointer file, else the default."""
    home = _lms_home()
    ptr = home / ".lmstudio" / ".internal" / "user-concrete-model-default-directory"
    try:
        text = ptr.read_text().strip()
    except OSError:
        text = ""
    return Path(text) if text else home / ".lmstudio" / "models"


_LMS_SHARD_RE = re.compile(r"-\d{5}-of-\d{5}(?=\.gguf$)", re.IGNORECASE)


def lms_delete_endpoint(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Delete a downloaded model's files; LMS has no CLI/REST delete (#492)."""
    ctx = _require_ctx()
    ctx.check_bearer(authorization); _lms_check_enabled()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="model required")
    if not _valid_model_id(model_id):
        raise HTTPException(status_code=400, detail="invalid model id")
    base_key = model_id.partition("@")[0]
    ps = lms_get_ps()
    if ps is None:
        return {"ok": False, "error": f"cannot verify {model_id} is unloaded: lms ps unreadable"}
    for p in ps:
        for pk in (p.get("identifier"), p.get("model")):
            if pk and (model_id == pk or base_key == str(pk).partition("@")[0]):
                return {"ok": False,
                        "error": f"{model_id} is loaded — unload it first"}
    try:
        out = subprocess.check_output(
            [ctx.config.LMS_CMD, "ls", "--json"],
            text=True, timeout=_LMS_CLI_TIMEOUT_S, stderr=subprocess.DEVNULL)
        entries = json.loads(out.strip())
    except Exception as e:
        return {"ok": False, "error": f"lms ls failed: {e}"}
    entries = [e for e in entries if isinstance(e, dict) and e.get("path")]
    entry = next((e for e in entries if e.get("modelKey") == model_id), None)
    if not entry:
        # The CLI catalog may key without the @quant suffix; accept a base
        # match only when it is unambiguous.
        base_hits = [e for e in entries
                     if str(e.get("modelKey") or "").partition("@")[0] == base_key]
        entry = base_hits[0] if len(base_hits) == 1 else None
    if not entry:
        return {"ok": False, "error": f"{model_id} is not in the local catalog"}
    root = _lms_models_root().resolve()
    target = (root / entry["path"]).resolve()
    if root not in target.parents:
        return {"ok": False, "error": "resolved path escapes the models dir"}
    if not target.is_file():
        return {"ok": False, "error": f"model file not found: {entry['path']}"}
    # Sharded GGUFs list shard 1; delete every sibling shard of the same stem.
    victims = [target]
    if _LMS_SHARD_RE.search(target.name):
        stem = _LMS_SHARD_RE.sub("", target.name)[:-5]
        victims = sorted(p for p in target.parent.iterdir()
                         if p.is_file() and _LMS_SHARD_RE.search(p.name)
                         and _LMS_SHARD_RE.sub("", p.name)[:-5] == stem)
    deleted, freed = [], 0
    try:
        for v in victims:
            freed += v.stat().st_size
            v.unlink()
            deleted.append(v.name)
        # Prune now-empty model/publisher dirs, never past the models root.
        d = target.parent
        while d != root and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
    except OSError as e:
        return {"ok": False, "error": f"delete failed: {e}",
                "deleted_files": deleted}
    log.info("lms delete %s: removed %d file(s), %d bytes",
             model_id, len(deleted), freed)
    return {"ok": True, "deleted_files": deleted, "freed_bytes": freed}


def lms_unload_endpoint(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    ctx = _require_ctx()
    ctx.check_bearer(authorization); _lms_check_enabled()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="model required")
    if not _valid_model_id(model_id):
        raise HTTPException(status_code=400, detail="invalid model id")
    try:
        # LMS unload requires instance_id, not model.
        resp = _get_session().post(
            f"{ctx.config.LMS_API_URL.rstrip('/')}/api/v1/models/unload",
            json={"instance_id": model_id},
            timeout=_cfg_timeout("LMS_UNLOAD_TIMEOUT_S", _LMS_UNLOAD_TIMEOUT_DEFAULT_S),
        )
        log.info("lms unload %s: %s", model_id, resp.status_code)
        if resp.ok:
            try:
                return {"ok": True, "response": resp.json()}
            except Exception:
                return {"ok": True, "response": {"raw": resp.text[:500]}}
        # CLI fallback — sometimes succeeds when HTTP doesn't (lock-file issues).
        log.warning("lms HTTP unload failed (%s), trying CLI", resp.status_code)
        rc, out = _lms_run_cli(["unload", model_id], timeout=30)
        return {"ok": rc == 0, "output": out, "http_status": resp.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Route registration ────────────────────────────────────────────────

_ROUTES: tuple = (
    # (method, path, handler)
    ("GET",  "/lms/server/status",  lms_status_endpoint),
    ("GET",  "/lms/models",         lms_models_endpoint),
    ("GET",  "/lms/ps",             lms_ps_endpoint),
    ("POST", "/lms/server/start",   lms_server_start_endpoint),
    ("POST", "/lms/server/stop",    lms_server_stop_endpoint),
    ("POST", "/lms/server/restart", lms_server_restart_endpoint),
    ("GET",  "/lms/server/log",     lms_server_log_endpoint),
    ("POST", "/lms/load",           lms_load_endpoint),
    ("POST", "/lms/download",       lms_download_endpoint),
    ("POST", "/lms/unload",         lms_unload_endpoint),
    ("POST", "/lms/delete",         lms_delete_endpoint),
    ("POST", "/lms/openai/chat/completions", lms_openai_chat),
    ("POST", "/lms/openai/completions",      lms_openai_completions),
)


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
