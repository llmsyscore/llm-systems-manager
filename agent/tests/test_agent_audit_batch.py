# Agent hardening batch: threadpool handlers, log tail across rotation, reload
# re-wiring, self-update exclusivity, env float parsing, token file perms.
from __future__ import annotations

import importlib.util
import os
import re
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]
AGENT_PY = AGENT_ROOT / "llm-systems-agent.py"
SRC = AGENT_PY.read_text()


def _extract(name: str) -> str:
    m = re.search(rf"^(?:async )?def {name}\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


def _exec(name: str, ns: dict) -> dict:
    exec(compile(_extract(name), str(AGENT_PY), "exec"), ns)
    return ns


def _load_utils():
    spec = importlib.util.spec_from_file_location("agent_utils_under_test", AGENT_ROOT / "_utils.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ── #674 / #683: blocking handlers must not be coroutines ─────────────────

def _handler_def(route: str, fn: str) -> str:
    m = re.search(rf'@app\.(?:get|post)\("{re.escape(route)}"\)\n((?:async )?def {fn}\()', SRC)
    assert m, f"route {route} → {fn}() not found"
    return m.group(1)


def test_metrics_handler_runs_in_threadpool():
    assert not _handler_def("/metrics", "metrics").startswith("async ")


def test_config_reload_handler_runs_in_threadpool():
    assert not _handler_def("/config/reload", "reload_config").startswith("async ")


# ── #675: log tail follows rotation ───────────────────────────────────────

def _tail_ns():
    import json
    import time as _time
    ns = {"json": json, "os": os,
          "time": SimpleNamespace(time=_time.time, sleep=lambda s: None)}
    _exec("_sse_frame", ns)
    _exec("_tail_stream", ns)
    return _exec("_tail_log_lines", ns)


def _next_line(gen, budget=50):
    for _ in range(budget):
        frame = next(gen)
        if b'"line"' in frame:
            return frame
    return None


def test_log_tail_delivers_lines_after_rotation(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("old\n")
    gen = _tail_ns()["_tail_log_lines"](str(log))
    with open(log, "a") as f:
        f.write("before\n")
    assert _next_line(gen) == b'data: {"line": "before"}\n\n'
    os.rename(log, tmp_path / "agent.log.1")
    log.write_text("")
    with open(log, "a") as f:
        f.write("after\n")
    assert _next_line(gen) == b'data: {"line": "after"}\n\n'


def test_log_tail_delivers_lines_after_truncation(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("x" * 100 + "\n")
    gen = _tail_ns()["_tail_log_lines"](str(log))
    log.write_text("")
    with open(log, "a") as f:
        f.write("fresh\n")
    assert _next_line(gen) == b'data: {"line": "fresh"}\n\n'


# ── #680: LSA_* env override typing ──────────────────────────────────────

@pytest.fixture
def env_override():
    import math
    return _exec("_env_override_value", {"sys": __import__("sys"), "math": math})["_env_override_value"]


def test_env_override_float_key_parses_float(env_override):
    v = env_override("METRIC_FLUSH_INTERVAL_S", 30.0, "30.5")
    assert isinstance(v, float) and v == 30.5


def test_env_override_int_key_accepts_decimal(env_override):
    v = env_override("POLL_INTERVAL_S", 5, "2.5")
    assert isinstance(v, int) and v == 2


def test_env_override_unparseable_number_keeps_default(env_override, capsys):
    assert env_override("POLL_INTERVAL_S", 5, "abc") == 5
    assert env_override("METRIC_FLUSH_INTERVAL_S", 30.0, "abc") == 30.0
    assert "LSA_POLL_INTERVAL_S" in capsys.readouterr().err


def test_env_override_non_finite_keeps_default(env_override, capsys):
    assert env_override("METRIC_FLUSH_INTERVAL_S", 30.0, "nan") == 30.0
    assert env_override("METRIC_FLUSH_INTERVAL_S", 30.0, "inf") == 30.0
    assert env_override("POLL_INTERVAL_S", 5, "-inf") == 5
    assert capsys.readouterr().err.count("not a finite number") == 3


def test_env_override_uses_declared_type_over_yaml_value(env_override):
    v = env_override("METRIC_FLUSH_INTERVAL_S", 30, "45.5", ref=30.0)
    assert isinstance(v, float) and v == 45.5


def test_env_override_load_loop_passes_declared_type():
    m = re.search(r"_env_override_value\(k, cur, raw, ref=getattr\(cls, k\)\)", SRC)
    assert m, "AgentConfig.load must dispatch on the class-declared field type"


def test_env_override_bool_list_str_unchanged(env_override):
    assert env_override("X", False, "yes") is True
    assert env_override("X", True, "0") is False
    assert env_override("X", ["a"], "a | b|") == ["a", "b"]
    assert env_override("X", "s", "raw") == "raw"


# ── #681: bearer token file is never readable by others ──────────────────

@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _no_world_readable(d: Path) -> bool:
    return all(stat.S_IMODE(p.stat().st_mode) & 0o077 == 0 for p in d.rglob("*") if p.is_file())


def test_atomic_write_text_exact_mode_under_strict_umask(tmp_path):
    utils = _load_utils()
    old = os.umask(0o077)
    try:
        utils.atomic_write_text(tmp_path / "ca.pem", "x", mode=0o644)
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "ca.pem").stat().st_mode) == 0o644


def test_atomic_write_text_mode_holds_when_chmod_fails(tmp_path, monkeypatch, umask_022):
    utils = _load_utils()
    monkeypatch.setattr(utils.os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("no chmod")))
    try:
        utils.atomic_write_text(tmp_path / "token", "secret", mode=0o600)
    except OSError:
        pass
    assert _no_world_readable(tmp_path)


def _persist_ns(token_file):
    import logging
    ns = {"CONFIG": SimpleNamespace(TOKEN_FILE=str(token_file)), "Path": Path, "os": os,
          "atomic_write_text": _load_utils().atomic_write_text,
          "logger": logging.getLogger("test")}
    return _exec("_persist_token", ns)


def test_persist_token_writes_0600(tmp_path, umask_022):
    tf = tmp_path / "data" / "token"
    _persist_ns(tf)["_persist_token"]("secret")
    assert tf.read_text() == "secret"
    assert stat.S_IMODE(tf.stat().st_mode) == 0o600
    assert _no_world_readable(tmp_path)


def test_persist_token_never_leaves_readable_file_when_chmod_fails(tmp_path, monkeypatch, umask_022):
    monkeypatch.setattr(os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("no chmod")))
    _persist_ns(tmp_path / "data" / "token")["_persist_token"]("secret")
    assert _no_world_readable(tmp_path)


