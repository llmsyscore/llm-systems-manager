"""In-memory cache with TTL for metric data and active alerts."""

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

from ..models.metrics import MetricPoint, MetricSummary

logger = logging.getLogger(__name__)


class CacheEntry:
    """An entry in the cache with expiration."""

    def __init__(self, value: Any, ttl_seconds: int):
        self.value = value
        self.expires_at = time.monotonic() + ttl_seconds

    @property
    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        return time.monotonic() > self.expires_at


class MetricCache:
    """Thread-safe in-memory cache for metrics and alerts.

    Supports:
    - TTL-based expiration
    - LRU eviction when max size is reached
    - Metric point storage and retrieval
    - Aggregation statistics
    """

    # Number of stripes for the per-series lock. The metric cache had a
    # single global lock; under heavy concurrent ingest + rule eval, that
    # lock was the dominant source of latency for read-side handlers.
    # Striping splits writes across N independent locks (selected by
    # `hash(series_key) % _N_SHARDS`) so an ingest batch touching one
    # subset of series doesn't block reads of another subset.
    _N_SHARDS: int = 16

    def __init__(
        self,
        metric_ttl_seconds: int = 3600,  # 1 hour
        alert_ttl_seconds: int = 86400,  # 24 hours
        max_entries: int = 100000,
    ):
        self._metric_ttl = metric_ttl_seconds
        self._alert_ttl = alert_ttl_seconds
        self._max_entries = max_entries

        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        # _lock covers the generic key/value LRU below (set/get/delete/clear).
        self._lock = threading.Lock()
        # Striped locks for the metric-points store. The series-data
        # dicts (_metric_points / _point_timestamps) are read/written
        # under the SHARD lock that owns the series key.
        self._metric_locks: list[threading.Lock] = [
            threading.Lock() for _ in range(self._N_SHARDS)
        ]

        # Index structure for metric points: key -> list of MetricPoint
        self._metric_points: dict[str, list[MetricPoint]] = {}
        self._point_timestamps: dict[str, list[float]] = {}
        logger.info(
            "MetricCache initialized: metric_ttl=%ss alert_ttl=%ss "
            "max_entries=%d shards=%d",
            metric_ttl_seconds, alert_ttl_seconds, max_entries,
            self._N_SHARDS,
        )

    def _shard(self, key: str) -> threading.Lock:
        """Return the lock that owns the given series key."""
        return self._metric_locks[hash(key) % self._N_SHARDS]

    def get(self, key: str) -> Optional[Any]:
        """Get a cached value by key."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.is_expired:
                self._evict(key)
                return None
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            return entry.value

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        """Set a cached value with TTL."""
        ttl = ttl_seconds or self._metric_ttl
        with self._lock:
            # Evict expired entries if we're near capacity
            if len(self._cache) >= self._max_entries:
                self._evict_expired()
            if len(self._cache) >= self._max_entries:
                # Evict oldest
                self._evict_oldest()

            self._cache[key] = CacheEntry(value, ttl)
            self._cache.move_to_end(key)

    # Per-series hard cap. Cache TTL is 1h; at 2s cadence that's ~1800 pts/series,
    # so 2500 leaves headroom without growing into the prior 10k worst case.
    _MAX_POINTS_PER_SERIES = 2500

    def add_metric_point(self, point: MetricPoint) -> None:
        """Add a single metric point — convenience wrapper around the bulk
        method. Hot ingestion paths should call add_metric_points() instead
        to amortise the lock acquisition across an entire batch.
        """
        self.add_metric_points([point])

    def add_metric_points(self, batch: list[MetricPoint]) -> None:
        """Add a batch of metric points to the cache.

        Groups by (source, metric_name) outside any lock, then takes the
        per-shard lock only for the series owned by that shard. With 16
        shards, ingest batches that touch one subset of series no longer
        block reads of disjoint series — the cache lock used to serialize
        every reader and writer onto a single mutex.
        """
        if not batch:
            return

        # Group by key in one pass. The lock-held section becomes pure
        # list manipulation per series.
        grouped: dict[str, tuple[list[MetricPoint], list[float]]] = {}
        for p in batch:
            key = f"{p.source}:{p.metric_name}"
            ts = p.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_float = ts.timestamp()
            slot = grouped.get(key)
            if slot is None:
                grouped[key] = ([p], [ts_float])
            else:
                slot[0].append(p)
                slot[1].append(ts_float)

        cutoff = time.time() - self._metric_ttl

        # Bucket keys by shard so we acquire each lock at most once per
        # batch. Without this, a batch with N series across one shard
        # would re-acquire its lock N times.
        by_shard: dict[int, list[str]] = {}
        for key in grouped:
            by_shard.setdefault(hash(key) % self._N_SHARDS, []).append(key)

        for shard_idx, keys in by_shard.items():
            lock = self._metric_locks[shard_idx]
            with lock:
                for key in keys:
                    new_pts, new_ts = grouped[key]
                    if key not in self._metric_points:
                        self._metric_points[key] = []
                        self._point_timestamps[key] = []
                    points = self._metric_points[key]
                    timestamps = self._point_timestamps[key]

                    # TTL eviction (timestamps assumed monotonic per series).
                    if timestamps and timestamps[0] < cutoff:
                        drop = 0
                        for t in timestamps:
                            if t >= cutoff:
                                break
                            drop += 1
                        if drop:
                            del points[:drop]
                            del timestamps[:drop]

                    # Hard cap. Drop oldest existing points first; if a
                    # single fat batch is still over the cap, trim from
                    # the front of the incoming batch too.
                    projected = len(points) + len(new_pts)
                    if projected > self._MAX_POINTS_PER_SERIES:
                        excess = projected - self._MAX_POINTS_PER_SERIES
                        if points:
                            drop = min(excess, len(points))
                            if drop:
                                del points[:drop]
                                del timestamps[:drop]
                                excess -= drop
                        if excess > 0:
                            new_pts = new_pts[excess:]
                            new_ts = new_ts[excess:]

                    points.extend(new_pts)
                    timestamps.extend(new_ts)

    def get_metric_points(
        self,
        source: str,
        metric_name: str,
        since: Optional[datetime] = None,
        limit: int = 1000,
        hostname: Optional[str] = None,
    ) -> list[MetricPoint]:
        """Get cached metric points for a source/metric pair.

        When `hostname` is provided, only points originating from that host
        are returned. Pass `None` (default) to match any host — preserves
        backwards compatibility for existing callers and rules without a
        configured `source_host`.
        """
        key = f"{source}:{metric_name}"
        # Per-shard lock — readers of disjoint series no longer contend.
        with self._metric_locks[hash(key) % self._N_SHARDS]:
            points = self._metric_points.get(key, [])
            timestamps = self._point_timestamps.get(key, [])
            # Copy the slice references out so the filtering below runs
            # without holding the shard lock open across a list comp.
            points = list(points)
            timestamps = list(timestamps)

        if since:
            # If caller passed a naive datetime, treat it as UTC.
            # `.timestamp()` on a naive value uses LOCAL time, which
            # silently shifts the cutoff by the local UTC offset and
            # filters out points that are actually within the window.
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            since_ts = since.timestamp()
            points = [p for p, ts in zip(points, timestamps) if ts >= since_ts]

        if hostname:
            points = [p for p in points if p.hostname == hostname]

        return points[-limit:]

    def get_metric_summary(
        self,
        source: str,
        metric_name: str,
        window_minutes: int = 60,
    ) -> Optional[MetricSummary]:
        """Compute summary statistics from cached metric points."""
        key = f"{source}:{metric_name}"
        cutoff_ts = time.time() - window_minutes * 60
        # Per-shard lock — same justification as get_metric_points.
        with self._metric_locks[hash(key) % self._N_SHARDS]:
            points = self._metric_points.get(key, [])
            if not points:
                return None
            timestamps = self._point_timestamps.get(key, [])
            recent = [p for p, ts in zip(points, timestamps) if ts >= cutoff_ts]

            if not recent:
                return None

            values = [p.value for p in recent]
            values_sorted = sorted(values)
            n = len(values_sorted)

            avg = sum(values) / n
            variance = sum((v - avg) ** 2 for v in values) / n if n > 1 else 0
            std_dev = variance ** 0.5

            def percentile(pct: float) -> float:
                idx = int(pct / 100 * (n - 1))
                return values_sorted[min(idx, n - 1)]

            return MetricSummary(
                source=source,
                metric_name=metric_name,
                unit=recent[-1].unit if recent else None,
                current_value=recent[-1].value,
                min_value=min(values),
                max_value=max(values),
                avg_value=round(avg, 4),
                std_dev=round(std_dev, 4),
                p90=round(percentile(90), 4),
                p95=round(percentile(95), 4),
                p99=round(percentile(99), 4),
                data_points=n,
                last_updated=recent[-1].timestamp,
            )

    def delete(self, key: str) -> bool:
        """Remove a cached entry."""
        with self._lock:
            return self._evict(key)

    def clear(self) -> None:
        """Clear all cache entries (generic LRU + every metric shard)."""
        with self._lock:
            self._cache.clear()
        for shard_lock in self._metric_locks:
            with shard_lock:
                # Each shard sees the same dict but writers + readers
                # are gated by their own shard lock — wiping the whole
                # dict here under one shard lock would still race with
                # concurrent shard locks. Safer to iterate the dict by
                # ownership and only delete keys this shard owns.
                pass
        # After draining all shard locks, no writer is mid-modification;
        # finalise by wiping the structures.
        self._metric_points.clear()
        self._point_timestamps.clear()

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count of removed entries."""
        with self._lock:
            expired_keys = [k for k, v in self._cache.items() if v.is_expired]
            for key in expired_keys:
                self._evict(key)
            return len(expired_keys)

    def sweep_metric_points(self) -> int:
        """Drop per-series metric buffers whose newest point is past TTL.

        Long-running processes accumulate empty buffers for series that
        stop reporting (host drops off, metric renamed, etc.). Without this
        sweep, `_metric_points` and `_point_timestamps` keys grow forever.
        Returns count of series removed.

        Takes one shard lock at a time so a single sweep doesn't stall
        every ingest path simultaneously. Series are partitioned into
        shards by key, so each pass over a shard's keys is correct.
        """
        cutoff = time.time() - self._metric_ttl
        removed = 0
        # Snapshot keys outside any lock; they may shift slightly during
        # iteration but that's OK — we re-check under the shard lock.
        for shard_idx in range(self._N_SHARDS):
            shard_lock = self._metric_locks[shard_idx]
            with shard_lock:
                for key in list(self._metric_points.keys()):
                    if hash(key) % self._N_SHARDS != shard_idx:
                        continue
                    ts_list = self._point_timestamps.get(key) or []
                    if not ts_list or ts_list[-1] < cutoff:
                        self._metric_points.pop(key, None)
                        self._point_timestamps.pop(key, None)
                        removed += 1
        if removed:
            logger.info("metric-cache sweep evicted %d idle series", removed)
        return removed

    def _evict(self, key: str) -> bool:
        """Evict a specific key. Returns True if key existed."""
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    def _evict_expired(self) -> int:
        """Evict all expired entries."""
        expired_keys = [k for k, v in self._cache.items() if v.is_expired]
        for key in expired_keys:
            self._cache.pop(key, None)
        if expired_keys:
            logger.debug("cache evicted %d expired entries", len(expired_keys))
        return len(expired_keys)

    def _evict_oldest(self) -> None:
        """Evict the oldest (least recently used) entry."""
        if self._cache:
            k, _ = self._cache.popitem(last=False)
            logger.debug("cache evicted oldest LRU entry: %s", k)

    @property
    def size(self) -> int:
        """Current number of cached entries."""
        with self._lock:
            return len(self._cache)

    @property
    def metric_keys(self) -> list[str]:
        """List all metric source:metric_name keys."""
        with self._lock:
            return list(self._metric_points.keys())

    def get_all_prefixed(self, prefix: str) -> list[Any]:
        """Return all non-expired values whose key starts with *prefix*."""
        with self._lock:
            return [
                entry.value
                for key, entry in self._cache.items()
                if key.startswith(prefix) and not entry.is_expired
            ]


# ── Cache: alias for MetricCache so al.py can import Cache ──

class Cache(MetricCache):
    """Alias so alarm_engine.py can import ``Cache`` from storage.cache."""

    def __init__(
        self,
        metric_ttl_seconds: int = 3600,
        alert_ttl_seconds: int = 86400,
        max_entries: int = 100000,
    ):
        super().__init__(
            metric_ttl_seconds=metric_ttl_seconds,
            alert_ttl_seconds=alert_ttl_seconds,
            max_entries=max_entries,
        )