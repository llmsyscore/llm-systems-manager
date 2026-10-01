"""#1161: the agent requires the manager's and alarm engine's certificate role once it has seen it."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"
SRC = AGENT_PY.read_text()


def _extract(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


def _ns(tmp_path, manager="https://10.0.0.1:5443", ae="https://10.0.0.1:8081") -> dict:
    ns: dict = {
        "CONFIG": SimpleNamespace(AGENT_INSTALL_DIR=str(tmp_path), MANAGER_URL=manager, ALARM_ENGINE_URL=ae),
        "Path": Path, "json": json, "os": __import__("os"), "logger": logging.getLogger("test"),
        "datetime": __import__("datetime").datetime, "timezone": __import__("datetime").timezone,
        "_ROLE_ZONE": "role.llmsys.internal",
        "_ROLE_NAMES": {"manager": "manager.role.llmsys.internal",
                        "alarm_engine": "alarm-engine.role.llmsys.internal"},
        "_tls_roles_cache": {},
        "urlsplit": __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit,
    }

    def atomic_write_text(path, content, mode=None, encoding="utf-8"):
        Path(path).write_text(content)
    ns["atomic_write_text"] = atomic_write_text
    for fn in ("_tls_roles_path", "_tls_roles_load", "_tls_role_locked", "_tls_role_lock", "_peer_role_for"):
        exec(_extract(fn), ns)
    (tmp_path / "data").mkdir(exist_ok=True)
    return ns


def test_role_names_match_the_manager_side():
    pki = (AGENT_PY.parents[1] / "llm-systems-manager" / "backend" / "_pki.py").read_text()
    assert 'ROLE_ZONE = "role.llmsys.internal"' in pki
    assert '_ROLE_ZONE = "role.llmsys.internal"' in SRC
    assert '"manager": f"manager.{_ROLE_ZONE}"' in SRC
    assert '"alarm_engine": f"alarm-engine.{_ROLE_ZONE}"' in SRC


def test_no_lock_file_means_unlocked(tmp_path):
    ns = _ns(tmp_path)
    assert ns["_tls_roles_load"]() == {}
    assert not ns["_tls_role_locked"]("manager")


def test_lock_persists_across_a_restart(tmp_path):
    ns = _ns(tmp_path)
    ns["_tls_role_lock"]("manager")
    assert ns["_tls_role_locked"]("manager") and not ns["_tls_role_locked"]("alarm_engine")
    again = _ns(tmp_path)
    assert again["_tls_role_locked"]("manager")
    assert set(json.loads((tmp_path / "data" / "tls-roles.json").read_text())) == {"manager"}


def test_unreadable_lock_file_means_all_locked(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "tls-roles.json").write_text("{broken")
    ns = _ns(tmp_path)
    assert ns["_tls_role_locked"]("manager") and ns["_tls_role_locked"]("alarm_engine")


def test_locking_an_unknown_role_is_refused(tmp_path):
    ns = _ns(tmp_path)
    try:
        ns["_tls_role_lock"]("nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_peer_role_follows_the_configured_urls(tmp_path):
    ns = _ns(tmp_path)
    f = ns["_peer_role_for"]
    assert f("https://10.0.0.1:5443/api/agents/heartbeat") == "manager"
    assert f("https://10.0.0.1:8081/api/metrics/ingest") == "alarm_engine"
    assert f("HTTPS://10.0.0.1:5443/x") == "manager"
    assert f("https://10.0.0.1:9999/x") is None
    assert f("http://10.0.0.1:5443/x") is None
    assert f("https://10.0.0.1:54430/x") is None


def test_peer_role_is_none_for_plain_http_peers(tmp_path):
    ns = _ns(tmp_path, manager="http://10.0.0.1:5000", ae="")
    assert ns["_peer_role_for"]("http://10.0.0.1:5000/x") is None


def test_a_failed_lock_write_still_locks_for_this_run(tmp_path, caplog):
    ns = _ns(tmp_path)

    def boom(path, content, mode=None, encoding="utf-8"):
        raise OSError("read-only file system")
    ns["atomic_write_text"] = boom
    with caplog.at_level(logging.WARNING, logger="test"):
        ns["_tls_role_lock"]("manager")
    assert ns["_tls_role_locked"]("manager")
    assert any("could not save" in r.getMessage() for r in caplog.records)
    assert not (tmp_path / "data" / "tls-roles.json").exists()


def test_heartbeat_does_not_send_an_unused_role_field():
    assert "tls_roles_locked" not in SRC


def test_session_and_probes_use_the_router():
    assert '_post_session.mount("https://", _RoleRouter())' in SRC
    assert "session_factory=_new_post_session" in SRC
    assert '_role_get("manager", f"{https_url}/health"' in SRC
    assert '_role_get("alarm_engine", f"{new_ae}/health"' in SRC
    assert SRC.count("_maybe_lock_peer_roles()") >= 2
