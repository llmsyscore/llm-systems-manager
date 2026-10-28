"""Anonymous install, crash and diagnostic reports: payload, files, send, hooks.
Also a CLI (payload / send / facts) so the shell installers reuse the builder."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA = 1
EVENTS = ("install", "update", "heartbeat", "checkin", "crash")
PENDING = "telemetry-pending.json"
LAST = "telemetry-last.json"
CRASHES = "telemetry-crashes.json"
LOG = "telemetry.log"
IDFILE = "install-id"
TIMEOUT_S = 5.0
CRASH_WINDOW_S = 24 * 3600
CRASH_DAILY_CAP = 5
FRAME_LIMIT = 20
FRAMES_MAX_CHARS = 2048
HEARTBEAT_EVERY_S = 24 * 3600
HEARTBEAT_STAMP = "telemetry-heartbeat"
FIRST_DELAY_S = 300
TICK_S = 60
HEARTBEAT_RETRY_S = 3600
HEARTBEAT_SPREAD_S = 1800

_settings: Any = None
_data_dir: Optional[Path] = None
_version = ""
_root: Optional[Path] = None
_agent_count_fn: Optional[Callable[[], int]] = None
_provider_mix_fn: Optional[Callable[[], dict]] = None
_release_fn: Optional[Callable[[], Optional[str]]] = None
_manager_version_fn: Optional[Callable[[], Optional[str]]] = None
_ae_version_fn: Optional[Callable[[], Optional[str]]] = None
_agent_versions_fn: Optional[Callable[[], Optional[dict]]] = None
_features_fn: Optional[Callable[[], Optional[dict]]] = None
_lock = threading.RLock()
_started_at = time.time()
_log = None


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def configure(*, settings, data_dir: Path, version: str, root: Path,
              agent_count_fn=None, provider_mix_fn=None, log=None, release_fn=None,
              manager_version_fn=None, alarm_engine_version_fn=None, agent_versions_fn=None,
              features_fn=None) -> None:
    global _settings, _data_dir, _version, _root, _agent_count_fn, _provider_mix_fn, _log
    global _release_fn, _manager_version_fn, _ae_version_fn, _agent_versions_fn, _features_fn
    _settings, _data_dir, _version, _root = settings, Path(data_dir), version, Path(root)
    _agent_count_fn, _provider_mix_fn, _log = agent_count_fn, provider_mix_fn, log
    _release_fn, _manager_version_fn = release_fn, manager_version_fn
    _ae_version_fn, _agent_versions_fn = alarm_engine_version_fn, agent_versions_fn
    _features_fn = features_fn


def _snapshot_settings():
    # Saved-but-unapplied TOML edits win; falls back to the boot-time object.
    try:
        import settings_catalog
        return settings_catalog._snapshot()
    except Exception:
        return _settings


def _cfg():
    s = _snapshot_settings()
    return getattr(getattr(s, "manager", None), "telemetry", None)


def do_not_track() -> bool:
    """True when DO_NOT_TRACK is set to anything other than empty, 0 or false."""
    return os.environ.get("DO_NOT_TRACK", "").strip().lower() not in ("", "0", "false")


def enabled(kind: str) -> bool:
    if do_not_track():
        return False
    c = _cfg()
    default = kind not in ("heartbeat", "checkin")
    return bool(getattr(c, kind, default)) if c is not None else default


DEFAULT_ENDPOINT = "https://llmsyscore-reports.llmsyscore.workers.dev/v1/event"


def endpoint() -> str:
    return os.environ.get("LLMSYS_TELEMETRY_ENDPOINT") or DEFAULT_ENDPOINT


def reload() -> None:
    pass  # settings are re-read on every call; kept for the hot-reload registry


# ── identity and host facts ──────────────────────────────────────────────────

def _write_private(tmp: Path, text: str) -> None:
    # Exclusive no-follow create of a 0600 temp file, then write.
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass  # nothing to remove
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def install_identity(data_dir: Path) -> tuple[str, str]:
    data_dir = Path(data_dir)
    f = data_dir / IDFILE
    if f.is_symlink() or not f.is_file() or not f.read_text().strip():
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        _write_private(tmp, str(uuid.uuid4()) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, f)
    return f.read_text().strip(), _iso(f.stat().st_mtime)


def _os_release() -> tuple[str, str]:
    if sys.platform == "darwin":
        try:
            ver = subprocess.run(["sw_vers", "-productVersion"], capture_output=True,
                                 text=True, timeout=3).stdout.strip()
        except Exception:
            ver = ""
        return "macos", ver
    kv = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip().strip('"')
    except Exception:
        pass  # falls back to platform.system()
    return (kv.get("ID") or platform.system().lower() or "unknown"), kv.get("VERSION_ID", "")


def _cpu_model() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                  text=True, timeout=3).stdout.strip()[:128]
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()[:128]
    except Exception:
        pass  # falls back to platform.processor()
    return platform.processor()[:128] or "unknown"


def _ram_gb() -> Optional[int]:
    try:
        if sys.platform == "darwin":
            b = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                   text=True, timeout=3).stdout.strip())
            return round(b / 2**30)
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 2**20)
    except Exception:
        pass  # memory size unknown
    return None


def _gpu() -> tuple[str, int]:
    if sys.platform == "darwin":
        return "apple", 1
    vendors = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
    found: dict[str, int] = {}
    for p in Path("/sys/class/drm").glob("card[0-9]*/device/vendor"):
        try:
            v = vendors.get(p.read_text().strip().lower())
        except Exception:
            v = None
        if v:
            found[v] = found.get(v, 0) + 1
    if not found and shutil.which("nvidia-smi"):
        try:
            n = len([l for l in subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True,
                                               timeout=5).stdout.splitlines() if l.startswith("GPU")])
            if n:
                found["nvidia"] = n
        except Exception:
            pass  # nvidia-smi unavailable
    if not found:
        return "none", 0
    vendor = max(found, key=found.get)
    return vendor, found[vendor]


def host_facts() -> dict:
    os_id, os_ver = _os_release()
    vendor, count = _gpu()
    return {"os": os_id, "os_version": os_ver, "arch": platform.machine(),
            "cpu_model": _cpu_model(), "ram_gb": _ram_gb(),
            "gpu_vendor": vendor, "gpu_count": count}


_channel_cache: dict = {}


def detect_channel(root: Path) -> str:
    key = str(Path(root).resolve())
    if key not in _channel_cache:
        _channel_cache[key] = _detect_channel(Path(root))
    return _channel_cache[key]


def _detect_channel(root: Path) -> str:
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return "docker"
    s = str(root)
    if "/Cellar/" in s or s.startswith("/opt/homebrew") or "/homebrew/" in s.lower():
        return "brew"
    if shutil.which("dpkg-query"):
        try:
            if subprocess.run(["dpkg-query", "-S", s], capture_output=True, timeout=5).returncode == 0:
                return "deb"
        except Exception:
            pass  # dpkg-query failed
    if shutil.which("rpm"):
        try:
            if subprocess.run(["rpm", "-qf", s], capture_output=True, timeout=5).returncode == 0:
                return "rpm"
        except Exception:
            pass  # rpm failed
    return "script"


# ── payloads ─────────────────────────────────────────────────────────────────

_KEYS = ("schema", "event", "install_id", "installed_at", "sent_at", "version", "release", "manager_version",
         "alarm_engine_version", "agent_versions", "channel", "mode", "source", "features",
         "os", "os_version", "arch", "cpu_model", "ram_gb", "gpu_vendor", "gpu_count", "provider",
         "outcome", "agent_count", "provider_mix", "uptime_days", "component", "exc_type", "frames",
         "crash_hash", "uptime_s")


def _blank(event: str, data_dir: Path, version: str, channel: str) -> dict:
    iid, at = install_identity(data_dir)
    p = {k: None for k in _KEYS}
    p.update(schema=SCHEMA, event=event, install_id=iid, installed_at=at, sent_at=_iso(),
             version=(version or "")[:128], channel=channel)
    return p


def _present(p: dict) -> dict:
    return {k: p[k] for k in _KEYS if p.get(k) is not None}


def _mix_text(mix: Optional[dict]) -> Optional[str]:
    if not mix:
        return None
    return ",".join(f"{k}={int(v)}" for k, v in sorted(mix.items()))[:128]


def _call_str(fn) -> Optional[str]:
    try:
        v = fn() if fn else None
    except Exception:
        return None
    return str(v)[:128] if v else None


SOURCES = ("release", "git", "local")


def source_of_install(source: Optional[str]) -> Optional[str]:
    """Maps companion._installed_release()["source"] to release | git, else None."""
    if source in ("release-file", "dpkg", "rpm"):
        return "release"
    return "git" if source == "git" else None


def build_payload(event: str, *, data_dir: Path, version: str, channel: str, mode: str = "",
                  outcome: str = "", source: str = "", extra: Optional[dict] = None) -> dict:
    if event not in EVENTS:
        raise ValueError(event)
    p = _blank(event, data_dir, version, channel)
    if event in ("install", "update", "heartbeat"):
        p["mode"] = (mode or None)
    if event == "checkin":
        os_id, os_ver = _os_release()
        p.update(os=os_id, os_version=os_ver or None)
        p["release"] = _call_str(_release_fn)
    elif event in ("install", "update"):
        p.update(host_facts())
        p["outcome"] = (outcome or "ok")[:128]
        p["release"] = _call_str(_release_fn)
        p["source"] = source if source in SOURCES else None
    elif event == "heartbeat":
        p.update(host_facts())
        p["agent_count"] = int(_agent_count_fn()) if _agent_count_fn else None
        p["provider_mix"] = _mix_text(_provider_mix_fn()) if _provider_mix_fn else None
        p["uptime_days"] = int((time.time() - _started_at) // 86400)
        p["release"] = _call_str(_release_fn)
        p["manager_version"] = _call_str(_manager_version_fn)
        p["alarm_engine_version"] = _call_str(_ae_version_fn)
        try:
            p["agent_versions"] = _mix_text(_agent_versions_fn()) if _agent_versions_fn else None
        except Exception:
            p["agent_versions"] = None
        try:
            p["features"] = _mix_text(_features_fn()) if _features_fn else None
        except Exception:
            p["features"] = None
    if extra:
        p.update({k: v for k, v in extra.items() if k in _KEYS})
    return _present(p)


def _outside_product(rel: str) -> bool:
    parts = rel.split(os.sep)
    return any(p == "site-packages" or p == "venv" for p in parts[:-1])


def frames_text(tb, root: Path, limit: int = FRAME_LIMIT) -> str:
    root = str(Path(root).resolve())
    out = []
    for fr in traceback.extract_tb(tb):
        fn = str(Path(fr.filename).resolve()) if fr.filename else ""
        if not fn.startswith(root + os.sep):
            continue
        rel = fn[len(root) + 1:]
        if _outside_product(rel):
            continue
        out.append(f"{rel}:{fr.lineno}:{fr.name}")
    out = out[-limit:]
    while out and len(";".join(out)) > FRAMES_MAX_CHARS:
        out.pop(0)
    return ";".join(out)


def crash_hash(exc_type: str, frames: str) -> str:
    return hashlib.sha256(f"{exc_type}\n{frames}".encode()).hexdigest()[:16]


def build_crash_payload(exc, tb, *, component: str, data_dir: Path, version: str, channel: str,
                        root: Path, uptime_s: int, exc_type: Optional[str] = None,
                        frames: Optional[str] = None) -> dict:
    p = _blank("crash", data_dir, version, channel)
    exc_type = exc_type if exc_type is not None else type(exc).__name__[:128]
    frames = frames if frames is not None else frames_text(tb, root)
    p["release"] = _call_str(_release_fn)
    p.update(component=component, exc_type=exc_type, frames=frames or None,
             crash_hash=crash_hash(exc_type, frames), uptime_s=int(uptime_s))
    return _present(p)


# ── files ────────────────────────────────────────────────────────────────────

def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    with _lock:
        _write_private(tmp, json.dumps(obj, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)


def _append_log(data_dir: Path, line: str) -> None:
    # 0600 append, no symlink following.
    try:
        fd = os.open(Path(data_dir) / LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(f"{_iso()} {line}\n")
    except Exception:
        pass  # log append is best effort


def record_sent(data_dir: Path, payload: dict) -> None:
    _write_json(Path(data_dir) / LAST, payload)


def write_pending(data_dir: Path, payload: dict) -> None:
    _write_json(Path(data_dir) / PENDING, payload)


def flush_pending(data_dir: Path, endpoint_url: str) -> bool:
    f = Path(data_dir) / PENDING
    if not f.is_file():
        return False
    try:
        payload = json.loads(f.read_text())
    except Exception:
        f.unlink(missing_ok=True)
        return False
    payload["sent_at"] = _iso()
    if send(payload, endpoint_url):
        record_sent(data_dir, payload)
        f.unlink(missing_ok=True)
        _append_log(data_dir, f"sent pending {payload.get('event')} (manager start)")
        return True
    _append_log(data_dir, f"pending {payload.get('event')} still unsent")
    return False


def allow_crash(data_dir: Path, h: str, now: Optional[float] = None) -> bool:
    with _lock:
        return _allow_crash(data_dir, h, now)


def _allow_crash(data_dir: Path, h: str, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    f = Path(data_dir) / CRASHES
    try:
        st = json.loads(f.read_text()) if f.is_file() else {}
    except Exception:
        st = {}
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    if st.get("day") != day:
        st = {"day": day, "n": 0, "hashes": st.get("hashes", {})}
    last = st["hashes"].get(h)
    if last is not None and now - float(last) < CRASH_WINDOW_S:
        return False
    if int(st.get("n", 0)) >= CRASH_DAILY_CAP:
        return False
    st["hashes"][h] = now
    st["n"] = int(st.get("n", 0)) + 1
    st["hashes"] = {k: v for k, v in st["hashes"].items() if now - float(v) < CRASH_WINDOW_S}
    try:
        _write_json(f, st)
    except Exception:
        pass  # stamp write is best effort
    return True


# ── sending ──────────────────────────────────────────────────────────────────

def _post(url: str, body: bytes, timeout: float):
    """One HTTPS POST. Uses the companion's pinned session when importable, else urllib."""
    try:
        import companion  # pinned adapter: public address only, no proxies, no redirects
    except ImportError:
        companion = None
    if companion is not None:
        session = companion._push_session(url)
        try:
            return session.post(url, data=body, headers={"Content-Type": "application/json"},
                                timeout=timeout)
        finally:
            session.close()
    import urllib.request
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "llm-systems-manager"})
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.ProxyHandler({}))
    opener.add_handler(urllib.request.HTTPSHandler())
    with opener.open(req, timeout=timeout) as r:
        return type("R", (), {"status_code": r.status})()


