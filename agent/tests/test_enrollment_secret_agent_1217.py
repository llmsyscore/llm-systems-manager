"""#1217: the agent sends the install-time enrollment secret on registration and keeps the
value the manager hands it in the heartbeat ack."""
from __future__ import annotations

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


def _ns(tmp_path) -> dict:
    ns: dict = {"CONFIG": SimpleNamespace(ENROLLMENT_SECRET_FILE=str(tmp_path / "data" / "enrollment-secret")),
                "Path": Path, "Optional": object, "logger": logging.getLogger("test")}

    def atomic_write_text(path, content, mode=None, encoding="utf-8"):
        Path(path).write_text(content)
        if mode is not None:
            Path(path).chmod(mode)
    ns["atomic_write_text"] = atomic_write_text
    for fn in ("_load_enrollment_secret", "_persist_enrollment_secret", "_enrollment_headers"):
        exec(compile(_extract(fn), str(AGENT_PY), "exec"), ns)
    return ns


def test_missing_file_means_no_header(tmp_path):
    ns = _ns(tmp_path)
    assert ns["_load_enrollment_secret"]() is None
    assert ns["_enrollment_headers"]({"Authorization": "Bearer t"}) == {"Authorization": "Bearer t"}


def test_file_value_is_sent_stripped(tmp_path):
    ns = _ns(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "enrollment-secret").write_text("  abc123\n")
    assert ns["_enrollment_headers"]() == {"X-Enrollment-Secret": "abc123"}


def test_persist_writes_0600_and_skips_unchanged(tmp_path):
    ns = _ns(tmp_path)
    ns["_persist_enrollment_secret"]("new")
    p = tmp_path / "data" / "enrollment-secret"
    assert p.read_text() == "new" and (p.stat().st_mode & 0o777) == 0o600
    before = p.stat().st_mtime_ns
    ns["_persist_enrollment_secret"]("new")
    assert p.stat().st_mtime_ns == before


def test_blank_ack_value_leaves_the_file_alone(tmp_path):
    ns = _ns(tmp_path)
    ns["_persist_enrollment_secret"]("keep")
    ns["_persist_enrollment_secret"]("")
    assert (tmp_path / "data" / "enrollment-secret").read_text() == "keep"


def test_config_default_resolves_beside_the_token():
    assert 'ENROLLMENT_SECRET_FILE: str = ""' in SRC
    assert 'cfg.ENROLLMENT_SECRET_FILE = os.path.join(cfg.AGENT_INSTALL_DIR, "data", "enrollment-secret")' in SRC


def test_registration_posts_carry_the_header():
    body = _extract("registry_register_blocking")
    assert body.count("headers=_enrollment_headers(") >= 1
    assert 'headers=_enrollment_headers({"Authorization": f"Bearer {tok}"})' in _extract("_post_registration")


def test_403_with_enrollment_code_logs_the_fix():
    body = _extract("registry_register_blocking")
    assert 'err_body.get("code") == "enrollment_secret"' in body
    assert "sudo bash install.sh --mode 5 -- --update --no-pull --enrollment-secret <secret>" in body
    assert "/install/install.sh" not in body


def test_heartbeat_persists_the_ack_secret():
    body = _extract("heartbeat_loop")
    assert '_persist_enrollment_secret(ack.get("enrollment_secret") or "")' in body
