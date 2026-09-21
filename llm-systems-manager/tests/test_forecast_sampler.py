"""#1031 forecast sampler: gauges, counter deltas, per-model gateway series, agent points."""
import forecast_sampler as fs

HOST = "mgr"


def pt(name, value, unit="", host=HOST):
    return {"source": fs.SOURCE, "metric_name": name, "value": value, "unit": unit, "hostname": host}


def names(pushes):
    return [p["metric_name"] for batch in pushes for p in batch]


def make(read=None, enabled=lambda: True, push=None):
    out = []
    sampler = fs.Sampler(read=read or {}, push=push or out.append, hostname=HOST, enabled=enabled)
    return sampler, out


def test_disabled_pushes_nothing():
    vals = {"model_loads": 10}
    on = {"v": False}
    s, out = make(read={"model_loads": lambda: vals["model_loads"], "db_wal_bytes": lambda: 5},
                  enabled=lambda: on["v"])
    assert s.tick() == 0 and out == []
    on["v"] = True
    s.tick()
    assert names(out) == ["db_wal_bytes"]
    vals["model_loads"] = 40
    on["v"] = False
    assert s.tick() == 0 and len(out) == 1
    on["v"] = True
    s.tick()
    assert names(out[-1:]) == ["db_wal_bytes"]
    vals["model_loads"] = 43
    s.tick()
    assert pt("model_loads", 3.0) in out[-1]


def test_gauges_and_none_dropped():
    s, out = make(read={"db_manager_bytes": lambda: 1024, "db_audit_bytes": lambda: None,
                        "db_energy_bytes": lambda: "nope", "backup_age_s": lambda: 3600,
                        "backup_bytes": lambda: 2048.5, "agents_stale": lambda: 2})
    assert s.tick() == 4
    assert out[-1] == [pt("db_manager_bytes", 1024.0, "bytes"), pt("backup_age_s", 3600.0, "s"),
                       pt("backup_bytes", 2048.5, "bytes"), pt("agents_stale", 2.0, "")]


def test_counter_deltas_first_tick_and_reset():
    vals = {"model_loads": 10}
    s, out = make(read={"model_loads": lambda: vals["model_loads"]})
    s.tick()
    assert names(out) == []
    vals["model_loads"] = 14
    s.tick()
    assert out[-1] == [pt("model_loads", 4.0)]
    vals["model_loads"] = 2
    s.tick()
    assert names(out[-1:]) == []
    vals["model_loads"] = 5
    s.tick()
    assert out[-1] == [pt("model_loads", 3.0)]


def _gw(requests, errors=0, timeouts=0, failovers=0):
    return {"requests": requests, "errors": errors, "timeouts": timeouts, "failovers": failovers}


def test_gateway_per_model_top20_and_slug():
    data = {"gw": {"qwen/Qwen3-8B:Q4": _gw(10), "idle model": _gw(4)}}
    s, out = make(read={"gateway": lambda: data["gw"]})
    s.tick()
    assert names(out) == []
    data["gw"] = {"qwen/Qwen3-8B:Q4": _gw(13, errors=2), "idle model": _gw(4, errors=1)}
    s.tick()
    # the busier model leads; the quiet one still pushes all four because one of its deltas moved
    assert out[-1] == [pt("gateway_requests_qwen_Qwen3-8BQ4", 3.0), pt("gateway_errors_qwen_Qwen3-8BQ4", 2.0),
                       pt("gateway_timeouts_qwen_Qwen3-8BQ4", 0.0), pt("gateway_failovers_qwen_Qwen3-8BQ4", 0.0),
                       pt("gateway_requests_idle_model", 0.0), pt("gateway_errors_idle_model", 1.0),
                       pt("gateway_timeouts_idle_model", 0.0), pt("gateway_failovers_idle_model", 0.0)]
    data["gw"] = {f"m{i:02d}": _gw(100 + i) for i in range(25)}
    s.tick()
    data["gw"] = {f"m{i:02d}": _gw(100 + i + i) for i in range(25)}
    s.tick()
    got = names(out[-1:])
    assert len(got) == fs.MAX_MODELS * 4
    assert "gateway_requests_m24" in got and "gateway_requests_m05" in got and "gateway_requests_m04" not in got