def send(payload: dict, endpoint_url: str, timeout: float = TIMEOUT_S) -> bool:
    if not endpoint_url.startswith("https://"):
        return False
    try:
        body = json.dumps(payload, separators=(",", ":")).encode()
        r = _post(endpoint_url, body, timeout)
        return 200 <= int(getattr(r, "status_code", 0)) < 300
    except Exception as e:
        if _log:
            _log.debug("telemetry send failed: %s", type(e).__name__)
        return False


def _write_stamp(path: Path, text: str) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    with _lock:
        _write_private(tmp, text + "\n")
        os.replace(tmp, path)


def _deliver(payload: dict) -> bool:
    assert _data_dir is not None
    ok = send(payload, endpoint())
    if ok:
        record_sent(_data_dir, payload)
        _append_log(_data_dir, f"sent {payload['event']}")
        if payload["event"] in ("heartbeat", "checkin"):
            _hb["sent"] = time.time()
            _write_stamp(_data_dir / HEARTBEAT_STAMP, payload.get("sent_at") or _iso())
    elif payload["event"] in ("install", "update"):
        write_pending(_data_dir, payload)
        _append_log(_data_dir, f"queued {payload['event']} (send failed)")
    else:
        _append_log(_data_dir, f"dropped {payload['event']} (send failed)")
    return ok


