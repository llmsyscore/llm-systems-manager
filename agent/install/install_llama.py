#!/usr/bin/env python3
"""Setup-time llama.cpp installer the agent installer (install.sh) shells out to.

Loads the providers/llama_install.py leaf directly (no fastapi/providers
package import), runs a fresh install via the shared run_install() executor,
and prints `RESOLVED_BIN=<path>` to stdout on success. Install/build output
goes to stderr so stdout stays machine-readable for the caller.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import types
from pathlib import Path


def _load_leaf():
    agent_root = Path(__file__).resolve().parent.parent
    leaf = agent_root / "providers" / "llama_install.py"
    if not leaf.exists():
        raise SystemExit(f"[error] llama_install leaf not found at {leaf}")
    spec = importlib.util.spec_from_file_location("llama_install", leaf)
    if spec is None or spec.loader is None:
        raise SystemExit(f"[error] cannot load llama_install leaf at {leaf}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llama_install"] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Install llama.cpp at agent setup time")
    ap.add_argument("--method", required=True)
    ap.add_argument("--build-dir", default="")
    ap.add_argument("--agent-user", default="")
    ap.add_argument("--llama-bin", default="")
    ap.add_argument("--backend", default="")
    ap.add_argument("--git-ref", default="")
    ap.add_argument("--version", dest="rel_version", default="")
    ap.add_argument("--jobs", default="")
    ap.add_argument("--script-path", default="")
    args = ap.parse_args(argv)

    li = _load_leaf()

    cfg = types.SimpleNamespace(
        AGENT_USER=args.agent_user,
        LLAMA_BIN=args.llama_bin,
        LLAMA_BUILD_DIR=args.build_dir,
        LLAMA_BUILD_OPTS={},
    )
    opts: dict = {}
    if args.backend:     opts["backend"] = args.backend
    if args.git_ref:     opts["git_ref"] = args.git_ref
    if args.rel_version: opts["version"] = args.rel_version
    if args.jobs:        opts["jobs"] = args.jobs
    if args.script_path: opts["script_path"] = args.script_path

    def emit(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    try:
        iplan = li.plan(args.method, opts, cfg)
    except li.InstallError as e:
        emit(f"[error] {e}")
        return 2

    try:
        rc, resolved = li.run_install(iplan, emit=emit)
    except OSError as e:
        emit(f"[error] llama.cpp install crashed: {e}")
        return 127
    if rc != 0:
        emit(f"[error] llama.cpp install failed (rc={rc})")
        return rc
    if not resolved or not os.path.exists(resolved):
        emit("[error] install completed but llama-server binary not found"
             f"{(' at ' + resolved) if resolved else ''}")
        return 4
    print(f"RESOLVED_BIN={resolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