def test_gateway_pushes_all_four_series_for_a_minute_with_no_new_request():
    """A timeout and an error landing in a minute that served no new request still get their point."""
    data = {"gw": {"m": _gw(0)}}
    s, out = make(read={"gateway": lambda: data["gw"]})
    s.tick()
    data["gw"] = {"m": _gw(1)}
    s.tick()
    assert out[-1] == [pt("gateway_requests_m", 1.0), pt("gateway_errors_m", 0.0),
                       pt("gateway_timeouts_m", 0.0), pt("gateway_failovers_m", 0.0)]
    data["gw"] = {"m": _gw(1, errors=1, timeouts=1)}
    s.tick()
    assert out[-1] == [pt("gateway_requests_m", 0.0), pt("gateway_errors_m", 1.0),
                       pt("gateway_timeouts_m", 1.0), pt("gateway_failovers_m", 0.0)]
    data["gw"] = {"m": _gw(1, errors=1, timeouts=1)}
    s.tick()
    assert names(out[-1:]) == []                       # nothing moved at all


def test_gateway_ranking_breaks_a_request_tie_on_total_activity():
    quiet = {f"m{i:02d}": _gw(0) for i in range(22)}
    data = {"gw": dict(quiet)}
    s, out = make(read={"gateway": lambda: data["gw"]})
    s.tick()
    data["gw"] = {**{f"m{i:02d}": _gw(0, errors=1) for i in range(22)}, "m21": _gw(0, errors=9)}
    s.tick()
    got = [p["metric_name"] for p in out[-1] if p["metric_name"].startswith("gateway_errors_")]
    assert len(out[-1]) == fs.MAX_MODELS * 4
    assert got[0] == "gateway_errors_m21"


def test_push_failure_is_logged_once_per_outage(caplog):
    import logging
    fail = {"v": False}

    def push(points):
        if fail["v"]:
            raise OSError("alarm engine down")

    s, _ = make(read={"db_wal_bytes": lambda: 5}, push=push)
    with caplog.at_level(logging.INFO, logger="llm-systems-manager.forecast"):
        s.tick()
        fail["v"] = True
        s.tick()
        s.tick()
        s.tick()
        fail["v"] = False
        s.tick()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 1 and "OSError" in warnings[0].getMessage()
    assert len(infos) == 1 and "recovered" in infos[0].getMessage()


def test_agent_points_use_agent_hostname():
    rows = [{"host": "rig", "gap_s": 12, "skew_s": -0.5}, {"host": "pi", "gap_s": None, "skew_s": 0.25}]
    s, out = make(read={"agents": lambda: rows})
    s.tick()
    assert out[-1] == [pt("agent_heartbeat_gap_s", 12.0, "s", "rig"), pt("agent_clock_skew_s", -0.5, "s", "rig"),
                       pt("agent_clock_skew_s", 0.25, "s", "pi")]


def test_reader_exception_is_swallowed():
    def boom():
        raise RuntimeError("no db")

    s, out = make(read={"db_manager_bytes": boom, "db_audit_bytes": lambda: 7, "agents": boom})
    assert s.tick() == 1
    assert out[-1] == [pt("db_audit_bytes", 7.0, "bytes")]


def test_push_exception_is_swallowed():
    vals = {"model_loads": 10}
    sent, fail = [], {"v": False}

    def push(points):
        if fail["v"]:
            raise OSError("alarm engine down")
        sent.append(points)

    s, _ = make(read={"model_loads": lambda: vals["model_loads"]}, push=push)
    s.tick()
    vals["model_loads"] = 14
    fail["v"] = True
    assert s.tick() == 0
    fail["v"] = False
    vals["model_loads"] = 17
    assert s.tick() == 1
    assert sent[-1] == [pt("model_loads", 3.0)]