def send_now() -> dict:
    """Sends a heartbeat report immediately, whatever the daily toggle says. Never raises.
    One send at a time; a second caller gets {"ok": False, "error": "busy"}."""
    if do_not_track():
        return {"ok": False, "error": "do_not_track"}
    if not _send_lock.acquire(blocking=False):
        return {"ok": False, "error": "busy"}
    try:
        p = build_payload("heartbeat", data_dir=_data_dir, version=_version,
                          channel=detect_channel(_root))
        ok = bool(_deliver(p))
        return {"ok": ok, "event": "heartbeat", "sent_at": p.get("sent_at")}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}
    finally:
        _send_lock.release()


# ── crash reporting ──────────────────────────────────────────────────────────

def report_exception(exc, tb, *, component: str) -> None:
    """Never raises. Sends at most one report per crash hash per day, five per day."""
    try:
        if _data_dir is None or not enabled("crash_reports"):
            return
        root = _root or _data_dir.parent
        exc_type = type(exc).__name__[:128]
        frames = frames_text(tb, root)
        if not allow_crash(_data_dir, crash_hash(exc_type, frames)):
            return
        p = build_crash_payload(exc, tb, component=component, data_dir=_data_dir, version=_version,
                                channel=detect_channel(root), root=root,
                                uptime_s=int(time.time() - _started_at),
                                exc_type=exc_type, frames=frames)
        t = threading.Thread(target=_deliver, args=(p,), name="telemetry-crash", daemon=True)
        t.start()
        t.join(2.0)
    except Exception:
        pass  # crash reporting never raises


