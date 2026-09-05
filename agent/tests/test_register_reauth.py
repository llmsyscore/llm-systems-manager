# agent/tests/test_register_reauth.py
# #562: re-registration must present the cached bearer token, and the
# fingerprint must be reboot-stable (persisted random identity, not boot_time).
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace

AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"


def _extract_func(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", AGENT_PY.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


def _atomic_write_text(path, content, mode=None, encoding="utf-8"):
    p = Path(path)
    p.write_text(content, encoding=encoding)
    if mode is not None:
        os.chmod(p, mode)


def _identity_ns(token_file):
    cfg = SimpleNamespace(TOKEN_FILE=str(token_file), AGENT_OS="linux")
    ns = {"CONFIG": cfg, "Path": Path, "os": os,
          "atomic_write_text": _atomic_write_text,
          "logger": logging.getLogger("test"),
          "_machine_identity_value": None}
    exec(compile(_extract_func("_machine_identity"), str(AGENT_PY), "exec"), ns)
    return ns


def test_machine_identity_prefers_persisted_file(tmp_path):
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "machine-identity").write_text("persisted-id\n")
    ns = _identity_ns(tmp_path / "data" / "token")
    assert ns["_machine_identity"]() == "persisted-id"


def test_machine_identity_generates_persists_and_survives_restart(tmp_path):
    token_file = tmp_path / "data" / "token"
    first = _identity_ns(token_file)["_machine_identity"]()
    # Random secret, never sourced from readable host facts like machine-id.
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert (tmp_path / "data" / "machine-identity").read_text().strip() == first
    # Fresh namespace = process restart: reads the persisted value back.
    assert _identity_ns(token_file)["_machine_identity"]() == first


def test_machine_identity_stable_in_process_when_persist_fails(tmp_path):
    # TOKEN_FILE parent is a file, so both read and persist fail with OSError.
    (tmp_path / "data").write_text("not a directory")
    ns = _identity_ns(tmp_path / "data" / "token")
    first = ns["_machine_identity"]()
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert ns["_machine_identity"]() == first


def test_machine_identity_not_derived_from_host_facts():
    src = _extract_func("_machine_identity")
    for banned in ("/etc/machine-id", "IOPlatformUUID", "ioreg", "uname(",
                   "boot_time", "getnode"):
        assert banned not in src, \
            f"machine identity reads {banned} — guessable/shared, enables fp forgery"


def test_fingerprint_input_is_reboot_stable():
    src = _extract_func("_agent_fingerprint")
    assert "psutil.boot_time" not in src, \
        "fingerprint hashes boot_time — changes every reboot, breaking re-auth"
    assert "platform.uname" not in src, \
        "fingerprint hashes uname — changes on kernel updates, breaking re-auth"
    assert "_machine_identity" in src


def test_register_posts_carry_bearer_token():
    srcs = {name: _extract_func(name)
            for name in ("_post_registration", "registry_register_blocking")}
    posts = [m for src in srcs.values()
             for m in re.finditer(r"_post_session\.post\((.*?)\n\s+\)", src, re.DOTALL)
             if "/api/agents/register" in m.group(1)]
    assert len(posts) == 2, "expected exactly two register POSTs"
    for m in posts:
        assert "headers=" in m.group(1), \
            f"register POST missing Authorization headers: {m.group(1)[:120]}"
    assert 'reg_headers = {"Authorization": f"Bearer {cached}"} if cached else {}' \
        in srcs["registry_register_blocking"]
    assert 'headers={"Authorization": f"Bearer {tok}"}' in srcs["_post_registration"]


def test_403_handler_does_not_clobber_registration_body():
    src = _extract_func("registry_register_blocking")
    assert not re.search(r"\bbody = r\.json\(\)", src), \
        "403 handler rebinds `body`, corrupting the next registration POST"
    assert "err_body" in src


def test_agent_fingerprint_hashes_host_os_and_identity():
    import hashlib
    ns = {"CONFIG": SimpleNamespace(AGENT_HOSTNAME="h", AGENT_OS="linux"),
          "_machine_identity": lambda: "mid"}
    exec(compile(_extract_func("_agent_fingerprint"), str(AGENT_PY), "exec"), ns)
    assert ns["_agent_fingerprint"]() == "sha256:" + hashlib.sha256(b"h|linux|mid").hexdigest()


def test_status_poll_sends_fingerprint_header():
    src = _extract_func("registry_register_blocking")
    m = re.search(r"_post_session\.get\((.*?/status.*?)\n\s+\)", src, re.DOTALL)
    assert m and 'headers={"X-Agent-Fingerprint": _agent_fingerprint()}' in m.group(1)