def test_non_finite_readings_dropped():
    vals = {"v": float("nan")}
    s, out = make(read={"model_loads": lambda: vals["v"], "db_wal_bytes": lambda: float("inf"),
                        "db_audit_bytes": lambda: float("nan"), "db_manager_bytes": lambda: 8})
    s.tick()
    assert names(out) == ["db_manager_bytes"]
    vals["v"] = 5
    s.tick()
    assert names(out[-1:]) == ["db_manager_bytes"]
    vals["v"] = 9
    s.tick()
    assert pt("model_loads", 4.0) in out[-1]
    assert all(p["value"] == p["value"] and abs(p["value"]) != float("inf") for p in out[-1])


def test_unchanged_counter_pushes_zero():
    vals = {"model_loads": 14}
    s, out = make(read={"model_loads": lambda: vals["model_loads"]})
    s.tick()
    s.tick()
    assert out[-1] == [pt("model_loads", 0.0)]


def test_gateway_slug_collisions_merge_and_truncate():
    long_name = "x" * 200
    data = {"gw": {"a b": _gw(0), "a_b": _gw(0), long_name: _gw(0)}}
    s, out = make(read={"gateway": lambda: data["gw"]})
    s.tick()
    data["gw"] = {"a b": _gw(2), "a_b": _gw(3), long_name: _gw(1)}
    s.tick()
    got = {p["metric_name"]: p["value"] for p in out[-1]}
    assert len(out[-1]) == 8 and len(got) == 8
    assert got["gateway_requests_a_b"] == 5.0
    assert f"gateway_requests_{'x' * fs.MAX_SLUG}" in got
    assert max(len(n) for n in got) <= len("gateway_failovers_") + fs.MAX_SLUG


def test_gateway_model_names_are_sanitised():
    first = {"evil|model": _gw(0), 'a"b\nc': _gw(0), "modèl-日本": _gw(0), "|||": _gw(0)}
    data = {"gw": first}
    s, out = make(read={"gateway": lambda: data["gw"]})
    s.tick()
    data["gw"] = {"evil|model": _gw(1), 'a"b\nc': _gw(1), "modèl-日本": _gw(1), "|||": _gw(1)}
    s.tick()
    got = [p["metric_name"] for p in out[-1] if p["metric_name"].startswith("gateway_requests_")]
    assert sorted(got) == ["gateway_requests_a_b_c", "gateway_requests_evil_model",
                           "gateway_requests_mod_l-", "gateway_requests_model"]
    assert all(n.isascii() for n in got)


def test_gateway_sanitised_collisions_are_summed():
    data = {"gw": {"evil|model": _gw(0), "evil model": _gw(0)}}
    s, out = make(read={"gateway": lambda: data["gw"]})
    s.tick()
    data["gw"] = {"evil|model": _gw(2), "evil model": _gw(5)}
    s.tick()
    assert out[-1] == [pt("gateway_requests_evil_model", 7.0), pt("gateway_errors_evil_model", 0.0),
                       pt("gateway_timeouts_evil_model", 0.0), pt("gateway_failovers_evil_model", 0.0)]


def test_gateway_baselines_evicted_when_model_disappears():
    data = {"gw": {"m": _gw(10)}}
    s, out = make(read={"gateway": lambda: data["gw"]})
    s.tick()
    data["gw"] = {}
    s.tick()
    data["gw"] = {"m": _gw(50)}
    s.tick()
    assert names(out[-1:]) == []
    data["gw"] = {"m": _gw(52)}
    s.tick()
    assert out[-1] == [pt("gateway_requests_m", 2.0), pt("gateway_errors_m", 0.0),
                       pt("gateway_timeouts_m", 0.0), pt("gateway_failovers_m", 0.0)]


def test_wrong_container_readers_are_survived():
    s, out = make(read={"gateway": lambda: [1, 2], "agents": lambda: {"rig": {"gap_s": 1}},
                        "db_wal_bytes": lambda: 3})
    assert s.tick() == 1
    assert out[-1] == [pt("db_wal_bytes", 3.0, "bytes")]


def test_overlapping_tick_returns_zero():
    inner = []

    def push(points):
        inner.append(s.tick())

    s, _ = make(read={"db_wal_bytes": lambda: 5}, push=push)
    assert s.tick() == 1 and inner == [0]


def test_start_thread_is_none_under_pytest():
    s, _ = make()
    assert fs.start_thread(s, lambda: True) is None
