"""#1419: save detected llama settings and detect them at start."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_llama_mode_switch import llama

_EXAMPLE = Path(__file__).resolve().parent.parent / "agent_config.yaml.example"
_DET = {"unit": "llama-server.service", "presets_file": "/p/models.ini", "models_dir": "/models",
        "api_url": "http://127.0.0.1:9931"}


_OWNER = ("unit", "presets_file", "api_url", "models_dir")


@pytest.fixture
def env(tmp_path, monkeypatch):
    f = tmp_path / "agent_config.yaml"
    shutil.copy(_EXAMPLE, f)
    cfg = SimpleNamespace(LLAMA_ENABLED=True, LLAMA_SYSTEMD_UNIT="llama_server.service",
                          LLAMA_API_URL="http://localhost:8080", LLAMA_CONFIG_INI="", LLAMA_MODELS_DIR="",
                          LLAMA_CACHE_DIR="", _loaded_from=str(f))
    llama.set_context(SimpleNamespace(config=cfg, check_bearer=lambda *_: None))
    monkeypatch.setattr(llama, "_detected", dict(_DET))
    monkeypatch.setattr(llama, "_unit_info_invalidate", lambda: None)
    monkeypatch.setattr(llama, "_mode_probe_memo", {"ts": 0.0, "url": None, "ok": False})
    monkeypatch.setattr(llama, "_cached_models_memo", {"ts": 0.0, "key": None, "rows": []})
    return cfg, f


def test_save_writes_three_keys_and_marks_saved(env, monkeypatch):
    cfg, f = env
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"settings_source": "detected"})
    before = f.read_text()
    r = llama.llama_settings_save({})
    assert r["ok"] is True
    text = f.read_text()
    assert 'LLAMA_API_URL: "http://127.0.0.1:9931"' in text
    assert 'LLAMA_CONFIG_INI: "/p/models.ini"' in text
    assert 'LLAMA_SYSTEMD_UNIT: "llama-server.service"' in text
    assert 'LLAMA_MODELS_DIR: "/models"' in text
    keys = ("LLAMA_API_URL", "LLAMA_CONFIG_INI", "LLAMA_SYSTEMD_UNIT", "LLAMA_MODELS_DIR")
    for k in keys:
        assert sum(1 for ln in text.splitlines() if ln.startswith(k + ":")) == 1
    kept = [ln for ln in before.splitlines() if not any(ln.lstrip("# ").startswith(k + ":") for k in keys)]
    assert [ln for ln in text.splitlines() if not any(ln.startswith(k + ":") for k in keys)] == kept
    assert cfg.LLAMA_SYSTEMD_UNIT == "llama-server.service" and cfg.LLAMA_API_URL == _DET["api_url"]
    assert llama._detected == {}
    assert llama._llama_settings_source() == "saved"
    baks = list(f.parent.glob(f.name + ".*.bak"))
    assert len(baks) == 1 and baks[0].read_text() == before


def test_save_refuses_when_not_detected(env, monkeypatch):
    _cfg, f = env
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"settings_source": "saved"})
    before = f.read_text()
    r = llama.llama_settings_save({})
    assert r.status_code == 409 and json.loads(r.body)["reason"] == "not-detected"
    assert f.read_text() == before


def test_save_replaces_commented_example_line(env, monkeypatch):
    _cfg, f = env
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"settings_source": "detected"})
    llama.llama_settings_save({})
    text = f.read_text()
    assert '# LLAMA_SYSTEMD_UNIT:' not in text and '# LLAMA_CONFIG_INI:' not in text
    assert text.count("LLAMA_SYSTEMD_UNIT:") == 1
    assert "# LLAMA_BIN:" in text and "LLAMA_ENABLED: false" in text


def test_save_replaces_live_line_and_keeps_mode(env, monkeypatch):
    _cfg, f = env
    f.write_text('A: 1\nLLAMA_API_URL: "http://old:1"   # x\nB: 2')
    f.chmod(0o640)
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"settings_source": "detected"})
    llama.llama_settings_save({})
    lines = f.read_text().splitlines()
    assert lines[:3] == ['A: 1', 'LLAMA_API_URL: "http://127.0.0.1:9931"', 'B: 2']
    assert f.stat().st_mode & 0o777 == 0o640


def test_startup_detect_fills_detected_when_keys_blank(env, monkeypatch):
    monkeypatch.setattr(llama, "_detected", {})
    det = SimpleNamespace(found=True, host="0.0.0.0", port=9931, unit="u.service", presets_file="",
                          models_dir="", mode="single", managed=True)
    seen = {}
    monkeypatch.setattr(llama, "_detect_module", lambda: SimpleNamespace(
        detect=lambda cfg_port=None, **kw: seen.setdefault("port", cfg_port) and det))
    llama.llama_startup_detect()
    assert seen["port"] == 8080
    assert llama._detected["api_url"] == "http://127.0.0.1:9931" and llama._detected["unit"] == "u.service"


def test_startup_detect_never_overrides_saved(env, monkeypatch):
    cfg = env[0]
    monkeypatch.setattr(llama, "_detected", {})
    cfg.LLAMA_SYSTEMD_UNIT, cfg.LLAMA_API_URL = "mine.service", "http://h:1234"
    det = SimpleNamespace(found=True, host="", port=9931, unit="u.service", presets_file="/p/m.ini",
                          models_dir="", mode="router", managed=True)
    monkeypatch.setattr(llama, "_detect_module", lambda: SimpleNamespace(detect=lambda cfg_port=None, **kw: det))
    llama.llama_startup_detect()
    assert "unit" not in llama._detected and "api_url" not in llama._detected
    assert llama._detected["presets_file"] == "/p/m.ini"
    cfg.LLAMA_CONFIG_INI = "/mine.ini"
    monkeypatch.setattr(llama, "_detected", {})
    llama.llama_startup_detect()
    assert not any(k in llama._detected for k in _OWNER)


def test_startup_detect_skips_when_disabled(env, monkeypatch):
    cfg = env[0]
    cfg.LLAMA_ENABLED = False
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_detect_module", lambda: (_ for _ in ()).throw(AssertionError("ran")))
    llama.llama_startup_detect()
    assert llama._detected == {}


def test_single_mode_save_is_saved_and_startup_leaves_detected_empty(env, monkeypatch):
    llama._detected.clear()
    llama._detected.update({"unit": "u.service", "api_url": "http://127.0.0.1:9931", "mode": "single"})
    monkeypatch.setattr(llama, "llama_unit_info", lambda force=False: {
        "flags": ["-m", "/m.gguf"], "environment": {}, "unit": "u.service", "unit_path": "/u",
        "unit_user": "", "load_state": "loaded"})
    monkeypatch.setattr(llama, "_llama_cache_root", lambda: None)
    assert llama.llama_mode_block()["settings_source"] == "detected"
    assert llama.llama_settings_save({})["settings_source"] == "saved"
    det = SimpleNamespace(found=True, host="", port=9931, unit="u.service", presets_file="",
                          models_dir="", mode="single", managed=True)
    monkeypatch.setattr(llama, "_detect_module", lambda: SimpleNamespace(detect=lambda cfg_port=None, **kw: det))
    llama.llama_startup_detect()
    assert not any(k in llama._detected for k in _OWNER)
    assert llama._llama_settings_source("single") == "saved"


def test_router_mode_blank_presets_stays_detected(env, monkeypatch):
    cfg = env[0]
    cfg.LLAMA_SYSTEMD_UNIT, cfg.LLAMA_API_URL = "u.service", "http://h:1"
    monkeypatch.setattr(llama, "_detected", {"mode": "router"})
    assert llama._llama_settings_source("router") == "default"
    assert llama._llama_settings_source("single") == "saved"


def test_save_live_line_wins_over_commented(env, monkeypatch):
    _cfg, f = env
    f.write_text('# LLAMA_API_URL: "http://c:1"\nLLAMA_API_URL: "http://old:1"\n')
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"settings_source": "detected"})
    llama.llama_settings_save({})
    assert f.read_text().splitlines()[:2] == ['# LLAMA_API_URL: "http://c:1"', 'LLAMA_API_URL: "http://127.0.0.1:9931"']


def _det(port=8080):
    return SimpleNamespace(found=True, host="", port=port, unit="llama_server.service", presets_file="",
                           models_dir="", mode="single", managed=True)


def test_default_values_matching_detection_count_as_saved(env, monkeypatch):
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_detect_matched", set())
    monkeypatch.setattr(llama, "_detect_module", lambda: SimpleNamespace(detect=lambda cfg_port=None, **kw: _det()))
    llama.llama_startup_detect()
    assert not any(k in llama._detected for k in _OWNER)
    assert llama._llama_settings_source("single") == "saved"


def test_detected_port_differs_fills_only_api_url(env, monkeypatch):
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_detect_matched", set())
    monkeypatch.setattr(llama, "_detect_module", lambda: SimpleNamespace(detect=lambda cfg_port=None, **kw: _det(9931)))
    llama.llama_startup_detect()
    assert llama._detected["api_url"] == "http://127.0.0.1:9931" and "unit" not in llama._detected
    assert llama._llama_settings_source("single") == "detected"


def test_start_background_detects_before_sse(env, monkeypatch):
    order = []
    monkeypatch.setattr(llama, "llama_startup_detect", lambda: order.append("detect"))
    monkeypatch.setattr(llama, "_maybe_start_sse_listener", lambda: order.append("sse"))
    cfg = env[0]
    cfg.LLAMA_ENABLED = False
    llama.start_background()
    assert order == ["detect", "sse"]


def test_default_url_spelled_127_counts_as_open(env, monkeypatch):
    cfg, _ = env
    cfg.LLAMA_API_URL = "http://127.0.0.1:8080/"
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_detect_matched", set())
    monkeypatch.setattr(llama, "_detect_module", lambda: SimpleNamespace(detect=lambda cfg_port=None, **kw: _det(9931)))
    llama.llama_startup_detect()
    assert llama._detected["api_url"] == "http://127.0.0.1:9931"


def test_effective_unit_prefers_detected_over_loaded_default_name(env, monkeypatch):
    cfg, _ = env
    cfg.LLAMA_SYSTEMD_UNIT = "llama_server.service"
    monkeypatch.setattr(llama, "_detected", {"unit": "llama-server.service"})
    monkeypatch.setattr(llama, "_unit_show", lambda u: {"load_state": "loaded"})
    assert llama._llama_effective_unit() == "llama-server.service"
    cfg.LLAMA_SYSTEMD_UNIT = "my-llama.service"
    assert llama._llama_effective_unit() == "my-llama.service"


def test_effective_api_url_treats_127_default_as_unset(env, monkeypatch):
    cfg, _ = env
    cfg.LLAMA_API_URL = "http://127.0.0.1:8080"
    monkeypatch.setattr(llama, "_detected", {"api_url": "http://127.0.0.1:9931"})
    assert llama._llama_effective_api_url() == "http://127.0.0.1:9931"
    cfg.LLAMA_API_URL = "http://127.0.0.1:8081"
    assert llama._llama_effective_api_url() == "http://127.0.0.1:8081"


def test_auto_enable_tick_turns_llama_on_when_a_server_appears(env, monkeypatch):
    cfg, _ = env
    cfg.LLAMA_ENABLED = False
    cfg._auto_role = True
    cfg.AGENT_ROLE = "system_only"
    monkeypatch.setattr(llama, "_redetect_last", None)
    monkeypatch.setattr(llama, "_auto_enabled", False)
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_detect_matched", set())
    monkeypatch.setattr(llama, "_detect_module", lambda: SimpleNamespace(detect=lambda cfg_port=None, **kw: _det(9931)))
    started = []
    monkeypatch.setattr(llama, "_maybe_start_sse_listener", lambda: started.append(1))
    assert llama.llama_auto_enable_tick() is True
    assert cfg.LLAMA_ENABLED is True and cfg.AGENT_ROLE == "llama_host" and started == [1]
    assert llama._detected["api_url"] == "http://127.0.0.1:9931"
    assert llama.llama_auto_enable_tick() is False


def test_auto_enable_tick_is_inert_without_auto_role_or_server(env, monkeypatch):
    cfg, _ = env
    cfg.LLAMA_ENABLED = False
    monkeypatch.setattr(llama, "_redetect_last", None)
    assert llama.llama_auto_enable_tick() is False and cfg.LLAMA_ENABLED is False
    cfg._auto_role = True
    monkeypatch.setattr(llama, "_detect_module", lambda: SimpleNamespace(detect=lambda cfg_port=None, **kw: SimpleNamespace(found=False)))
    assert llama.llama_auto_enable_tick() is False and cfg.LLAMA_ENABLED is False


def test_save_persists_llama_enabled_after_auto_enable(env, monkeypatch):
    cfg, f = env
    monkeypatch.setattr(llama, "_auto_enabled", True)
    monkeypatch.setattr(llama, "_detected", {"api_url": "http://127.0.0.1:9931", "unit": "u.service"})
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"settings_source": "detected"})
    out = llama.llama_settings_save({})
    assert out["ok"] is True
    text = f.read_text()
    assert "\nLLAMA_ENABLED: true\n" in text and 'LLAMA_ENABLED: "true"' not in text


def test_save_also_writes_detected_values_that_equal_the_default(env, monkeypatch):
    cfg, f = env
    cfg.LLAMA_API_URL = "http://127.0.0.1:8080"
    monkeypatch.setattr(llama, "_detected", {"unit": "llama-server.service"})
    monkeypatch.setattr(llama, "_detect_matched", {"LLAMA_API_URL"})
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {"settings_source": "detected"})
    out = llama.llama_settings_save({})
    assert out["saved"]["LLAMA_API_URL"] == "http://127.0.0.1:8080"
    assert 'LLAMA_API_URL: "http://127.0.0.1:8080"' in f.read_text()


def test_detection_keeps_the_mode_even_when_every_setting_matches(env, monkeypatch):
    cfg, _ = env
    cfg.LLAMA_API_URL = "http://127.0.0.1:9931"
    cfg.LLAMA_SYSTEMD_UNIT = ""
    monkeypatch.setattr(llama, "_detected", {})
    monkeypatch.setattr(llama, "_detect_matched", set())
    det = SimpleNamespace(found=True, host="", port=9931, unit="", presets_file="", models_dir="",
                          mode="single", configured_by="flags", managed=False, unit_path="")
    monkeypatch.setattr(llama, "_detect_module", lambda: SimpleNamespace(detect=lambda cfg_port=None, **kw: det))
    llama.llama_startup_detect()
    assert llama._detected["mode"] == "single" and "api_url" not in llama._detected
    assert llama._llama_settings_source("single") != "detected"
