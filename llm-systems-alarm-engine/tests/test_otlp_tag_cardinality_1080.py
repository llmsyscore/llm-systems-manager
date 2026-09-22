"""#1080: the OTLP receiver must not mint an InfluxDB series per event."""
from __future__ import annotations

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import KeyValue

from backend.receivers import otlp_receiver as otlp
from config.unified_config import settings


def _kv(key, value):
    kv = KeyValue(key=key)
    if isinstance(value, bool):
        kv.value.bool_value = value
    elif isinstance(value, int):
        kv.value.int_value = value
    elif isinstance(value, float):
        kv.value.double_value = value
    else:
        kv.value.string_value = str(value)
    return kv


def _series(records):
    return {tuple(sorted(r["tags"].items())) for r in records}


@pytest.fixture(autouse=True)
def _fresh_policy(monkeypatch):
    otlp.reset_policy_state()
    monkeypatch.setattr(settings.alarm_engine.otlp, "tag_value_cap", 20)
    monkeypatch.setattr(settings.alarm_engine.otlp, "max_tags", 8)
    monkeypatch.setattr(settings.alarm_engine.otlp, "tag_allow", [])
    monkeypatch.setattr(settings.alarm_engine.otlp, "tag_deny", [])
    yield
    otlp.reset_policy_state()


def _logs_request(n, extra=None):
    req = ExportLogsServiceRequest()
    rl = req.resource_logs.add()
    rl.resource.attributes.extend([
        _kv("service.name", "openclaw-gateway"),
        _kv("host.name", "mac1"),
        _kv("host.id", "74B65A6B-2144"),
        _kv("process.pid", 66057),
        _kv("process.command", "/opt/homebrew/bin/node"),
    ])
    sl = rl.scope_logs.add()
    for i in range(n):
        rec = sl.log_records.add()
        rec.time_unix_nano = 1_700_000_000_000_000_000 + i
        rec.severity_number = 9
        rec.attributes.extend([
            _kv("openclaw.security.event_id", f"evt-{i}"),
            _kv("openclaw.security.action", "gateway.auth.succeeded"),
            _kv("openclaw.gateway.rpc.admission_ms", 0.01 * i),
            _kv("openclaw.remotePort", 40000 + i),
            _kv("openclaw.endpoint", f"192.168.1.59:{40000 + i}->192.168.1.73:18789"),
            _kv("gen_ai.input.messages", f"user said {i}"),
            _kv("openclaw.durationMs", str(i)),
            _kv("openclaw.token", f"tok{i}"),
        ] + (extra(i) if extra else []))
    return req


def test_unique_ids_do_not_mint_series():
    records = otlp._flatten_logs(_logs_request(1000))
    assert len(records) == 1000
    assert len(_series(records)) == 1
    tags = records[0]["tags"]
    assert tags["source"] == "openclaw-gateway"
    assert tags["hostname"] == "mac1"
    assert tags["openclaw_security_action"] == "gateway.auth.succeeded"
    assert tags["severity"] == "info"
    for gone in ("openclaw_security_event_id", "openclaw_remotePort", "openclaw_endpoint",
                 "gen_ai_input_messages", "openclaw_token", "host_id", "process_pid",
                 "process_command"):
        assert gone not in tags
    assert otlp._otlp_tags_dropped > 0


def test_numeric_attributes_become_fields():
    records = otlp._flatten_logs(_logs_request(3))
    fields = records[2]["fields"]
    assert fields["value"] == 1.0
    assert fields["openclaw_gateway_rpc_admission_ms"] == pytest.approx(0.02)
    assert fields["openclaw_durationMs"] == 2.0
    assert "openclaw_gateway_rpc_admission_ms" not in records[2]["tags"]
    assert otlp._otlp_attr_fields == 6


def test_per_key_value_cap_collapses_to_other():
    records = otlp._flatten_logs(_logs_request(50, extra=lambda i: [_kv("openclaw.reason", f"r{i}")]))
    reasons = [r["tags"]["openclaw_reason"] for r in records]
    assert reasons[:20] == [f"r{i}" for i in range(20)]
    assert set(reasons[20:]) == {"other"}
    assert len(_series(records)) == 21
    assert otlp._otlp_tags_capped == 30
    # A value seen before the cap keeps passing through.
    again = otlp._flatten_logs(_logs_request(1, extra=lambda i: [_kv("openclaw.reason", "r3")]))
    assert again[0]["tags"]["openclaw_reason"] == "r3"


def test_per_point_key_cap_keeps_allowlisted_first(monkeypatch):
    monkeypatch.setattr(settings.alarm_engine.otlp, "max_tags", 3)
    extra = lambda i: [_kv(f"openclaw.dim{d}", "x") for d in range(6)] + [_kv("gen_ai.token_type", "input")]
    records = otlp._flatten_logs(_logs_request(1, extra=extra))
    tags = records[0]["tags"]
    attr_keys = [k for k in tags if k not in ("source", "metric_name", "unit", "hostname", "severity")]
    assert len(attr_keys) == 3
    assert "gen_ai_token_type" in tags


