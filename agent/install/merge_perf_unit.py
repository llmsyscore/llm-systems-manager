#!/usr/bin/env python3
"""Merge a shipped example systemd unit into its installed copy, keeping operator edits.

    merge_perf_unit.py --example EX --dest DEST [--base OLD_EX] [--force] [--verbose] [--dry-run]

Prints one status line (installed | merged | replaced | unchanged | would-merge),
then the unified diff when --verbose. Exit 1 on error.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import os
import re
import shutil
import sys
import tempfile

# Active lines older examples shipped that the current examples no longer carry.
RETIRED = {
    "ConditionPathExists=/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
}

# One value per unit: the operator's edit replaces the shipped line in place.
SINGLE_VALUED = {
    "Description", "Documentation", "DefaultDependencies", "After", "Before",
    "Type", "RemainAfterExit", "User", "Group", "WantedBy", "TimeoutStartSec",
}

_SECTION = re.compile(r"^\[([^\]]+)\]\s*$")
_KV = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*=(.*)$")
_CKV = re.compile(r"^\s*#\s?([A-Za-z][A-Za-z0-9_-]*)=(\S.*)$")


class Line:
    __slots__ = ("kind", "section", "key", "norm", "raw")

    def __init__(self, kind, section, raw, key=None, norm=None):
        self.kind, self.section, self.raw, self.key, self.norm = kind, section, raw, key, norm

    def __repr__(self):
        return f"Line({self.kind}, {self.section!r}, {self.raw!r})"


def parse(text: str) -> list[Line]:
    out: list[Line] = []
    section = None
    for raw in text.splitlines():
        raw = raw.rstrip()
        m = _SECTION.match(raw)
        if m:
            section = m.group(1)
            out.append(Line("section", section, raw))
            continue
        if not raw.strip():
            out.append(Line("blank", section, raw))
            continue
        if raw.lstrip().startswith(("#", ";")):
            m = _CKV.match(raw)
            if m:
                out.append(Line("ckv", section, raw, m.group(1), f"{m.group(1)}={m.group(2).strip()}"))
            else:
                out.append(Line("comment", section, raw))
            continue
        m = _KV.match(raw)
        if m and section is not None:
            out.append(Line("kv", section, raw, m.group(1), f"{m.group(1)}={m.group(2).strip()}"))
        else:
            out.append(Line("other", section, raw, None, raw.strip()))
    return out


def identity(key: str | None, norm: str):
    """What makes two lines 'the same setting' even when a value was edited."""
    if key in SINGLE_VALUED:
        return (key,)
    value = norm.split("=", 1)[1] if "=" in norm else norm
    m = re.search(r"/sys/[^\s'\";]+", value)
    if m:
        return (key, "sysfs", m.group(0))
    return (key, " ".join(re.sub(r"\d+", "", value).split()))


def merge(ours: str, theirs: str, base: str | None = None) -> str:
    ours_l, theirs_l = parse(ours), parse(theirs)
    base_l = parse(base) if base is not None else None

    ours_active = {(l.section, l.norm): l for l in ours_l if l.kind in ("kv", "other")}
    ours_ckv = {(l.section, l.norm): l for l in ours_l if l.kind == "ckv"}
    theirs_active = {(l.section, l.norm) for l in theirs_l if l.kind == "kv"}
    theirs_comments = {l.raw.strip() for l in theirs_l if l.kind in ("comment", "ckv")}
    if base_l is None:
        base_active = set(theirs_active)
        base_comments = set()
        retired = RETIRED
    else:
        base_active = {(l.section, l.norm) for l in base_l if l.kind == "kv"}
        base_comments = {l.raw.strip() for l in base_l if l.kind in ("comment", "ckv")}
        retired = RETIRED | {n for (_s, n) in base_active}

    # Operator lines: active in the install, not shipped (now or before).
    added: list[Line] = [
        l for l in ours_l
        if l.kind in ("kv", "other")
        and (l.section, l.norm) not in base_active
        and not (base_l is None and l.norm in retired)
    ]
    removed = {k for k in base_active if k not in ours_active}
    consumed: set[int] = set()

    # Comment lines the operator wrote directly above one of their lines.
    def own_comments(line: Line) -> list[str]:
        idx = ours_l.index(line)
        got: list[str] = []
        for prev in reversed(ours_l[:idx]):
            if prev.kind not in ("comment", "ckv"):
                break
            s = prev.raw.strip()
            if s in theirs_comments or s in base_comments:
                break
            got.append(prev.raw)
        return list(reversed(got))

    def take(section, ident):
        for l in added:
            if id(l) in consumed or l.section != section:
                continue
            if identity(l.key, l.norm) == ident:
                consumed.add(id(l))
                return l
        return None

    def take_exact(section, norm):
        l = ours_active.get((section, norm))
        if l is not None and id(l) in {id(a) for a in added}:
            consumed.add(id(l))
        return l

    out: list[str] = []
    section_start = 0

    def emit_op(op: Line):
        out.extend(own_comments(op))
        out.append(op.raw)

    def flush_section(section):
        """Append the operator's leftover lines for `section` after its last setting."""
        pending = [l for l in added if l.section == section and id(l) not in consumed]
        if not pending:
            return
        insert_at = len(out)
        while insert_at > section_start and not out[insert_at - 1].strip():
            insert_at -= 1
        block: list[str] = [""]
        for l in pending:
            block.extend(own_comments(l))
            block.append(l.raw)
            consumed.add(id(l))
        out[insert_at:insert_at] = block

    current = None
    for l in theirs_l:
        if l.kind == "section":
            flush_section(current)
            current = l.section
            out.append(l.raw)
            section_start = len(out)
            continue
        if l.kind in ("comment", "blank"):
            out.append(l.raw)
            continue
        key = (l.section, l.norm)
        if l.kind == "kv":
            if key in ours_active:
                out.append(take_exact(*key).raw)
            elif key in removed:
                op = take(l.section, identity(l.key, l.norm))
                if op is not None:
                    emit_op(op)
                elif key in ours_ckv:
                    out.append(ours_ckv[key].raw)
            else:
                op = take(l.section, identity(l.key, l.norm))
                if op is not None:
                    emit_op(op)
                else:
                    out.append(l.raw)
            continue
        if l.kind == "ckv":
            if key in ours_active:
                out.append(take_exact(*key).raw)
                continue
            op = take(l.section, identity(l.key, l.norm))
            if op is not None:
                emit_op(op)
            else:
                out.append(l.raw)
            continue
        out.append(l.raw)
    flush_section(current)

    # Whole sections only the operator has.
    their_sections = {l.section for l in theirs_l if l.kind == "section"}
    for sec in [s for s in dict.fromkeys(l.section for l in ours_l if l.kind == "section") if s not in their_sections]:
        while out and not out[-1].strip():
            out.pop()
        out.append("")
        out.extend(l.raw for l in ours_l if l.section == sec and l.kind != "blank")
        for l in ours_l:
            if l.section == sec:
                consumed.add(id(l))

    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write_atomic(path: str, text: str) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".merge-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup(path: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{path}.bak-{stamp}"
    n = 1
    while os.path.exists(bak):
        n += 1
        bak = f"{path}.bak-{stamp}-{n}"
    shutil.copy2(path, bak)
    return bak


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--example", required=True, help="shipped example unit")
    ap.add_argument("--dest", required=True, help="installed unit path")
    ap.add_argument("--base", help="the example this install was last merged with, when known")
    ap.add_argument("--force", action="store_true", help="replace the installed unit with the example (backup first)")
    ap.add_argument("--verbose", action="store_true", help="print the unified diff")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    a = ap.parse_args(argv)

    try:
        example = _read(a.example)
    except OSError as e:
        print(f"error: cannot read example: {e}", file=sys.stderr)
        return 1
    name = os.path.basename(a.dest)

    if not os.path.exists(a.dest):
        if a.dry_run:
            print(f"would-install {name}")
            return 0
        _write_atomic(a.dest, example)
        print(f"installed {name}")
        return 0

    try:
        installed = _read(a.dest)
    except OSError as e:
        print(f"error: cannot read {a.dest}: {e}", file=sys.stderr)
        return 1
    base = None
    if a.base and os.path.exists(a.base):
        base = _read(a.base)

    merged = example if a.force else merge(installed, example, base)
    if merged == installed:
        print(f"unchanged {name}")
        return 0

    diff = ""
    if a.verbose:
        diff = "".join(difflib.unified_diff(
            installed.splitlines(True), merged.splitlines(True),
            fromfile=f"{a.dest} (installed)", tofile=f"{a.dest} (merged)"))
    if a.dry_run:
        print(f"would-{'replace' if a.force else 'merge'} {name}")
        if diff:
            print(diff, end="")
        return 0
    bak = _backup(a.dest)
    _write_atomic(a.dest, merged)
    print(f"{'replaced' if a.force else 'merged'} {name} (backup: {bak})")
    if diff:
        print(diff, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