# ── #682: bearer compare is constant-time ────────────────────────────────

def _bearer_ns(expected="tok"):
    import hmac
    ns = {"_state": {}, "_token_provider": lambda: expected,
          "HTTPException": _HTTPException, "hmac": hmac}
    return _exec("_check_bearer", ns)["_check_bearer"]


def test_check_bearer_accepts_and_rejects():
    chk = _bearer_ns("tok")
    chk("Bearer tok")
    with pytest.raises(_HTTPException) as ei:
        chk("Bearer nope")
    assert ei.value.status_code == 403


def test_check_bearer_rejects_non_ascii_token_cleanly():
    with pytest.raises(_HTTPException) as ei:
        _bearer_ns("tok")("Bearer tök")
    assert ei.value.status_code == 403


def test_check_bearer_uses_compare_digest():
    assert "hmac.compare_digest(" in _extract("_check_bearer")


# ── #676: reload re-applies TLS verify + AE url ──────────────────────────

class _FakeMetricClient:
    def __init__(self):
        self.endpoint_url = "http://old-ae:8081/ingest"

    def update_alarm_engine_url(self, new_ae):
        self.endpoint_url = new_ae.rstrip("/") + "/ingest"


def test_reload_reapplies_tls_verify_and_ae_url(tmp_path):
    import logging
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    cfg = SimpleNamespace(MANAGER_URL="https://mgr:5000", ALARM_ENGINE_URL="http://new-ae:8081",
                          TLS_CA_FILE=str(ca), AGENT_INSTALL_DIR=str(tmp_path), _loaded_from=None)
    client = _FakeMetricClient()
    session = SimpleNamespace(verify=True)
    ns = {"CONFIG": SimpleNamespace(MANAGER_URL="http://mgr:5000", ALARM_ENGINE_URL="http://old-ae:8081"),
          "AgentConfig": SimpleNamespace(load=lambda: cfg),
          "collectors": SimpleNamespace(configure_all=lambda c: None),
          "providers": SimpleNamespace(configure_all=lambda ctx: None),
          "AgentContext": lambda **kw: kw,
          "_check_bearer": lambda a: None, "_check_stream_auth": None, "_probe_http": None,
          "_post_session": session, "_runtime_lock": threading.Lock(), "_reload_lock": threading.Lock(),
          "_state": {"ae_url_applied": "http://old-ae:8081"},
          "_now_iso": None, "_metric_client": client, "Path": Path,
          "logger": logging.getLogger("test"), "Header": lambda default=None: default}
    for fn in ("_ca_bundle_path", "_configure_manager_tls_verify", "_configure_ae_tls_verify",
               "_reload_config_locked", "reload_config"):
        _exec(fn, ns)
    out = ns["reload_config"](authorization=None)
    assert out["ok"] is True
    assert session.verify == str(ca)
    assert client.endpoint_url == "http://new-ae:8081/ingest"
    assert ns["_state"].get("ae_url_applied") in (None, "", "http://new-ae:8081")


# ── #679: self-update is exclusive for the tarball path too ──────────────

def _claim_ns():
    ns = {"_state": {}, "_runtime_lock": threading.Lock(), "HTTPException": _HTTPException}
    _exec("_claim_self_update", ns)
    _exec("_release_self_update", ns)
    return ns


def test_self_update_claim_is_exclusive():
    ns = _claim_ns()
    ns["_claim_self_update"]()
    with pytest.raises(_HTTPException) as ei:
        ns["_claim_self_update"]()
    assert ei.value.status_code == 409
    ns["_release_self_update"]()
    ns["_claim_self_update"]()


def test_self_update_streams_close_deterministically():
    for fn in ("agent_self_update", "_frozen_self_update_response"):
        assert "stream_pool.guarded_async(_gen(), pooled=False)" in _extract(fn), fn


def test_reload_config_is_serialized():
    body = _extract("reload_config")
    assert "with _reload_lock:" in body


def test_tarball_self_update_path_claims_and_releases():
    body = _extract("agent_self_update")
    frozen = _extract("_frozen_self_update_response")
    tarball_part = body.split("def _gen(", 1)
    assert "_claim_self_update()" in tarball_part[0], "tarball path must claim before starting"
    assert "_release_self_update()" in tarball_part[1], "tarball generator must release"
    assert "_release_self_update()" in frozen
    assert '_state["self_update_running"] = True' not in frozen
