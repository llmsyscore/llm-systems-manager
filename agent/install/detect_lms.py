#!/usr/bin/env python3
"""Detect a running LM Studio server and the lms command (stdlib only).

CLI: detect_lms.py [--port N] [--cmd PATH] [--agent-user U] [--json | --sh]; always exits 0.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
from typing import Optional

_DEFAULT_PORT = 1234
_FALLBACK_PORTS = (1234, 1235)
_FIXED_LMS = ("/usr/local/bin/lms", "/opt/homebrew/bin/lms")
_NAME_RE = re.compile(r"(?:^|/)(?:lm-studio|lmstudio|lm studio|llmster)(?:\s|$)", re.I)
_CLI_RE = re.compile(r"/\.lmstudio/bin/lms(?:\s|$)")
_SOCK_RE = re.compile(r"^socket:\[(\d+)\]$")


@dataclasses.dataclass
class Detection:
    found: bool = False
    running: bool = False
    pid: Optional[int] = None
    process_user: str = ""
    port: Optional[int] = None
    api_url: str = ""
    lms_cmd: str = ""
    lms_user: str = ""
    candidates: list = dataclasses.field(default_factory=list)


def _read(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _is_lms(cmd: str) -> bool:
    """True for an LM Studio app, helper or llmster command line; the lms command itself is not."""
    if _CLI_RE.search(cmd):
        return False
    return "/.lmstudio/" in cmd or bool(_NAME_RE.search(cmd))


def _user_of_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _home(user: str) -> str:
    if not user:
        return ""
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return ""


def _status_user(status: str) -> str:
    m = re.search(r"^Uid:\s+(\d+)", status, re.M)
    return _user_of_uid(int(m.group(1))) if m else ""


def _listen_ports(proc_root: str) -> dict:
    """Map socket inode -> port for listening TCP sockets in net/tcp and net/tcp6."""
    out = {}
    for name in ("tcp", "tcp6"):
        for line in _read(os.path.join(proc_root, "net", name)).splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            try:
                out[parts[9]] = int(parts[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
    return out


def _socket_inodes(base: str) -> Optional[set]:
    """Socket inodes held by a process, or None when its fd directory cannot be read."""
    fd_dir = os.path.join(base, "fd")
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return None
    inodes = set()
    for name in names:
        try:
            m = _SOCK_RE.match(os.readlink(os.path.join(fd_dir, name)))
        except OSError:
            continue
        if m:
            inodes.add(m.group(1))
    return inodes


def _scan_proc(proc_root: str) -> list:
    """LM Studio processes from /proc as (pid, user, ports or None)."""
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    listen = None
    found = []
    for name in sorted((n for n in names if n.isdigit()), key=int):
        base = os.path.join(proc_root, name)
        argv = [a for a in _read(os.path.join(base, "cmdline")).split("\0") if a]
        if not argv or not _is_lms(argv[0]):
            continue
        if listen is None:
            listen = _listen_ports(proc_root)
        inodes = _socket_inodes(base)
        ports = None if inodes is None else {listen[i] for i in inodes if i in listen}
        found.append((int(name), _status_user(_read(os.path.join(base, "status"))), ports))
    return found


def _ps_output() -> str:
    try:
        res = subprocess.run(["ps", "-axo", "pid=,user=,comm="],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout or ""


def _scan_ps() -> list:
    """LM Studio processes from ps as (pid, user, None); comm is the executable path, no arguments."""
    found = []
    for line in _ps_output().splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and _is_lms(parts[2]):
            found.append((int(parts[0]), parts[1], None))
    return sorted(found)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, so a 3xx surfaces as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe_v1_models(port: int, timeout: float = 1.0) -> bool:
    """True when GET http://127.0.0.1:<port>/v1/models answers 200 without a redirect."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(f"http://127.0.0.1:{int(port)}/v1/models", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ordered(ports, hint: Optional[int]) -> list:
    """Ports with the hint first, then 1234, then the rest ascending."""
    first = [p for p in (hint, _DEFAULT_PORT) if p is not None and p in ports]
    return list(dict.fromkeys(first + sorted(ports)))


