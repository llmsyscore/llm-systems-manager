"""Disk-backed buffered metric client for forwarding to the alarm engine.

Endpoint contract:

    POST /api/alarm/metrics/ingest
    {"host": "<hostname>", "samples": [{...}, ...]}
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import requests

try:
    from ._utils import atomic_write_bytes
except ImportError:
    try:
        from _utils import atomic_write_bytes  # type: ignore
    except ImportError:
        # Older deployments may not ship _utils.py; inline to avoid crash-loop.
        def atomic_write_bytes(path, data, mode=None):  # type: ignore[no-redef]
            from pathlib import Path as _P
            p = _P(path)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_bytes(data)
            if mode is not None:
                os.chmod(tmp, mode)
            tmp.replace(p)

logger = logging.getLogger(__name__)


_DEFAULT_FLUSH_INTERVAL = 15.0
_DEFAULT_MAX_DISK_BYTES = 20 * 1024 * 1024
_DEFAULT_MAX_MEMORY_SAMPLES = 200
_DEFAULT_BATCH_LIMIT = 500
_DEFAULT_HTTP_TIMEOUT = 10.0

# Compact the file once the consumed prefix exceeds this many bytes.
_COMPACT_THRESHOLD_BYTES = 1 * 1024 * 1024

INGEST_PATH = "/api/alarm/metrics/ingest"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class MetricStats:
    """Counters exposed for logging / inspection."""
    enqueued: int = 0
    spilled_to_disk: int = 0
    evicted_for_budget: int = 0
    flush_attempts: int = 0
    flush_successes: int = 0
    flush_failures: int = 0
    samples_posted: int = 0


@dataclass(frozen=True)
class _Claim:
    """Opaque receipt from snapshot(); passed to commit() to advance state."""
    disk_lines: int
    new_offset: int
    mem: int


def _sanitize_non_finite(obj: Any) -> Any:
    # Replace non-finite floats (inf/-inf/nan) with None throughout the sample.
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_non_finite(v) for v in obj]
    return obj


def _write_jsonl(f, samples: list[dict[str, Any]]) -> None:
    for s in samples:
        f.write(json.dumps(s, default=str))
        f.write("\n")


class BufferStore:
    """Memory deque + on-disk JSONL overflow with streaming reads.

    Not thread-safe; BufferedMetricClient holds a lock around store calls.
    snapshot()/commit() are decoupled so POSTs happen outside the lock.
    """

    def __init__(
        self,
        cache_file: Path,
        max_disk_bytes: int,
        max_memory_samples: int,
    ) -> None:
        self.cache_file = cache_file
        self.max_disk_bytes = max_disk_bytes
        self.max_memory_samples = max_memory_samples
        self._memory: deque[dict[str, Any]] = deque()
        self._offset = 0
        self._disk_count = self._scan_disk_count()
        try:
            self._disk_bytes = self.cache_file.stat().st_size
        except OSError:
            self._disk_bytes = 0
        self._evicted_total = 0

    def enqueue(self, sample: dict[str, Any]) -> tuple[int, bool, int]:
        """Append a sample. Returns (spilled_count, was_first_spill_event, evicted_count)."""
        self._memory.append(sample)
        if len(self._memory) <= self.max_memory_samples:
            return (0, False, 0)
        was_first = self._disk_count == 0 and self._offset == 0
        spill_count = len(self._memory) - (self.max_memory_samples // 2)
        spilled = [self._memory.popleft() for _ in range(spill_count)]
        evicted_before = self._evicted_total
        self._append_disk(spilled)
        evicted = self._evicted_total - evicted_before
        return (len(spilled), was_first, evicted)

    def snapshot(self, limit: int) -> tuple[list[dict[str, Any]], _Claim]:
        """Read up to `limit` oldest samples (disk first, then memory) without mutating state."""
        if limit <= 0:
            return [], _Claim(0, self._offset, 0)
        out: list[dict[str, Any]] = []
        disk_lines = 0
        new_offset = self._offset
        if self._disk_count > 0:
            try:
                with self.cache_file.open("rb") as f:
                    f.seek(self._offset)
                    for raw in f:
                        if len(out) >= limit:
                            break
                        new_offset += len(raw)
                        disk_lines += 1
                        s = raw.strip()
                        if not s:
                            continue
                        try:
                            out.append(json.loads(s))
                        except json.JSONDecodeError:
                            continue
            except FileNotFoundError:
                self._disk_count = 0
                self._offset = 0
                disk_lines = 0
                new_offset = 0
        mem_consumed = 0
        if len(out) < limit and self._memory:
            need = limit - len(out)
            mem_consumed = min(need, len(self._memory))
            for i in range(mem_consumed):
                out.append(self._memory[i])
        return out, _Claim(disk_lines, new_offset, mem_consumed)

    def commit(self, claim: _Claim) -> None:
        """Advance state after a successful POST."""
        if claim.disk_lines:
            self._offset = claim.new_offset
            self._disk_count = max(0, self._disk_count - claim.disk_lines)
        if claim.mem:
            for _ in range(claim.mem):
                if self._memory:
                    self._memory.popleft()
        if self._disk_count == 0:
            self._unlink_cache()
        elif self._offset >= _COMPACT_THRESHOLD_BYTES:
            self._compact()

    def memory_count(self) -> int:
        return len(self._memory)

    def disk_count(self) -> int:
        return self._disk_count

    def total(self) -> int:
        return self._disk_count + len(self._memory)

    def breakdown(self) -> tuple[int, int]:
        return len(self._memory), self._disk_count

    def _append_disk(self, samples: list[dict[str, Any]]) -> int:
        """Append samples; return bytes written (0 on error)."""
        try:
            with self.cache_file.open("a", encoding="utf-8") as f:
                start = f.tell()
                _write_jsonl(f, samples)
                written = f.tell() - start
        except OSError as exc:
            logger.error(
                "disk cache write failed — path=%s err=%s (samples will remain in memory only)",
                self.cache_file, exc,
            )
            return 0
        self._disk_count += len(samples)
        self._disk_bytes += written
        if self._disk_bytes > self.max_disk_bytes:
            self._enforce_budget()
        return written

    def _enforce_budget(self) -> int:
        """Drop oldest lines to fit max_disk_bytes. Returns evicted count."""
        self._compact()
        try:
            size = self.cache_file.stat().st_size
        except OSError:
            return 0
        self._disk_bytes = size
        if size <= self.max_disk_bytes:
            return 0
        try:
            with self.cache_file.open("rb") as f:
                running = size
                evicted = 0
                while running > self.max_disk_bytes:
                    line = f.readline()
                    if not line:
                        break
                    running -= len(line)
                    evicted += 1
                tail = f.read()
        except OSError:
            logger.exception("failed to read disk cache for eviction %s", self.cache_file)
            return 0
        try:
            atomic_write_bytes(self.cache_file, tail)
        except OSError:
            logger.exception("failed to rotate disk cache %s", self.cache_file)
            return 0
        if evicted:
            self._disk_count = max(0, self._disk_count - evicted)
            self._disk_bytes = len(tail)
            self._evicted_total += evicted
            logger.warning(
                "disk cache full — evicted %d oldest samples to fit %d byte budget",
                evicted, self.max_disk_bytes,
            )
        return evicted

    def _compact(self) -> None:
        """Rewrite the cache file starting from the consumed offset."""
        if self._offset == 0:
            return
        try:
            with self.cache_file.open("rb") as f:
                f.seek(self._offset)
                tail = f.read()
        except FileNotFoundError:
            self._offset = 0
            self._disk_bytes = 0
            return
        except OSError:
            logger.exception("failed to read disk cache for compaction %s", self.cache_file)
            return
        if not tail:
            self._unlink_cache()
            return
        try:
            atomic_write_bytes(self.cache_file, tail)
        except OSError:
            logger.exception("failed to compact disk cache %s", self.cache_file)
            return
        self._offset = 0
        self._disk_bytes = len(tail)

    def _unlink_cache(self) -> None:
        try:
            self.cache_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("failed to unlink disk cache %s", self.cache_file)
        self._offset = 0
        self._disk_count = 0
        self._disk_bytes = 0

    def _scan_disk_count(self) -> int:
        try:
            with self.cache_file.open("rb") as f:
                return sum(1 for _ in f)
        except FileNotFoundError:
            return 0
        except OSError:
            return 0


class BufferedMetricClient:
    """Disk-backed metric buffer with periodic POST flush. Thread-safe."""

    def __init__(
        self,
        endpoint_url: str,
        host: str,
        cache_dir: Path,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
        max_disk_bytes: int = _DEFAULT_MAX_DISK_BYTES,
        max_memory_samples: int = _DEFAULT_MAX_MEMORY_SAMPLES,
        batch_limit: int = _DEFAULT_BATCH_LIMIT,
        http_timeout: float = _DEFAULT_HTTP_TIMEOUT,
        session: Optional[requests.Session] = None,
        on_flush_success: Optional[Callable[[int], None]] = None,
        on_flush_failure: Optional[Callable[[Exception], None]] = None,
        auth_token_provider: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.host = host
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.flush_interval = float(flush_interval)
        self.batch_limit = int(batch_limit)
        self.http_timeout = float(http_timeout)

        self._session = session or requests.Session()
        self._on_flush_success = on_flush_success
        self._on_flush_failure = on_flush_failure
        # Re-evaluated per flush so token rotation takes effect without rebuild.
        self._auth_token_provider = auth_token_provider

        self._store = BufferStore(
            cache_file=self.cache_dir / "buffer.jsonl",
            max_disk_bytes=int(max_disk_bytes),
            max_memory_samples=int(max_memory_samples),
        )
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures: int = 0
        self.stats = MetricStats()

    @property
    def cache_file(self) -> Path:
        return self._store.cache_file

    @property
    def max_disk_bytes(self) -> int:
        return self._store.max_disk_bytes

    @property
    def max_memory_samples(self) -> int:
        return self._store.max_memory_samples

    def update_alarm_engine_url(self, new_ae_url: str) -> None:
        """Retarget subsequent flushes at a new alarm-engine URL; buffer is preserved."""
        new_ae = (new_ae_url or "").rstrip("/")
        if not new_ae:
            return
        with self._lock:
            self.endpoint_url = new_ae + INGEST_PATH

    def enqueue(self, sample: dict[str, Any]) -> None:
        if not isinstance(sample, dict):
            raise TypeError("sample must be a dict")
        sample = _sanitize_non_finite(sample)
        with self._lock:
            self.stats.enqueued += 1
            spilled, first_spill, evicted = self._store.enqueue(sample)
            if not spilled:
                return
            self.stats.spilled_to_disk += spilled
            self.stats.evicted_for_budget += evicted
            mem, disk = self._store.breakdown()
        if first_spill:
            logger.info(
                "disk cache created at %s — spilled %d samples (memory=%d disk=%d)",
                self._store.cache_file, spilled, mem, disk,
            )
        else:
            logger.info(
                "spilled %d samples to disk (memory=%d disk=%d total=%d)",
                spilled, mem, disk, mem + disk,
            )

    def start(self) -> None:
        """Start the background flush thread. Idempotent."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._flush_loop, name="metric-flush", daemon=True
        )
        self._thread.start()
        writable = os.access(self.cache_dir, os.W_OK)
        logger.info(
            "BufferedMetricClient started — endpoint=%s host=%s flush_interval=%.1fs "
            "max_memory_samples=%d max_disk_bytes=%d cache_dir=%s (writable=%s, "
            "existing_disk_samples=%d)",
            self.endpoint_url, self.host, self.flush_interval,
            self.max_memory_samples, self.max_disk_bytes, self.cache_dir,
            writable, self._store.disk_count(),
        )

    def stop(self, drain: bool = True) -> None:
        """Stop the flush thread. If `drain`, attempt a final flush."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.flush_interval + self.http_timeout + 1)
            self._thread = None
        if drain:
            self._flush_once()
        logger.info("BufferedMetricClient stopped — stats=%s", asdict(self.stats))

    def buffered_count(self) -> int:
        with self._lock:
            return self._store.total()

    def buffer_breakdown(self) -> tuple[int, int]:
        with self._lock:
            return self._store.breakdown()

    def _flush_loop(self) -> None:
        while not self._stop_event.wait(self.flush_interval):
            try:
                self._flush_once()
            except Exception:
                logger.exception("flush loop error")

    def _flush_once(self) -> None:
        with self._lock:
            batch, claim = self._store.snapshot(self.batch_limit)
            if not batch:
                return
            mem_now, disk_now = self._store.breakdown()

        self.stats.flush_attempts += 1
        payload = {"host": self.host, "samples": batch}

        headers = {"Content-Type": "application/json"}
        if self._auth_token_provider is not None:
            try:
                tok = self._auth_token_provider()
            except Exception:
                tok = None
            if tok:
                headers["Authorization"] = f"Bearer {tok}"

        t0 = time.perf_counter()
        try:
            resp = self._session.post(
                self.endpoint_url,
                json=payload,
                timeout=self.http_timeout,
                headers=headers,
            )
            resp.raise_for_status()
        except Exception as exc:
            self.stats.flush_failures += 1
            self._consecutive_failures += 1
            if self._consecutive_failures == 1:
                logger.warning(
                    "POST failed — endpoint=%s err=%s (buffered memory=%d disk=%d)",
                    self.endpoint_url, exc, mem_now, disk_now,
                )
            elif self._consecutive_failures % 10 == 0:
                logger.warning(
                    "POST still failing after %d attempts — endpoint=%s err=%s "
                    "(buffered memory=%d disk=%d)",
                    self._consecutive_failures, self.endpoint_url, exc,
                    mem_now, disk_now,
                )
            else:
                logger.debug(
                    "POST failed (attempt %d) — %s", self._consecutive_failures, exc,
                )
            if self._on_flush_failure:
                try:
                    self._on_flush_failure(exc)
                except Exception:
                    logger.exception("on_flush_failure callback raised")
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        sent = len(batch)
        self.stats.flush_successes += 1
        self.stats.samples_posted += sent
        prev_failures = self._consecutive_failures
        self._consecutive_failures = 0

        with self._lock:
            self._store.commit(claim)
            remaining = self._store.total()

        if prev_failures > 0:
            logger.info(
                "POST recovered after %d failures — sent=%d remaining=%d in %.0f ms",
                prev_failures, sent, remaining, elapsed_ms,
            )
        else:
            logger.info(
                "POST ok — sent=%d remaining=%d in %.0f ms",
                sent, remaining, elapsed_ms,
            )
        if self._on_flush_success:
            try:
                self._on_flush_success(sent)
            except Exception:
                logger.exception("on_flush_success callback raised")


def from_env(
    endpoint_url: str,
    host: str,
    cache_dir: Path,
    auth_token_provider: Optional[Callable[[], Optional[str]]] = None,
    session: Optional[requests.Session] = None,
) -> BufferedMetricClient:
    """Factory honouring METRIC_FLUSH_INTERVAL / METRIC_MAX_DISK_BYTES / METRIC_MAX_MEMORY / METRIC_BATCH_LIMIT / METRIC_HTTP_TIMEOUT env vars."""
    return BufferedMetricClient(
        endpoint_url=endpoint_url,
        host=host,
        cache_dir=cache_dir,
        flush_interval=_env_float("METRIC_FLUSH_INTERVAL", _DEFAULT_FLUSH_INTERVAL),
        max_disk_bytes=_env_int("METRIC_MAX_DISK_BYTES", _DEFAULT_MAX_DISK_BYTES),
        max_memory_samples=_env_int("METRIC_MAX_MEMORY", _DEFAULT_MAX_MEMORY_SAMPLES),
        batch_limit=_env_int("METRIC_BATCH_LIMIT", _DEFAULT_BATCH_LIMIT),
        http_timeout=_env_float("METRIC_HTTP_TIMEOUT", _DEFAULT_HTTP_TIMEOUT),
        auth_token_provider=auth_token_provider,
        session=session,
    )
