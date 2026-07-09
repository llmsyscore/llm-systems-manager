#!/usr/bin/env python3
# Shared TOML span-walk for the installer config reconcile: `merge` appends
# example keys missing from live; `prune` removes upstream-deleted keys.

# Protocol: stdout = resulting TOML; stderr = ADDED=N / PRUNED=N + diagnostic
# tags (PARSE_FAILED / VALIDATE_FAILED / NOTFOUND); exit 0 / 2 / 3 / 64.
import re
import sys
import tomllib

# SECTION_RE tolerates a trailing comment after the closing bracket.
SECTION_RE = re.compile(r'^\s*\[([^\]]+?)\]\s*(?:#.*)?$')
ARRAY_SECTION_RE = re.compile(r'^\s*\[\[([^\]]+?)\]\]\s*(?:#.*)?$')
# KEY_RE accepts dotted top-level keys (`a.b.c = ...`).
KEY_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*=')


def flatten(d, prefix=""):
    """Yield 'a.b.c' for every leaf in the parsed dict."""
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            yield from flatten(v, path)
        else:
            yield path


def split_sections(text):
    """Returns [(section_name, header_line_or_None, [body_lines])].
    [[array-of-tables]] get a '['-prefixed name no dotted path can address."""
    out = []
    cur = ['', None, []]
    for ln in text.splitlines():
        am = ARRAY_SECTION_RE.match(ln)
        m = SECTION_RE.match(ln) if am is None else None
        if am or m:
            out.append(tuple(cur))
            name = '[[' + am.group(1).strip() if am else m.group(1).strip()
            cur = [name, ln, []]
        else:
            cur[2].append(ln)
    out.append(tuple(cur))
    return out


def keys_with_spans(body):
    """Yield (key, start, end_exclusive, comment_block_start) per top-level
    key; spans cover multi-line values via bracket balance."""
    i = 0
    while i < len(body):
        m = KEY_RE.match(body[i])
        if not m:
            i += 1
            continue
        key = m.group(1)
        start = i
        val = body[i].split('=', 1)[1] if '=' in body[i] else ''
        depth_sq = val.count('[') - val.count(']')
        depth_br = val.count('{') - val.count('}')
        i += 1
        while i < len(body) and (depth_sq > 0 or depth_br > 0):
            depth_sq += body[i].count('[') - body[i].count(']')
            depth_br += body[i].count('{') - body[i].count('}')
            i += 1
        # Walk upward over the contiguous comment block above this key.
        cb = start - 1
        while cb >= 0 and body[cb].lstrip().startswith('#'):
            cb -= 1
        yield (key, start, i, cb + 1)


def load_or_die(path, label):
    """Read + parse a TOML file; exits 2 with PARSE_FAILED on error."""
    with open(path) as f:
        text = f.read()
    try:
        return text, tomllib.loads(text)
    except Exception as e:
        sys.stderr.write(f"PARSE_FAILED: {label} TOML at {path} doesn't parse: {e}\n")
        sys.exit(2)


