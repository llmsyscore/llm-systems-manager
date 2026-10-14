"""Agent crash reports: stdlib only, consent from state/telemetry.json, never raises."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone

DEFAULT_ENDPOINT = "https://telemetry-receiver.llmsyscore.workers.dev/v1/event"
CONSENT = "telemetry.json"
CRASHES = "telemetry-crashes.json"
WINDOW_S = 24 * 3600
DAILY_CAP = 5
FRAME_LIMIT = 20
_started = time.time()
_lock = threading.RLock()


def _iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path, obj):
    d, name = os.path.split(path)
    tmp = os.path.join(d, f".{name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with _lock:
        try:
            fd = os.open(tmp, flags, 0o600)
        except FileExistsError:
            os.unlink(tmp)
            fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)


def load_consent(state_dir):
    try:
        with open(os.path.join(state_dir, CONSENT)) as fh:
            d = json.load(fh)
        d.setdefault("crash_reports", False)
        return d
    except Exception:
        return {"crash_reports": False}


def write_consent(state_dir, *, install_report, crash_reports, install_id, installed_at):
    os.makedirs(state_dir, exist_ok=True)
    _write_json(os.path.join(state_dir, CONSENT),
                {"install_report": bool(install_report), "crash_reports": bool(crash_reports),
                 "install_id": install_id, "installed_at": installed_at})


def frames_text(tb, root, limit=FRAME_LIMIT):
    root = os.path.realpath(root)
    out = []
    for fr in traceback.extract_tb(tb):
        fn = os.path.realpath(fr.filename) if fr.filename else ""
        if not fn.startswith(root + os.sep):
            continue
        rel = fn[len(root) + 1:]
        if any(p in ("venv", "site-packages") for p in rel.split(os.sep)[:-1]):
            continue
        out.append(f"{rel}:{fr.lineno}:{fr.name}")
    return ";".join(out[-limit:])[:2048]


def crash_hash(exc_type, frames):
    return hashlib.sha256(f"{exc_type}\n{frames}".encode()).hexdigest()[:16]


def allow_crash(state_dir, h, now=None):
    with _lock:
        return _allow_crash(state_dir, h, now)


def _allow_crash(state_dir, h, now):
    now = time.time() if now is None else now
    path = os.path.join(state_dir, CRASHES)
    try:
        with open(path) as fh:
            st = json.load(fh)
    except Exception:
        st = {}
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    if st.get("day") != day:
        st = {"day": day, "n": 0, "hashes": st.get("hashes", {})}
    last = st["hashes"].get(h)
    if last is not None and now - float(last) < WINDOW_S:
        return False
    if int(st.get("n", 0)) >= DAILY_CAP:
        return False
    st["hashes"][h] = now
    st["n"] = int(st.get("n", 0)) + 1
    try:
        os.makedirs(state_dir, exist_ok=True)
        _write_json(path, st)
    except Exception:
        pass  # stamp write is best effort
    return True


def build_crash_payload(exc, tb, *, version, root, state_dir, uptime_s):
    c = load_consent(state_dir)
    exc_type = type(exc).__name__[:128]
    frames = frames_text(tb, root)
    p = {"schema": 1, "event": "crash", "install_id": c.get("install_id"),
            "installed_at": c.get("installed_at"), "sent_at": _iso(), "version": version[:128],
            "channel": "agent", "component": "agent", "exc_type": exc_type,
            "frames": frames or None, "crash_hash": crash_hash(exc_type, frames),
            "uptime_s": int(uptime_s)}
    return {k: v for k, v in p.items() if v is not None}


def send(payload, endpoint, timeout=5.0):
    if not str(endpoint).startswith("https://"):
        return False
    try:
        body = json.dumps(payload, separators=(",", ":")).encode()
        req = urllib.request.Request(endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        opener = urllib.request.OpenerDirector()
        opener.add_handler(urllib.request.ProxyHandler({}))
        opener.add_handler(urllib.request.HTTPSHandler())
        with opener.open(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def report_exception(exc, tb, *, state_dir, version, root):
    try:
        c = load_consent(state_dir)
        if not c.get("crash_reports") or not c.get("install_id"):
            return
        p = build_crash_payload(exc, tb, version=version, root=root, state_dir=state_dir,
                                uptime_s=int(time.time() - _started))
        if not allow_crash(state_dir, p["crash_hash"]):
            return
        t = threading.Thread(target=send, args=(p, os.environ.get("LLMSYS_TELEMETRY_ENDPOINT") or DEFAULT_ENDPOINT),
                             daemon=True)
        t.start()
        t.join(2.0)
    except Exception:
        pass  # crash reporting never raises


def install_hooks(*, state_dir, version, root):
    prev_sys, prev_thr = sys.excepthook, threading.excepthook

    def _sys_hook(exc_type, exc, tb):
        report_exception(exc, tb, state_dir=state_dir, version=version, root=root)
        prev_sys(exc_type, exc, tb)

    def _thr_hook(args):
        report_exception(args.exc_value, args.exc_traceback, state_dir=state_dir, version=version, root=root)
        prev_thr(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thr_hook
