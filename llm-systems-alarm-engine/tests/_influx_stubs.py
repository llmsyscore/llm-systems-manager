"""Shared InfluxDBClient test doubles for the storage-layer suites."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.storage import influxdb_client as ic


class QApi:
    """Capture every Flux query; serve canned results per call index,
    then empty tables (Influx's shape for "no rows")."""

    def __init__(self, results=None):
        self.queries = []
        self._results = list(results or [])

    def query(self, q, org=None):
        self.queries.append(q)
        return self._results.pop(0) if self._results else []


def make_client(rollup_enabled=True, results=None):
    """InfluxDBClient built via __new__ with the attrs query_metrics and
    the rollup provisioning paths read; returns (client, shared QApi)."""
    cli = ic.InfluxDBClient.__new__(ic.InfluxDBClient)
    cli.metrics_bucket = "raw_b"
    cli.metrics_rollup_bucket = "roll_b"
    cli.org = "o"
    cli.url = "http://influx:8086"
    qapi = QApi(results)
    cli._metrics_query = qapi
    cli._rollup_query = qapi
    cli._rollup_enabled = rollup_enabled
    cli._rollup_measurement = "metrics_1m"
    cli._max_measurement = "metrics_1m_max"
    cli._rollup_read_bucket = "roll_b"
    cli._rollup_every = "1m"
    cli._admin_token = "tok"
    return cli, qapi
