#!/usr/bin/env python3
"""Detect a running llama-server and its systemd unit (stdlib only).

CLI: detect_llama.py [--port N] [--json | --sh]; always exits 0.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
from typing import Optional

_SINGLE_MODEL_FLAGS = ("-m", "--model", "-hf", "-hfr", "--hf-repo", "-mu", "--model-url")
_ROUTER_FLAGS = ("--models-preset", "--models-dir")
_ENV_MODEL_KEYS = ("LLAMA_ARG_MODEL", "LLAMA_ARG_HF_REPO")
_SHOW_PROPS = "FragmentPath,DropInPaths,User,ExecStart,Environment,EnvironmentFiles,LoadState,ActiveState"


def single_model_flags() -> tuple:
    """Flags that select single-model mode."""
    return _SINGLE_MODEL_FLAGS


@dataclasses.dataclass
class Detection:
    found: bool = False
    running: bool = False
    pid: Optional[int] = None
    binary: str = ""
    host: str = ""
    port: Optional[int] = None
    presets_file: str = ""
    models_dir: str = ""
    log_file: str = ""
    mode: str = ""
    configured_by: str = ""
    unit: str = ""
    unit_path: str = ""
    drop_ins: list = dataclasses.field(default_factory=list)
    unit_user: str = ""
    process_user: str = ""
    exec_start: str = ""
    candidates: list = dataclasses.field(default_factory=list)
    managed: bool = False


def parse_exec_flags(line: str) -> list:
    """Flag tokens (those starting with '-') from a shell command line."""
    try:
        parts = shlex.split(line)
    except ValueError:
        return []
    return [p for p in parts if p.startswith("-")]


def flag_value(flags: list, *names: str) -> str:
    """Value of the first matching flag (`--x v`, `--x=v`), else ''."""
    for i, tok in enumerate(flags):
        for name in names:
            if tok == name:
                return flags[i + 1] if i + 1 < len(flags) else ""
            if tok.startswith(name + "="):
                return tok[len(name) + 1:]
    return ""


def _read(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _has_flag(argv: list, names) -> bool:
    return any(t == n or t.startswith(n + "=") for t in argv for n in names)


def _unit_from_cgroup(text: str) -> str:
    """Return the system-manager unit a cgroup path names; user-session units yield ''."""
    for line in text.splitlines():
        if "/user.slice/" in line or "user@" in line:
            continue
        last = line.rsplit("/", 1)[-1].strip()
        if last.endswith(".service"):
            return last
    return ""


def _process_user(status: str) -> str:
    m = re.search(r"^Uid:\s+(\d+)", status, re.M)
    if not m:
        return ""
    uid = int(m.group(1))
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _systemctl_show(unit: str, run) -> dict:
    try:
        res = run(["systemctl", "show", unit, "-p", _SHOW_PROPS],
                  capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = getattr(res, "stdout", "") or ""
    props: dict = {}
    for line in out.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            k = k.strip()
            props[k] = (props[k] + " " + v.strip()) if k == "EnvironmentFiles" and k in props else v.strip()
    return props


def _exec_argv(exec_start: str) -> str:
    m = re.search(r"argv\[\]=(.*?)\s;", exec_start)
    return m.group(1) if m else exec_start


def _exec_path(exec_start: str) -> str:
    m = re.search(r"path=(.*?)\s;", exec_start)
    return m.group(1) if m else ""


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _unit_env(props: dict, read=open) -> dict:
    """Merged KEY=VALUE pairs from a unit's Environment= and EnvironmentFile= entries."""
    env = {}
    raw = props.get("Environment", "")
    try:
        items = shlex.split(raw)
    except ValueError:
        items = raw.split()
    for item in items:
        k, sep, v = item.partition("=")
        if sep and k:
            env[k] = v
    files = props.get("EnvironmentFiles", "")
    paths = re.findall(r"(\S+) \(ignore_errors=[^)]*\)", files) or files.split()
    for path in paths:
        try:
            with read(path) as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            k, sep, v = line.partition("=")
            if sep and k.strip():
                env[k.strip()] = _unquote(v.strip())
    return env


def _scan(proc_root: str) -> list:
    pids = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return pids
    for name in names:
        if not name.isdigit():
            continue
        base = os.path.join(proc_root, name)
        if _read(os.path.join(base, "comm")).strip() != "llama-server":
            continue
        raw = _read(os.path.join(base, "cmdline"))
        argv = [a for a in raw.split("\0") if a]
        try:
            exe = os.readlink(os.path.join(base, "exe"))
        except OSError:
            exe = ""
        binary = exe or (argv[0] if argv else "")
        if "/.lmstudio/" in binary or (argv and "/.lmstudio/" in argv[0]):
            continue
        pids.append((int(name), base, argv, binary))
    return sorted(pids)


