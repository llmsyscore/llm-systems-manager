"""Comment-preserving TOML write-back for the settings editor (#606).
Patch → schema-validate the whole candidate → backup → atomic 0600 replace."""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import tomllib
from pathlib import Path
from typing import Optional

from config.unified_config import CONFIG_PATH, Settings


def _tomlkit():
    """Deferred import: a deploy that skipped pip install degrades to a clear
    per-request error instead of failing the whole service at import."""
    import tomlkit
    return tomlkit

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_BACKUP_KEEP = 10


class SettingsIOError(RuntimeError):
    pass


class SettingsValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


def resolve_config_path() -> Path:
    if CONFIG_PATH is not None:
        return Path(CONFIG_PATH)
    return Path(__file__).resolve().parent.parent.parent / "config" / "llm-systems.toml"


def _load_doc(path: Path):
    tomlkit = _tomlkit()
    if path.is_file():
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise SettingsIOError(f"config file unparseable: {e}") from e
    example = path.with_name(path.name + ".example")
    if example.is_file():
        return tomlkit.parse(example.read_text(encoding="utf-8"))
    return tomlkit.document()


def _set_dotted(doc, dotted: str, value, created: Optional[set] = None) -> None:
    tomlkit = _tomlkit()
    parts = dotted.split(".")
    node = doc
    for i, key in enumerate(parts[:-1]):
        if key not in node:
            node[key] = tomlkit.table()
            if created is not None:
                created.add(".".join(parts[:i + 1]))
        node = node[key]
    try:
        node[parts[-1]] = value
    except Exception as e:
        raise SettingsValidationError({dotted: f"unsupported value: {e}"}) from e


def _remove_dotted(doc, dotted: str) -> None:
    parts = dotted.split(".")
    node = doc
    try:
        for key in parts[:-1]:
            node = node[key]
        del node[parts[-1]]
    except (KeyError, TypeError):
        pass  # absent path or non-table intermediate: nothing to remove


_BANNER_RE = re.compile(r'^#\s*(?:[─═]{2,}|-{3,})')


def _lift_once(lines: list[str], header: str) -> bool:
    """Move one new section above a banner comment block sitting directly over it."""
    try:
        h = lines.index(header)
    except ValueError:
        return False
    j = h - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    k = j
    while k >= 0 and lines[k].lstrip().startswith("#"):
        k -= 1
    block = lines[k + 1:j + 1]
    if not block or not any(_BANNER_RE.match(ln.strip()) for ln in block):
        return False
    e = h + 1
    while e < len(lines) and not lines[e].lstrip().startswith("["):
        e += 1
    section = lines[h:e]
    while section and not section[-1].strip():
        section.pop()
    p = k + 1
    while p > 0 and not lines[p - 1].strip():
        p -= 1
    head = lines[:p] + ([""] if p else [])
    lines[:] = head + section + lines[p:j + 1] + lines[j + 1:h] + lines[e:]
    return True


def _lift_new_sections(text: str, created: set) -> str:
    """Place newly created table headers above the next section's banner comment."""
    if not created:
        return text
    lines = text.splitlines()
    moved = False
    for path in sorted(created):
        moved |= _lift_once(lines, f"[{path}]")
    if not moved:
        return text
    lifted = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    try:
        same = tomllib.loads(lifted) == tomllib.loads(text)
    except Exception:
        same = False
    if not same:
        log.warning("settings layout lift changed the data; keeping tomlkit layout")
        return text
    return lifted


def _validate(text: str) -> None:
    try:
        data = tomllib.loads(text)
    except Exception as e:
        raise SettingsValidationError({"_toml": str(e)}) from e
    try:
        # Validates the candidate document via init kwargs, not the on-disk file.
        Settings(**data)
    except Exception as e:
        raise SettingsValidationError({"_schema": str(e)[:500]}) from e


def _backup(path: Path) -> None:
    if not path.is_file():
        return
    try:
        bdir = path.parent / "backups"
        bdir.mkdir(mode=0o700, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        dst = bdir / f"{path.name}.{ts}"
        n = 1
        while dst.exists():
            n += 1
            dst = bdir / f"{path.name}.{ts}-{n}"
        shutil.copy2(path, dst)
        # Prune only our own timestamped backups, never other files in backups/.
        pat = re.compile(re.escape(path.name) + r"\.\d{8}-\d{6}(-\d+)?$")
        ours = sorted(p for p in bdir.iterdir() if pat.fullmatch(p.name))
        for old in ours[:-_BACKUP_KEEP]:
            old.unlink()
    except Exception:
        log.exception("settings backup failed (continuing)")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def apply_patches(changes: dict[str, object],
                  config_path: Optional[Path] = None,
                  removals: "tuple[str, ...] | list[str]" = ()) -> None:
    path = Path(config_path) if config_path else resolve_config_path()
    with _LOCK:
        doc = _load_doc(path)
        created: set = set()
        for dotted, value in changes.items():
            _set_dotted(doc, dotted, value, created)
        for dotted in removals:
            _remove_dotted(doc, dotted)
        text = _lift_new_sections(_tomlkit().dumps(doc), created)
        _validate(text)
        _backup(path)
        _atomic_write(path, text)
    log.info("settings updated: %s",
             ", ".join(sorted(list(changes) + list(removals))))


def read_sections(prefixes: tuple[str, ...],
                  config_path: Optional[Path] = None) -> dict:
    path = Path(config_path) if config_path else resolve_config_path()
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise SettingsIOError(f"config file unparseable: {e}") from e
    return {k: v for k, v in data.items() if k in prefixes}
