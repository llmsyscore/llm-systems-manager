"""#1422: POST /lms/settings/save writes the detected LM Studio settings to agent_config.yaml."""
from __future__ import annotations

import json

import pytest

import test_lms_detect_runtime as _rt

det, lms = _rt.det, _rt.lms
env = _rt.env  # shared fixture, registered here by name


def _body(resp):
    return json.loads(resp.body)


@pytest.mark.parametrize("auto", [True, False])
def test_save_writes_keys_and_marks_saved(env, auto):  # noqa: F811
    if auto:
        env.cfg.LMS_ENABLED, env.cfg._auto_role, env.cfg.AGENT_ROLE = False, True, "system_only"
        assert lms.lms_auto_enable_tick() is True
    else:
        lms.lms_startup_detect()
    r = lms.lms_settings_save({})
    want = {"LMS_API_URL": "http://127.0.0.1:1240", "LMS_CMD": "/home/ana/.lmstudio/bin/lms"}
    if auto:
        want["LMS_ENABLED"] = True
    assert r == {"ok": True, "saved": want, "settings_source": "saved"}
    text = env.file.read_text()
    assert 'LMS_API_URL: "http://127.0.0.1:1240"' in text
    assert 'LMS_CMD: "/home/ana/.lmstudio/bin/lms"' in text
    assert ("LMS_ENABLED: true" in text.splitlines()) is auto
    assert lms.lms_settings_block()["settings_source"] == "saved"


def test_save_refuses_when_not_detected(env):  # noqa: F811
    before = env.file.read_text()
    r = lms.lms_settings_save({})
    assert r.status_code == 409
    assert _body(r) == {"reason": "not-detected", "detail": "There are no detected settings to save."}
    assert env.file.read_text() == before


def test_save_after_auto_enable_at_defaults_writes_enabled(env):  # noqa: F811
    env.cfg.LMS_ENABLED, env.cfg._auto_role, env.cfg.AGENT_ROLE = False, True, "system_only"
    env.fake.result = det(port=1234, api_url="http://127.0.0.1:1234", lms_cmd=env.cfg.LMS_CMD)
    assert lms.lms_auto_enable_tick() is True
    r = lms.lms_settings_save({})
    want = {"LMS_API_URL": "http://localhost:1234", "LMS_CMD": env.cfg.LMS_CMD, "LMS_ENABLED": True}
    assert r == {"ok": True, "saved": want, "settings_source": "saved"}
    lines = env.file.read_text().splitlines()
    assert "LMS_ENABLED: true" in lines
    assert 'LMS_API_URL: "http://localhost:1234"' in lines
    assert lms.lms_settings_save({}).status_code == 409


def test_save_after_startup_auto_detect_writes_enabled(env):  # noqa: F811
    env.cfg._auto_role, env.cfg.AGENT_ROLE = True, "lms_host"
    lms.mark_auto_enabled()
    env.fake.result = det(port=1234, api_url="http://127.0.0.1:1234",
                          lms_cmd="/home/llmsys/.lmstudio/bin/lms", process_user="llmsys")
    lms.lms_startup_detect()
    assert lms.lms_settings_block()["settings_source"] == "detected"
    r = lms.lms_settings_save({})
    want = {"LMS_API_URL": "http://localhost:1234", "LMS_CMD": "/home/llmsys/.lmstudio/bin/lms", "LMS_ENABLED": True}
    assert r == {"ok": True, "saved": want, "settings_source": "saved"}
    lines = env.file.read_text().splitlines()
    assert "LMS_ENABLED: true" in lines and "LMS_ENABLED: false" not in lines
    assert 'LMS_API_URL: "http://localhost:1234"' in lines
