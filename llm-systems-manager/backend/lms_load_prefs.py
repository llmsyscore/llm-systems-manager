"""Per-model LM Studio load preferences (#916): the load options an autotune applied, kept by the
manager because LM Studio's API cannot store per-model defaults. data/lms_load_prefs.json (0600)."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Native load keys LM Studio accepts, with the value type each takes.
KEYS: dict = {
    "context_length": int, "eval_batch_size": int, "physical_batch_size": int, "parallel": int,
    "context_checkpoints": int, "num_experts": int, "speculative_draft_max_tokens": int,
    "speculative_draft_min_tokens": int, "flash_attention": bool, "offload_kv_cache_to_gpu": bool,
    "speculative_draft_mtp": bool, "speculative_draft_simple": bool, "speculative_draft_model": str,
    "speculative_draft_min_continue_probability": float,
}
MAX_STR = 200


def clean_config(cfg: dict) -> "tuple[dict, Optional[str]]":
    """(typed config, error): unknown keys and wrong types are refused; strings coerce to their key's type."""
    out: dict = {}
    for k, v in (cfg or {}).items():
        t = KEYS.get(str(k))
        if t is None:
            return {}, f"unknown load option: {k}"
        if v is None or v == "":
            continue
        try:
            if t is bool:
                if isinstance(v, str):
                    if v.strip().lower() not in ("true", "false"):
                        raise ValueError
                    v = v.strip().lower() == "true"
                elif not isinstance(v, bool):
                    raise ValueError
            elif t is int:
                if isinstance(v, bool) or (isinstance(v, float) and v != int(v)):
                    raise ValueError
                v = int(v)
                if v < 0 or v > 100_000_000:
                    raise ValueError
            elif t is float:
                if isinstance(v, bool):
                    raise ValueError
                v = float(v)
                if not 0.0 <= v <= 1.0:
                    raise ValueError
            else:
                v = str(v).strip()[:MAX_STR]
        except (TypeError, ValueError):
            return {}, f"invalid value for {k}"
        out[str(k)] = v
    return out, None


class PrefStore:
    def __init__(self, path: "Path | str") -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        tmp = f"{self._path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    @staticmethod
    def _key(agent_id: str, model_id: str) -> str:
        return f"{agent_id}|{model_id}"

    def get(self, agent_id: str, model_id: str) -> Optional[dict]:
        with self._lock:
            rec = self._load().get(self._key(agent_id, model_id))
        return dict(rec) if isinstance(rec, dict) else None

    def list_agent(self, agent_id: str) -> dict:
        """model_id → record for one agent."""
        with self._lock:
            data = self._load()
        prefix = f"{agent_id}|"
        return {k[len(prefix):]: v for k, v in data.items() if k.startswith(prefix) and isinstance(v, dict)}

    def put(self, agent_id: str, model_id: str, config: dict, note: str = "") -> dict:
        rec = {"config": dict(config), "note": note, "ts": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            data = self._load()
            data[self._key(agent_id, model_id)] = rec
            self._save(data)
        return rec

    def delete(self, agent_id: str, model_id: str) -> bool:
        with self._lock:
            data = self._load()
            hit = data.pop(self._key(agent_id, model_id), None) is not None
            if hit:
                self._save(data)
        return hit


STORE: Any = None


def configure(path: "Path | str") -> PrefStore:
    global STORE
    STORE = PrefStore(path)
    return STORE