def test_operator_allow_and_deny_lists(monkeypatch):
    monkeypatch.setattr(settings.alarm_engine.otlp, "tag_allow", ["openclaw.security.event_id"])
    monkeypatch.setattr(settings.alarm_engine.otlp, "tag_deny", ["openclaw_security_action"])
    records = otlp._flatten_logs(_logs_request(2))
    tags = records[1]["tags"]
    assert tags["openclaw_security_event_id"] == "evt-1"
    assert "openclaw_security_action" not in tags


def test_spans_keep_status_and_bounded_dims():
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.extend([_kv("service.name", "openclaw-gateway")])
    ss = rs.scope_spans.add()
    for i in range(200):
        sp = ss.spans.add()
        sp.name = "openclaw.model.call"
        sp.start_time_unix_nano = 1_700_000_000_000_000_000
        sp.end_time_unix_nano = sp.start_time_unix_nano + 5_000_000
        sp.attributes.extend([
            _kv("gen_ai.request.model", "qwen3"),
            _kv("openclaw.instanceId", f"inst-{i}"),
            _kv("openclaw.model_call.usage.total_tokens", 100 + i),
        ])
    records = otlp._flatten_spans(req)
    assert len(records) == 200
    assert len(_series(records)) == 1
    tags = records[0]["tags"]
    assert tags["gen_ai_request_model"] == "qwen3"
    assert tags["status"] == "unset"
    assert records[5]["fields"]["openclaw_model_call_usage_total_tokens"] == 105.0


def test_metrics_datapoint_attrs_and_histograms():
    req = ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    rm.resource.attributes.extend([_kv("service.name", "openclaw-gateway")])
    sm = rm.scope_metrics.add()
    m = sm.metrics.add()
    m.name = "gen_ai.client.operation.duration"
    dp = m.histogram.data_points.add()
    dp.count = 4
    dp.sum = 12.5
    dp.attributes.extend([_kv("gen_ai.operation.name", "chat"), _kv("openclaw.session.key", "abc")])
    records = otlp._flatten_metrics(req)
    assert [r["tags"]["metric_name"] for r in records] == [
        "gen_ai.client.operation.duration.sum", "gen_ai.client.operation.duration.count"]
    for r in records:
        assert r["tags"]["gen_ai_operation_name"] == "chat"
        assert "openclaw_session_key" not in r["tags"]
        assert set(r["fields"]) == {"value"}


def test_non_finite_and_bool_attributes():
    req = _logs_request(1, extra=lambda i: [_kv("openclaw.hasDeviceIdentity", True),
                                            _kv("openclaw.elapsed_ms", "nan")])
    records = otlp._flatten_logs(req)
    assert records[0]["tags"]["openclaw_hasDeviceIdentity"] == "true"
    assert "openclaw_elapsed_ms" not in records[0]["fields"]


def test_attributes_cannot_overwrite_reserved_tags():
    req = _logs_request(1, extra=lambda i: [_kv("source", "evil"), _kv("severity", "fatal"),
                                            _kv("metric_name", "x")])
    tags = otlp._flatten_logs(req)[0]["tags"]
    assert tags["source"] == "openclaw-gateway"
    assert tags["severity"] == "info"
    assert tags["metric_name"] == "openclaw-gateway.log.count"


def test_same_key_never_lands_as_both_tag_and_field():
    req = ExportLogsServiceRequest()
    rl = req.resource_logs.add()
    rl.resource.attributes.extend([_kv("service.name", "svc"), _kv("openclaw.retries", 3)])
    rec = rl.scope_logs.add().log_records.add()
    rec.attributes.extend([_kv("openclaw.retries", "abc")])
    r = otlp._flatten_logs(req)[0]
    assert r["tags"]["openclaw_retries"] == "abc"
    assert "openclaw_retries" not in r["fields"]
    # numeric override after a string keeps only the field
    rl.resource.attributes[1].value.string_value = "abc"
    rec.attributes[0].value.int_value = 7
    r = otlp._flatten_logs(req)[0]
    assert r["fields"]["openclaw_retries"] == 7.0
    assert "openclaw_retries" not in r["tags"]


@pytest.mark.parametrize("key", ["sessionid", "apikey", "openclaw.clientid", "URLPath", "openclaw.eventId"])
def test_compound_identifier_keys_are_denied(key):
    assert otlp._classify(key, settings.alarm_engine.otlp) == "deny"


@pytest.mark.parametrize("key", ["openclaw.valid", "openclaw.paid", "openclaw.grid"])
def test_id_suffix_words_stay_tags(key):
    assert otlp._classify(key, settings.alarm_engine.otlp) == "tag"
