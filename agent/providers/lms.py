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
from fastapi import Header, HTTPException

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


def lms_get_ps() -> list[dict[str, Any]]:
    global _lms_ps_timeout_count
    ctx = _require_ctx()
    if not os.path.exists(ctx.config.LMS_CMD):
        return []
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
                    "model": item.get("model", item.get("identifier", "")),
                    "status": str(item.get("status", "IDLE")).upper(),
                    "size": item.get("size", ""),
                    "context": item.get("context", item.get("contextLength")),
                    "parallel": item.get("parallel"),
                    "device": item.get("device", ""),
                }
                for item in data
            ]
    except subprocess.TimeoutExpired:
        with _lms_counter_lock:
            _lms_ps_timeout_count += 1
            cur = _lms_ps_timeout_count
        if cur == 1 or cur % _LMS_TIMEOUT_LOG_BURST == 0:
            log.warning(
                "lms ps --json timed out after %ss (%d cycles)",
                _LMS_CLI_TIMEOUT_S, cur,
            )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.debug("lms ps --json parse fallback: %s", e)
    except Exception as e:
        log.warning("lms ps --json failed: %s", e, exc_info=True)
    return []


def lms_get_status() -> dict[str, Any]:
    global _lms_status_timeout_count
    ctx = _require_ctx()
    if not os.path.exists(ctx.config.LMS_CMD):
        return {"on": False, "port": None, "raw": "", "error": "LMS_CMD not found"}
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
        return {"on": False, "port": None, "raw": "",
                "error": f"lms server status timed out after {_LMS_CLI_TIMEOUT_S}s"}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        log.debug("lms server status --json parse fallback: %s", e)
    except Exception as e:
        log.warning("lms server status --json failed: %s", e, exc_info=True)
    return {"on": False, "port": None, "raw": ""}


# ── Private helpers ────────────────────────────────────────────────────

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
    return lms_get_ps()


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
    try:
        resp = _get_session().post(
            f"{ctx.config.LMS_API_URL.rstrip('/')}/api/v1/models/load",
            json={"model": model_id}, timeout=30,
        )
        log.info("lms load %s: %s", model_id, resp.status_code)
        try:
            body_resp = resp.json()
        except Exception:
            body_resp = {"raw": resp.text[:500]}
        return {"ok": resp.ok, "response": body_resp}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def lms_download_endpoint(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Forward a model-download request to the local LMS API."""
    ctx = _require_ctx()
    ctx.check_bearer(authorization); _lms_check_enabled()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="model required")
    try:
        resp = _get_session().post(
            f"{ctx.config.LMS_API_URL.rstrip('/')}/api/v1/models/download",
            json={"model": model_id}, timeout=60,
        )
        log.info("lms download %s: %s", model_id, resp.status_code)
        try:
            body_resp = resp.json()
        except Exception:
            body_resp = {"raw": resp.text[:500]}
        return {"ok": resp.ok, "response": body_resp}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
            json={"instance_id": model_id}, timeout=15,
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
)


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