def install_excepthooks(component: str) -> None:
    prev_sys, prev_thr = sys.excepthook, threading.excepthook

    def _sys_hook(exc_type, exc, tb):
        report_exception(exc, tb, component=component)
        prev_sys(exc_type, exc, tb)

    def _thr_hook(args):
        report_exception(args.exc_value, args.exc_traceback, component=component)
        prev_thr(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thr_hook


def _install_source() -> Optional[str]:
    try:
        import companion
        found = source_of_install(companion._installed_release()["source"])
    except Exception:
        found = None
    if found:
        return found
    return "git" if _root is not None and (Path(_root) / ".git").exists() else "local"


# ── scheduler ────────────────────────────────────────────────────────────────

def _heartbeat_age(now: Optional[float] = None) -> float:
    """Seconds since the last sent heartbeat; infinity when none was ever sent."""
    now = time.time() if now is None else now
    try:
        txt = (_data_dir / HEARTBEAT_STAMP).read_text().strip()
        ts = datetime.strptime(txt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        return now - ts
    except Exception:
        return float("inf")


_hb = {"attempt": 0.0, "sent": 0.0, "not_before": 0.0}
_send_lock = threading.Lock()


def _spread_s(data_dir: Optional[Path]) -> int:
    """Per-install offset, 0..HEARTBEAT_SPREAD_S, so hosts restarted together do not report together."""
    try:
        iid, _ = install_identity(data_dir)
        return int(hashlib.sha256(iid.encode()).hexdigest()[:8], 16) % HEARTBEAT_SPREAD_S
    except Exception:
        return 0


def _daily_event() -> Optional[str]:
    """The one report sent each day: the detailed heartbeat, else the check-in, else None."""
    if enabled("heartbeat"):
        return "heartbeat"
    return "checkin" if enabled("checkin") else None


def _heartbeat_tick() -> None:
    now = time.time()
    event = _daily_event()
    if event is None or now < _hb["not_before"]:
        return
    if min(_heartbeat_age(now), now - _hb["sent"]) < HEARTBEAT_EVERY_S:
        return
    if now - _hb["attempt"] < HEARTBEAT_RETRY_S or not _send_lock.acquire(blocking=False):
        return
    try:
        _hb["attempt"] = now
        _deliver(build_payload(event, data_dir=_data_dir, version=_version,
                               channel=detect_channel(_root)))
    finally:
        _send_lock.release()


def start_scheduler(stop_fn: Callable[[], bool] = lambda: False) -> threading.Thread:
    """First start: flush a pending file or send the install report when no report ever left.
    Then a heartbeat whenever enabled and the last one is a day old."""
    def _loop():
        try:
            detect_channel(_root)
        except Exception:
            pass  # channel detection is best effort
        time.sleep(FIRST_DELAY_S)
        try:
            if do_not_track():
                pass  # nothing leaves while DO_NOT_TRACK is set
            elif (_data_dir / PENDING).is_file():
                flush_pending(_data_dir, endpoint())
            elif not (_data_dir / LAST).is_file() and enabled("install_report"):
                _deliver(build_payload("install", data_dir=_data_dir, version=_version,
                                       channel=detect_channel(_root), mode="",
                                       source=_install_source() or ""))
        except Exception as e:
            if _log:
                _log.debug("telemetry first-start: %s", type(e).__name__)
        _hb["not_before"] = time.time() + _spread_s(_data_dir)
        while True:
            try:
                _heartbeat_tick()
            except Exception as e:
                if _log:
                    _log.debug("telemetry heartbeat: %s", type(e).__name__)
            if stop_fn():
                break
            time.sleep(TICK_S)
            if stop_fn():
                break
    t = threading.Thread(target=_loop, name="telemetry", daemon=True)
    t.start()
    return t


def status() -> dict:
    last = None
    try:
        f = (_data_dir / LAST) if _data_dir else None
        if f and f.is_file():
            p = json.loads(f.read_text())
            last = {"event": p.get("event"), "sent_at": p.get("sent_at"), "payload": p}
    except Exception:
        last = None
    return {"install_report": enabled("install_report"), "crash_reports": enabled("crash_reports"),
            "checkin": enabled("checkin"), "heartbeat": enabled("heartbeat"),
            "do_not_track": do_not_track(),
            "pending": bool(_data_dir and (_data_dir / PENDING).is_file()), "last": last}


# ── CLI for the shell installers ─────────────────────────────────────────────

def _main(argv) -> int:
    ap = argparse.ArgumentParser(prog="telemetry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("payload")
    p.add_argument("--event", required=True, choices=EVENTS)
    p.add_argument("--channel", required=True)
    p.add_argument("--mode", default="")
    p.add_argument("--outcome", default="")
    p.add_argument("--release", default="")
    p.add_argument("--source", default="")
    p.add_argument("--data-dir", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--root", required=True)
    s = sub.add_parser("send")
    s.add_argument("--endpoint", required=True)
    s.add_argument("--file", required=True)
    sub.add_parser("facts")
    a = ap.parse_args(argv)
    if a.cmd == "facts":
        print(json.dumps(host_facts()))
        return 0
    if a.cmd == "payload":
        out = build_payload(a.event, data_dir=Path(a.data_dir), version=a.version, channel=a.channel,
                            mode=a.mode, outcome=a.outcome, source=a.source,
                            extra={"release": a.release[:128] or None})
        print(json.dumps(out))
        return 0
    payload = json.loads(Path(a.file).read_text())
    payload["sent_at"] = _iso()
    return 0 if send(payload, a.endpoint) else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
