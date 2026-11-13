"""#1422: the LM Studio dashboard push carries the sample's lms.settings block."""
from __future__ import annotations

import re
import types
from pathlib import Path

_AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"
_SRC = _AGENT_PY.read_text()


class _Resp:
    ok = True
    status_code = 200


class _Session:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        return _Resp()


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ns():
    m = re.search(r"^def _push_dashboard_payload\(.*?(?=^\S)", _SRC, re.MULTILINE | re.DOTALL)
    assert m
    cfg = types.SimpleNamespace(LMS_ENABLED=True, AGENT_DESCRIPTION="", AGENT_HOSTNAME="h",
                                AGENT_USER="llmsys", MANAGER_URL="http://m")
    ns = {"Any": object, "CONFIG": cfg, "_LMS_BUSY_MARKERS": ("GENERATING",), "_token_provider": lambda: "",
          "_post_session": _Session(), "_runtime_lock": _Lock(), "_state": {},
          "logger": __import__("logging").getLogger("test")}
    exec(compile(m.group(0), str(_AGENT_PY), "exec"), ns)
    return ns


def test_payload_carries_lms_settings():
    ns = _ns()
    block = {"settings_source": "detected", "api_url": "http://127.0.0.1:1240", "lms_cmd": "/x/lms",
             "cli_found": True, "running": True, "process_user": "ana", "agent_user": "llmsys"}
    ns["_push_dashboard_payload"]({"ts": "t", "lms": {"server": {}, "settings": block}})
    assert ns["_post_session"].posts[0]["settings"] == block


def test_payload_settings_empty_when_sample_has_none():
    ns = _ns()
    ns["_push_dashboard_payload"]({"ts": "t", "lms": {}})
    assert ns["_post_session"].posts[0]["settings"] == {}
