"""InfluxDB v2 client for persistent storage."""

import logging
from datetime import datetime, timedelta
from .._best_effort import best_effort
from .._time import now_utc
from typing import Any, Optional

from influxdb_client import InfluxDBClient as _InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS, WriteOptions

logger = logging.getLogger(__name__)


# Grains that the 1-minute rollup measurement can satisfy. "30s" is excluded
# because a 1-min rollup can't synthesize 30-second bins.
_DEFAULT_ROLLUP_GRAINS = frozenset({"1m", "5m", "10m", "30m", "1h"})


def _flux_str(value: str) -> str:
    """Escape a value for interpolation inside a double-quoted Flux string;
    covers backslash, quote, newlines, and the ${} interpolation trigger."""
    return (str(value).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("${", "\\${"))


class InfluxDBClient:
    """InfluxDB v2 client wrapper for alarm engine metric data.

    Buckets:
      - alarm_engine_metrics:        raw metric points (short retention)
      - alarm_engine_metrics_rollup: 1-min rollup of `metrics` (long retention)

    All transactional alarm-engine state (rules, channels, configs,
    deliveries, alerts + history) lives in SQLite, not here.
    """

    _ROLLUP_GRAINS = _DEFAULT_ROLLUP_GRAINS

    def __init__(
        self,
        url: str = "http://localhost:8086",
        org: str = "llm-systems-manager",
        metrics_bucket: str = "alarm_engine_metrics",
        metrics_rollup_bucket: str = "alarm_engine_metrics_rollup",
        metrics_token: str = "",
        metrics_rollup_token: str = "",
        admin_token: str = "",
        rollup_enabled: bool = True,
        rollup_measurement: str = "metrics_1m",
        rollup_every: str = "1m",
    ):
        if not metrics_token:
            raise ValueError(
                "InfluxDBClient requires a metrics token "
                "(set [influxdb.tokens].metrics in /opt/llm-systems-manager/config/llm-systems.toml)"
            )
        self.url = url
        self.org = org
        self.metrics_bucket = metrics_bucket
        self.metrics_rollup_bucket = metrics_rollup_bucket

        self._metrics_client = _InfluxDBClient(url=url, token=metrics_token, org=org)
        # Metrics: high-volume firehose (~hundreds of series × 2-30s cadence).
        # Batched async writes coalesce many points into one HTTP request,
        # dropping InfluxDB ingest CPU dramatically vs SYNCHRONOUS.
        self._metrics_write = self._metrics_client.write_api(
            write_options=WriteOptions(
                batch_size=500,
                flush_interval=2_000,   # ms
                jitter_interval=500,    # ms
                retry_interval=5_000,   # ms
                max_retries=3,
                max_retry_delay=30_000,
                exponential_base=2,
            )
        )

        # Synchronous single-point writer for the self-monitor write probe.
        self._metrics_write_sync = self._metrics_client.write_api(write_options=SYNCHRONOUS)

        self._metrics_query = self._metrics_client.query_api()

        # Rollup config — surfaced by AlarmEngineHistoryDownsampling. Read
        # path consults these in query_metrics(); writer path is the Flux
        # task ensure_rollup_task() creates inside InfluxDB itself. The
        # admin token is optional and only used by ensure_rollup_task().
        self._rollup_enabled = rollup_enabled
        self._rollup_measurement = rollup_measurement
        self._rollup_every = rollup_every
        self._admin_token = admin_token

        # Rollup bucket client: only built when a token is configured.
        # Without a token, ensure_rollup_task() can still target the rollup
        # bucket (the admin token has access), but read-path Flux queries
        # would 401. In that case we fall back to reading the rollup
        # measurement from the raw `metrics_bucket` (legacy layout).
        if metrics_rollup_token:
            self._rollup_client = _InfluxDBClient(
                url=url, token=metrics_rollup_token, org=org
            )
            self._rollup_query = self._rollup_client.query_api()
            self._rollup_read_bucket = self.metrics_rollup_bucket
        else:
            self._rollup_client = None
            self._rollup_query = self._metrics_query
            self._rollup_read_bucket = self.metrics_bucket

    def write_metric(self, point: dict[str, Any]) -> None:
        """Write a single metric data point."""
        self._metrics_write.write(bucket=self.metrics_bucket, record=point)

    def write_metric_sync(self, point: dict[str, Any]) -> None:
        """Synchronous single-point write; raises on write failure."""
        self._metrics_write_sync.write(bucket=self.metrics_bucket, record=point)

    def write_metrics_batch(self, points: list[dict[str, Any]]) -> None:
        """Write multiple metric data points in a batch."""
        if not points:
            return
        self._metrics_write.write(bucket=self.metrics_bucket, record=points)

    def query_metrics(
        self,
        source: str,
        metric_name: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 1000,
        every: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Query metric data points from InfluxDB.

        When `every` is set (a Flux duration like "30s", "1m", "5m", "30m"),
        the result is server-side downsampled via aggregateWindow(mean).
        Hostname and unit are tag columns and stay in the group key through
        aggregation, so multi-host series remain distinct after downsampling.
        Pass `every=None` (default) for full-resolution raw points.

        When the rollup feature is enabled and the requested grain is >= the
        rollup bin size, this method reads the pre-aggregated rollup
        measurement (default "metrics_1m") instead of raw "metrics". That
        cuts the on-disk scan ~12x at 5s collection cadence.
        """
        start = start or now_utc() - timedelta(hours=24)
        end = end or now_utc()
        start_ts = start.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_ts = end.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Defense-in-depth: refuse arbitrary strings as Flux durations.
        # Only allow a small allowlist that matches our get_points() tiers.
        allowed_every = {"30s", "1m", "5m", "10m", "30m", "1h"}
        if every is not None and every not in allowed_every:
            logger.warning("query_metrics: ignoring disallowed every=%r", every)
            every = None

        # Route to the rollup measurement when (a) it's enabled, (b) the
        # client requested a grain >= the rollup bin, and (c) the rollup
        # measurement is non-empty. _ROLLUP_GRAINS is anything >= "1m" in
        # the allowlist; "30s" stays on raw because the 1-min rollup can't
        # synthesize 30-second bins.
        measurement = "metrics"
        bucket = self.metrics_bucket
        query_api = self._metrics_query
        skip_aggregate = False
        if (
            self._rollup_enabled
            and every is not None
            and every in self._ROLLUP_GRAINS
        ):
            measurement = self._rollup_measurement
            bucket = self._rollup_read_bucket
            query_api = self._rollup_query
            # The rollup is already at rollup_every grain. Only aggregate
            # further if the caller asked for a coarser bin than the rollup.
            if every == self._rollup_every:
                skip_aggregate = True

        aggregate_clause = (
            f"|> aggregateWindow(every: {every}, fn: mean, createEmpty: false)"
            if every and not skip_aggregate else ""
        )

        query = f'''
            from(bucket: "{bucket}")
              |> range(start: {start_ts}, stop: {end_ts})
              |> filter(fn: (r) => r._measurement == "{measurement}")
              |> filter(fn: (r) => r.source == "{_flux_str(source)}")
              |> filter(fn: (r) => r.metric_name == "{_flux_str(metric_name)}")
              |> filter(fn: (r) => r._field == "value")
              {aggregate_clause}
              |> sort(columns: ["_time"], desc: false)
              |> limit(n: {limit})
              |> keep(columns: ["_time", "_value", "unit", "hostname"])
        '''

        try:
            tables = query_api.query(query, org=self.org)
            results = []
            for table in tables:
                for record in table.records:
                    ts = record.get_time()
                    results.append({
                        "timestamp": ts.isoformat() if ts else "",
                        "value": record.get_value(),
                        "unit": record.values.get("unit"),
                        "hostname": record.values.get("hostname"),
                    })
        except Exception as e:
            logger.error(f"Error querying metrics: {e}")
            return []

        # Rollup may be empty (task missing, just created, or admin token
        # unset). Falling back to raw avoids blank charts. The recursive
        # call sets measurement="metrics" because `every` lookups in
        # _ROLLUP_GRAINS only happen when self._rollup_enabled is True.
        if (
            not results
            and measurement == self._rollup_measurement
            and self._rollup_enabled
        ):
            logger.debug(
                "rollup %s empty for %s/%s; falling back to raw metrics",
                self._rollup_measurement, source, metric_name,
            )
            self._rollup_enabled = False
            try:
                return self.query_metrics(
                    source=source, metric_name=metric_name,
                    start=start, end=end, limit=limit, every=every,
                )
            finally:
                self._rollup_enabled = True
        return results

    def warm_metric_history(self, minutes: int = 60, prefer_rollup: bool = True
                            ) -> list[dict[str, Any]]:
        """Return every point of every (source, metric_name, hostname) series
        seen in the last *minutes* minutes. Used by the alarm-engine
        startup warm-up to pre-fill the in-memory metric cache so requests
        for the last hour return data immediately after restart — instead
        of growing one tick at a time as new samples arrive.

        Reads from the rollup (`metrics_1m`) when available — that's ~1
        point/min/series instead of ~12 — so 60 min × 500 series fits in
        ~30k points. Falls back to raw `metrics` automatically when the
        rollup is empty or disabled. Returns raw dicts (matching the
        catalog shape) so the caller can mint MetricPoint objects.
        """
        start_ts = (now_utc() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _query(measurement: str) -> list[dict[str, Any]]:
            # Rollup reads target the rollup bucket (when configured); raw
            # `metrics` always reads from the raw bucket.
            if measurement == self._rollup_measurement:
                bucket = self._rollup_read_bucket
                api = self._rollup_query
            else:
                bucket = self.metrics_bucket
                api = self._metrics_query
            flux = f'''
                from(bucket: "{bucket}")
                  |> range(start: {start_ts})
                  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "value")
                  |> sort(columns: ["_time"], desc: false)
            '''
            try:
                tables = api.query(flux, org=self.org)
            except Exception as e:
                logger.warning("warm_metric_history(%s) query failed: %s", measurement, e)
                return []
            rows: list[dict[str, Any]] = []
            for tbl in tables:
                for rec in tbl.records:
                    ts = rec.get_time()
                    rows.append({
                        "source":      rec.values.get("source") or "",
                        "metric_name": rec.values.get("metric_name") or "",
                        "value":       rec.get_value(),
                        "unit":        rec.values.get("unit"),
                        "hostname":    rec.values.get("hostname"),
                        "timestamp":   ts.isoformat() if ts else "",
                    })
            return rows

        if prefer_rollup and self._rollup_enabled:
            rows = _query(self._rollup_measurement)
            if rows:
                return rows
            logger.info("warm_metric_history: rollup empty, falling back to raw metrics")
        return _query("metrics")

    def query_metric_catalog(self, days: int = 7) -> list[dict[str, Any]]:
        """Return one record per (source, metric_name, hostname) seen in the
        last *days* days.

        Uses the natural InfluxDB series grouping (one table per unique tag
        set) — each table's last() value gives us what we need without a
        manual Flux group(). Deduplication by (source, metric_name, hostname)
        is done in Python so we get exactly one cache entry per series key.

        Called at startup to warm the in-memory cache so every series visible
        in the dropdown immediately after restart.
        """
        query = f'''
            from(bucket: "{self.metrics_bucket}")
              |> range(start: -{days}d)
              |> filter(fn: (r) => r._measurement == "metrics")
              |> filter(fn: (r) => r._field == "value")
              |> last()
        '''
        tables = list(self._metrics_query.query(query, org=self.org))
        seen: dict[tuple, dict[str, Any]] = {}
        for table in tables:
            for record in table.records:
                src = record.values.get("source", "") or ""
                mname = record.values.get("metric_name", "") or ""
                host = record.values.get("hostname") or None
                if not src or not mname:
                    continue
                key = (src, mname, host)
                if key not in seen:
                    ts = record.get_time()
                    seen[key] = {
                        "timestamp": ts.isoformat() if ts else "",
                        "source": src,
                        "metric_name": mname,
                        "value": record.get_value(),
                        "unit": record.values.get("unit") or None,
                        "hostname": host,
                    }
        return list(seen.values())

    def query_latest_metric(self, source: str, metric_name: str) -> Optional[dict[str, Any]]:
        """Get the latest metric value for a source/metric pair."""
        query = f'''
            from(bucket: "{self.metrics_bucket}")
              |> range(start: -1h)
              |> filter(fn: (r) => r._measurement == "metrics")
              |> filter(fn: (r) => r.source == "{_flux_str(source)}")
              |> filter(fn: (r) => r.metric_name == "{_flux_str(metric_name)}")
              |> filter(fn: (r) => r._field == "value")
              |> last()
              |> keep(columns: ["_time", "_value", "unit"])
        '''

        try:
            tables = self._metrics_query.query(query, org=self.org)
            for table in tables:
                for record in table.records:
                    ts = record.get_time()
                    return {
                        "timestamp": ts.isoformat() if ts else "",
                        "value": record.get_value(),
                        "unit": record.values.get("unit"),
                    }
        except Exception as e:
            logger.error(f"Error querying latest metric: {e}")
        return None

    def query_metric_statistics(
        self,
        source: str,
        metric_name: str,
        window_minutes: int = 60,
    ) -> Optional[dict[str, Any]]:
        """Query aggregated statistics for a metric."""
        query = f'''
            from(bucket: "{self.metrics_bucket}")
              |> range(start: -{window_minutes}m)
              |> filter(fn: (r) => r._measurement == "metrics")
              |> filter(fn: (r) => r.source == "{_flux_str(source)}")
              |> filter(fn: (r) => r.metric_name == "{_flux_str(metric_name)}")
              |> filter(fn: (r) => r._field == "value")
              |> stats()
              |> keep(columns: ["min", "max", "mean", "stddev"])
        '''

        try:
            tables = self._metrics_query.query(query, org=self.org)
            for table in tables:
                for record in table.records:
                    return {
                        "min": record.values.get("min"),
                        "max": record.values.get("max"),
                        "mean": record.values.get("mean"),
                        "stddev": record.values.get("stddev"),
                    }
        except Exception as e:
            logger.error(f"Error querying metric statistics: {e}")
        return None

    def ensure_rollup_task(self) -> dict[str, Any]:
        """Idempotently ensure the metrics rollup Flux task exists.

        The task runs inside InfluxDB on a fixed cadence (rollup_every),
        reads raw `metrics`, applies aggregateWindow(mean), and writes the
        result back under measurement = rollup_measurement (default
        "metrics_1m"). Read-path lookups for grains >= rollup_every then
        hit the rollup instead of scanning raw points.

        Requires an operator/admin token (tokens.admin in llm-systems.toml).
        Bucket-scoped tokens can't create tasks in InfluxDB 2.x. If the
        admin token is missing, this method:
          - logs a one-line warning with the operator instruction
          - sets self._rollup_enabled = False so the read path falls
            back to raw `metrics` scans
          - returns {"skipped": "..."} (non-fatal)

        Idempotent: existing task is left untouched. To change rollup_every,
        delete the task in the InfluxDB UI and restart.

        Returns the task dict on success or {"skipped"/"error": ...}.
        Never raises.
        """
        if not self._rollup_enabled:
            return {"skipped": "rollup_enabled is false"}

        if not self._admin_token:
            logger.warning(
                "ensure_rollup_task: tokens.admin is empty — falling back to "
                "raw scans. Create an operator token in the InfluxDB UI "
                "(Data > API Tokens > Generate > All Access) and set "
                "[influxdb.tokens] admin = '...' in llm-systems.toml to enable "
                "the metrics rollup."
            )
            self._rollup_enabled = False
            return {"skipped": "tokens.admin not set; rollup disabled at runtime"}

        # Build a one-shot admin-scoped client just for the task API call.
        admin_client = _InfluxDBClient(url=self.url, token=self._admin_token, org=self.org)
        try:
            tasks_api = admin_client.tasks_api()
            # Suffix bumped from "<measurement>_rollup" to "..._v2" so that
            # splitting the rollup into its own bucket (alarm_engine_metrics
            # → alarm_engine_metrics_rollup) creates a fresh task rather than
            # reusing the legacy one that wrote into the raw bucket.
            task_name = f"{self._rollup_measurement}_rollup_v2"

            existing = tasks_api.find_tasks(name=task_name)
            if existing:
                t = existing[0]
                logger.info(
                    "rollup task already present: name=%s id=%s status=%s every=%s",
                    t.name, t.id, t.status, getattr(t, "every", "?"),
                )
                return {"name": t.name, "id": t.id, "status": t.status,
                        "every": getattr(t, "every", None)}

            # The body Flux passed to create_task_every() must NOT include
            # the `option task = {...}` block — that's generated from the
            # name/every kwargs. range(-2m) overlaps prior windows so a
            # missed run (e.g. influxd restart) doesn't leave a hole.
            flux_body = (
                f'from(bucket: "{self.metrics_bucket}")\n'
                f'  |> range(start: -2m)\n'
                f'  |> filter(fn: (r) => r._measurement == "metrics" '
                f'and r._field == "value")\n'
                f'  |> aggregateWindow(every: {self._rollup_every}, fn: mean, '
                f'createEmpty: false)\n'
                f'  |> set(key: "_measurement", value: "{self._rollup_measurement}")\n'
                f'  |> to(bucket: "{self.metrics_rollup_bucket}")\n'
            )

            # Build the task directly via TaskCreateRequest so we can pass
            # the org by NAME. create_task_every() requires an Organization
            # object with .id, which forces an org/bucket lookup that
            # silently fails when the admin token can't list orgs or when
            # the buckets API returns Bucket objects with empty org_id
            # (observed with all-access tokens on influxdb-client 1.x).
            from influxdb_client.domain.task_create_request import TaskCreateRequest

            flux_with_option = (
                f'{flux_body}\n\n'
                f'option task = {{name: "{task_name}", every: {self._rollup_every}}}'
            )
            req = TaskCreateRequest(
                flux=flux_with_option,
                org=self.org,
                status="active",
            )
            t = tasks_api.create_task(task_create_request=req)
            logger.info(
                "created rollup task: name=%s id=%s every=%s",
                t.name, t.id, self._rollup_every,
            )
            return {"name": t.name, "id": t.id, "status": getattr(t, "status", None),
                    "every": self._rollup_every}
        except Exception as e:
            logger.error("ensure_rollup_task failed: %s", e)
            # Don't disable rollup here — the task might just be transiently
            # unreachable. If the read path finds no rollup rows, charts
            # would go blank; gate that via the read-side fallback instead.
            return {"error": str(e)}
        finally:
            with best_effort("close rollup admin client", log=logger):
                admin_client.close()

    def close(self) -> None:
        """Close all per-bucket InfluxDB clients."""
        for api in (self._metrics_write, self._metrics_write_sync):
            with best_effort("close influx write api", log=logger):
                api.close()
        clients = [self._metrics_client]
        if self._rollup_client is not None:
            clients.append(self._rollup_client)
        for client in clients:
            with best_effort("close influx client", log=logger):
                client.close()