def merge(live_path, example_path):
    """Append example keys missing from live, at their example positions."""
    live_text, live_dict = load_or_die(live_path, "live")
    example_text, ex_dict = load_or_die(example_path, "example")

    live_paths = set(flatten(live_dict))
    ex_paths_ordered = list(flatten(ex_dict))
    missing_paths = [p for p in ex_paths_ordered if p not in live_paths]

    if not missing_paths:
        sys.stderr.write("ADDED=0\n")
        sys.stdout.write(live_text)
        return

    live_secs = split_sections(live_text)
    ex_secs = split_sections(example_text)

    live_idx = {}
    for i, (name, _, _) in enumerate(live_secs):
        if name and name not in live_idx:
            live_idx[name] = i
    ex_by_name = {}
    for name, header, body in ex_secs:
        if name:
            ex_by_name[name] = (header, body)

    def section_for_path(path):
        """Longest example-section prefix of the path, plus the key remainder."""
        parts = path.split('.')
        for i in range(len(parts) - 1, 0, -1):
            section = '.'.join(parts[:i])
            if section in ex_by_name:
                return section, '.'.join(parts[i:])
        return None, path

    # Group missing paths per containing example section (one splice each).
    missing_by_section = {}
    for path in missing_paths:
        section, key_basename = section_for_path(path)
        if section is None or section not in ex_by_name:
            # Top-level preamble scalar or [[aot]] content — skip, don't guess.
            continue
        missing_by_section.setdefault(section, set()).add(key_basename)

    added = []
    appended_sections = []

    def splice_at_positions(live_body, ex_body, missing_keys):
        """Insert each missing key right after the nearest preceding example
        key that live also has, preserving the example's ordering."""
        ex_keys = list(keys_with_spans(ex_body))
        live_end_of = {k: e for k, _s, e, _c in keys_with_spans(live_body)}

        plan = []
        last_anchor_end = 0
        for key, s, e, cb in ex_keys:
            if key in live_end_of:
                last_anchor_end = live_end_of[key]
            elif key in missing_keys:
                plan.append((last_anchor_end, ex_body[cb:e], key))

        # Apply in reverse so earlier inserts don't shift later anchors.
        out = list(live_body)
        for pos, lines, _key in reversed(plan):
            addition = []
            if pos > 0 and out[pos - 1].strip():
                addition.append('')
            addition.extend(lines)
            if pos < len(out) and out[pos].strip():
                addition.append('')
            out[pos:pos] = addition
        return out, [k for _p, _l, k in plan]

    for section, missing_set in missing_by_section.items():
        if section not in live_idx:
            continue
        _, ex_body = ex_by_name[section]
        li = live_idx[section]
        lname, lheader, lbody = live_secs[li]
        new_body, inserted_keys = splice_at_positions(lbody, ex_body, missing_set)
        live_secs[li] = (lname, lheader, new_body)
        for k in inserted_keys:
            added.append(f"{section}.{k}")

    # Sections absent from live: append only the missing keys, example order.
    for section, missing_set in missing_by_section.items():
        if section in live_idx:
            continue
        header, ex_body = ex_by_name[section]
        body_lines = []
        inserted_keys = []
        for key, s, e, cb in keys_with_spans(ex_body):
            if key not in missing_set:
                continue
            if body_lines:
                body_lines.append('')
            body_lines.extend(ex_body[cb:e])
            inserted_keys.append(key)
        appended_sections.append((section, header, body_lines))
        for k in inserted_keys:
            added.append(f"{section}.{k}")

    out = []
    for name, header, body in live_secs:
        if header is not None:
            out.append(header)
        out.extend(body)
    for name, header, body in appended_sections:
        if out and out[-1].strip() != '':
            out.append('')
        out.append(header)
        out.extend(body)
    result = '\n'.join(out)
    if not result.endswith('\n'):
        result += '\n'

    # Refuse to emit output that doesn't parse.
    try:
        tomllib.loads(result)
    except Exception as e:
        sys.stderr.write(f"VALIDATE_FAILED: merged TOML doesn't parse: {e}\n")
        sys.exit(3)

    sys.stderr.write(f"ADDED={len(added)}\n")
    for p in added:
        sys.stderr.write(f"  + {p}\n")
    sys.stdout.write(result)


def prune(live_path, prune_keys):
    """Remove the given dotted keys (and their comment blocks) from live."""
    text, live_dict = load_or_die(live_path, "live")

    live_paths = set(flatten(live_dict))
    targets = [k for k in prune_keys if k in live_paths]
    if not targets:
        sys.stderr.write("PRUNED=0\n")
        sys.stdout.write(text)
        return

    # Every surface form a dotted target can take: key 'c' in [a.b],
    # 'b.c' in [a], or 'a.b.c' at top level.
    want = {}
    for t in targets:
        parts = t.split('.')
        for i in range(len(parts)):
            want[('.'.join(parts[:i]), '.'.join(parts[i:]))] = t

    removed = set()
    pruned_secs = []
    for name, header, body in split_sections(text):
        dropidx = set()
        for key, s, e, cb in keys_with_spans(body):
            t = want.get((name, key))
            if t:
                dropidx.update(range(cb, e))
                removed.add(t)
        new_body = [ln for j, ln in enumerate(body) if j not in dropidx]
        pruned_secs.append((name, header, new_body, bool(dropidx)))

    # Drop a section header its prune emptied (only blanks/comments left).
    out = []
    for name, header, body, touched in pruned_secs:
        if touched and header is not None and not any(True for _ in keys_with_spans(body)):
            continue
        if header is not None:
            out.append(header)
        out.extend(body)
    result = '\n'.join(out)
    if not result.endswith('\n'):
        result += '\n'

    # Refuse to emit output that doesn't parse or that lost an unasked key.
    try:
        post_dict = tomllib.loads(result)
    except Exception as e:
        sys.stderr.write(f"VALIDATE_FAILED: pruned TOML doesn't parse: {e}\n")
        sys.exit(3)
    if set(flatten(post_dict)) != live_paths - removed:
        sys.stderr.write("VALIDATE_FAILED: pruned TOML key set doesn't match expected\n")
        sys.exit(3)

    missed = [t for t in targets if t not in removed]
    for t in missed:
        sys.stderr.write(f"NOTFOUND: {t} (present semantically but no textual match)\n")
    sys.stderr.write(f"PRUNED={len(removed)}\n")
    for t in sorted(removed):
        sys.stderr.write(f"  - {t}\n")
    sys.stdout.write(result)


def main():
    argv = sys.argv[1:]
    if len(argv) == 3 and argv[0] == "merge":
        merge(argv[1], argv[2])
    elif len(argv) >= 3 and argv[0] == "prune":
        prune(argv[1], argv[2:])
    else:
        sys.stderr.write(
            "usage: toml_reconcile.py merge <live.toml> <example.toml>\n"
            "       toml_reconcile.py prune <live.toml> <dotted.key> [...]\n"
        )
        sys.exit(64)


if __name__ == "__main__":
    main()