def _fill(det: Detection, argv: list, unit: str, props: dict, process_user: str,
          read=open) -> None:
    """Set flag, unit, environment and mode fields from argv and the unit's properties."""
    env = {}
    if unit:
        det.process_user = process_user
        det.unit = unit
        det.managed = True
        det.unit_path = props.get("FragmentPath", "")
        det.drop_ins = props.get("DropInPaths", "").split()
        det.unit_user = props.get("User", "")
        det.exec_start = _exec_argv(props.get("ExecStart", ""))
        env = _unit_env(props, read)
    det.host = flag_value(argv, "--host") or env.get("LLAMA_ARG_HOST", "")
    port = flag_value(argv, "--port") or env.get("LLAMA_ARG_PORT", "")
    det.port = int(port) if port.isdigit() else None
    det.presets_file = flag_value(argv, "--models-preset") or env.get("LLAMA_ARG_MODELS_PRESET", "")
    det.models_dir = flag_value(argv, "--models-dir") or env.get("LLAMA_ARG_MODELS_DIR", "")
    det.log_file = flag_value(argv, "--log-file") or env.get("LLAMA_ARG_LOG_FILE", "")
    env_model = any(k in env for k in _ENV_MODEL_KEYS)
    if _has_flag(argv, _SINGLE_MODEL_FLAGS):
        det.mode, det.configured_by = "single", "flags"
    elif _has_flag(argv, _ROUTER_FLAGS):
        det.mode, det.configured_by = "router", "flags"
    elif env_model:
        det.mode, det.configured_by = "single", "environment"
    else:
        det.mode = "unknown"


def _stopped_unit(run, prefer: str = "") -> tuple:
    """The configured unit when it runs llama-server, else the first loaded llama unit, as (name, props, argv)."""
    try:
        res = run(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--plain"],
                  capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "", {}, []
    names = [ln.split()[0] for ln in (getattr(res, "stdout", "") or "").splitlines() if ln.split()]
    names = ([prefer] if prefer else []) + [n for n in names if "llama" in n.lower() and n != prefer]
    for name in names:
        props = _systemctl_show(name, run)
        exec_start = props.get("ExecStart", "")
        if props.get("LoadState") != "loaded" or \
                os.path.basename(_exec_path(exec_start)) != "llama-server":
            continue
        try:
            argv = shlex.split(_exec_argv(exec_start))
        except ValueError:
            continue
        if argv:
            return name, props, argv
    return "", {}, []


def detect(cfg_port: Optional[int] = None, *, proc_root: str = "/proc",
           run=subprocess.run, read=open, cfg_unit: str = "") -> Detection:
    """Find the running llama-server, or an installed stopped unit, and how it is launched."""
    procs = _scan(proc_root)
    det = Detection()
    if not procs:
        name, props, argv = _stopped_unit(run, cfg_unit)
        if not name:
            return det
        det.found = True
        det.binary = argv[0]
        _fill(det, argv, name, props, props.get("User", ""), read)
        return det
    info = []
    for pid, base, argv, binary in procs:
        unit = _unit_from_cgroup(_read(os.path.join(base, "cgroup")))
        info.append((pid, base, argv, binary, unit))
    pick = None
    if cfg_port is not None:
        for it in info:
            if flag_value(it[2], "--port") == str(cfg_port):
                pick = it
                break
    if pick is None:
        pick = next((it for it in info if it[4]), info[0])
    pid, base, argv, binary, unit = pick
    det.found = True
    det.running = True
    det.pid = pid
    det.binary = binary
    det.candidates = [p[0] for p in procs]
    props = _systemctl_show(unit, run) if unit else {}
    if props.get("LoadState") != "loaded":
        unit = ""
    user = _process_user(_read(os.path.join(base, "status"))) if unit else ""
    _fill(det, argv, unit, props, user, read)
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
        lines.append(f"LLAMA_DETECTED_{f.name.upper()}={shlex.quote(s)}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="")
    ap.add_argument("--unit", default="")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true")
    g.add_argument("--sh", action="store_true")
    args = ap.parse_args(argv)
    port = int(args.port) if str(args.port).isdigit() else None
    det = detect(port, cfg_unit=args.unit or "")
    if args.sh:
        print("\n".join(_sh_lines(det)))
    else:
        print(json.dumps(dataclasses.asdict(det)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