def _pick(procs: list, port_hint: Optional[int], agent_user: str, probe) -> tuple:
    """The (pid, user, port) to report: answering first, then the agent user's, then lowest pid."""
    fallback = list(dict.fromkeys(p for p in (port_hint,) + _FALLBACK_PORTS if p is not None))
    answers: dict = {}
    ranked = []
    for pid, user, ports in procs:
        order = fallback if ports is None else _ordered(ports, port_hint)
        port = None
        for p in order:
            if p not in answers:
                answers[p] = bool(probe(p))
            if answers[p]:
                port = p
                break
        rank = order.index(port) if port is not None else len(order)
        ranked.append(((port is None, user != agent_user or not agent_user, rank, pid),
                       (pid, user, port)))
    ranked.sort(key=lambda r: r[0])
    return ranked[0][1]


def _lms_path(cmd_hint: str, owner: str, agent_user: str) -> str:
    """First existing lms: hint, owner's and agent user's ~/.lmstudio, PATH, fixed paths, other homes (root only)."""
    homes = [_home(owner), _home(agent_user)]
    tail = [shutil.which("lms") or "", *_FIXED_LMS]
    others = [e.pw_dir for e in sorted(pwd.getpwall(), key=lambda e: e.pw_uid)] if os.geteuid() == 0 else []
    paths = [cmd_hint] + [os.path.join(h, ".lmstudio", "bin", "lms") for h in homes if h] + tail
    paths += [os.path.join(h, ".lmstudio", "bin", "lms") for h in others if h and h not in homes]
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return ""


def _file_user(path: str) -> str:
    try:
        return _user_of_uid(os.stat(path).st_uid)
    except OSError:
        return ""


def detect(port_hint: Optional[int] = None, cmd_hint: str = "", agent_user: str = "", *,
           proc_root: str = "/proc", probe=_probe_v1_models, platform: str = sys.platform) -> Detection:
    """Find the LM Studio server process, its API port and the lms command."""
    procs = _scan_ps() if platform == "darwin" else _scan_proc(proc_root)
    det = Detection(candidates=[p[0] for p in procs])
    if procs:
        det.pid, det.process_user, det.port = _pick(procs, port_hint, agent_user, probe)
        det.running = True
        if det.port is not None:
            det.api_url = f"http://127.0.0.1:{det.port}"
    det.lms_cmd = _lms_path(cmd_hint, det.process_user, agent_user)
    det.lms_user = _file_user(det.lms_cmd) if det.lms_cmd else ""
    det.found = det.running or bool(det.lms_cmd)
    return det


def _sh_lines(det: Detection) -> list:
    lines = []
    for f in dataclasses.fields(det):
        v = getattr(det, f.name)
        if isinstance(v, list):
            continue
        if isinstance(v, bool):
            s = "true" if v else "false"
        elif v is None:
            s = ""
        else:
            s = str(v)
        lines.append(f"LMS_DETECTED_{f.name.upper()}={shlex.quote(s)}")
    return lines


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgError(message)


def _print(det: Detection, sh: bool) -> None:
    if sh:
        print("\n".join(_sh_lines(det)))
    else:
        print(json.dumps(dataclasses.asdict(det)))


def main(argv=None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    try:
        return _run(raw)
    except Exception:
        _print(Detection(), "--sh" in raw)
        return 0


def _run(raw: list) -> int:
    ap = _Parser()
    ap.add_argument("--port", default="")
    ap.add_argument("--cmd", default="")
    ap.add_argument("--agent-user", default="")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true")
    g.add_argument("--sh", action="store_true")
    args = ap.parse_args(raw)
    port = int(args.port) if str(args.port).isdigit() else None
    det = detect(port, args.cmd or "", args.agent_user or "")
    _print(det, args.sh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
