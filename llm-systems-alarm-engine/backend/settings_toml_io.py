"""Comment-preserving TOML write-back for the settings editor (#606).
Patch → schema-validate the whole candidate → backup → atomic 0600 replace."""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import tomllib
from pathlib import Path
from typing import Optional

import tomlkit

from config.unified_config import CONFIG_PATH, Settings

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


def _load_doc(path: Path) -> tomlkit.TOMLDocument:
    if path.is_file():
        try:
            return tomlkit.parse(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise SettingsIOError(f"config file unparseable: {e}") from e
    example = path.with_name(path.name + ".example")
    if example.is_file():
        return tomlkit.parse(example.read_text(encoding="utf-8"))
    return tomlkit.document()


def _set_dotted(doc, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = doc
    for key in parts[:-1]:
        if key not in node:
            node[key] = tomlkit.table()
        node = node[key]
    node[parts[-1]] = value


def _validate(text: str) -> None:
    try:
        data = tomllib.loads(text)
    except Exception as e:
        raise SettingsValidationError({"_toml": str(e)}) from e
    try:
        # Init kwargs outrank pydantic-settings' file/env sources, so this
        # validates the candidate document, not the on-disk file.
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
        for old in sorted(bdir.glob(path.name + ".*"))[:-_BACKUP_KEEP]:
            old.unlink()
    except Exception:
        log.exception("settings backup failed (continuing)")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
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
                  config_path: Optional[Path] = None) -> None:
    path = Path(config_path) if config_path else resolve_config_path()
    with _LOCK:
        doc = _load_doc(path)
        for dotted, value in changes.items():
            _set_dotted(doc, dotted, value)
        text = tomlkit.dumps(doc)
        _validate(text)
        _backup(path)
        _atomic_write(path, text)
    log.info("settings updated: %s", ", ".join(sorted(changes)))


def read_sections(prefixes: tuple[str, ...],
                  config_path: Optional[Path] = None) -> dict:
    path = Path(config_path) if config_path else resolve_config_path()
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if k in prefixes}
