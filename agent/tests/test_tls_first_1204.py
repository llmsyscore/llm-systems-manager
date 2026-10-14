"""#1204: an agent that holds the CA switches to the manager's https URL before any
token-bearing call, and never falls back to plain http while the manager advertises https."""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace


AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"
SRC = AGENT_PY.read_text()


def _extract(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


def _ca_pem(tmp_path) -> str:
    key = tmp_path / "ca.key"
    crt = tmp_path / "ca.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(crt), "-days", "2", "-subj", "/CN=test-ca"], check=True, capture_output=True)
    return crt.read_text()


class _Resp:
    def __init__(self, ok=True, payload=None, status_code=None, text=None):
        self.status_code = status_code if status_code is not None else (200 if ok else 500)
        self.ok = 200 <= self.status_code < 400
        self._p = payload if payload is not None else {}
        self.text = text

    def json(self):
        if self.text is not None:
            raise ValueError("not JSON")
        return self._p


def _ns(tmp_path, manager="http://10.0.0.1:5000", ca_pem=None, info=None, probe_ok=True, resp=None):
    (tmp_path / "data").mkdir(exist_ok=True)
    if ca_pem is not None:
        (tmp_path / "data" / "tls-ca.pem").write_text(ca_pem)
    calls = {"info": 0, "probe": 0, "verify": []}
    session = SimpleNamespace(verify=True)

    def _get(url, **kw):
        calls["info"] += 1
        if resp is not None:
            return resp
        if info is None:
            raise ConnectionError("down")
        return _Resp(True, info)
    session.get = _get

    def _role_get(role, url, **kw):
        calls["probe"] += 1
        calls["verify"].append(kw.get("verify"))
        if not probe_ok:
            raise ConnectionError("tls down")
        return _Resp(True)

    ns: dict = {
        "CONFIG": SimpleNamespace(AGENT_INSTALL_DIR=str(tmp_path), MANAGER_URL=manager,
                                  TLS_CA_FILE="data/tls-ca.pem"),
        "Path": Path, "logger": logging.getLogger("test"), "subprocess": subprocess, "logging": logging,
        "_post_session": session, "_role_get": _role_get, "_runtime_lock": __import__("threading").Lock(),
        "_state": {}, "calls": calls, "_configure_manager_tls_verify": lambda: calls.__setitem__("configured", True),
        "time": SimpleNamespace(sleep=lambda s: calls.__setitem__("slept", s), time=lambda: 0.0),
        "Optional": object,
    }

    def _diag(key, msg, *args, level=logging.INFO):
        calls.setdefault("diag", []).append(key)
    ns["_diag_throttle"] = _diag
    for fn in ("_ca_bundle_path", "_ca_fingerprint_of", "_https_hold_check", "_https_switch_if_verified"):
        exec(compile(_extract(fn), str(AGENT_PY), "exec"), ns)
    return ns


def test_fingerprint_matches_openssl(tmp_path):
    pem = _ca_pem(tmp_path)
    ns = _ns(tmp_path, ca_pem=pem)
    out = subprocess.check_output(["openssl", "x509", "-noout", "-fingerprint", "-sha256",
                                   "-in", str(tmp_path / "ca.crt")], text=True)
    want = "sha256:" + out.split("=", 1)[1].strip().replace(":", "").lower()
    assert ns["_ca_fingerprint_of"](pem) == want
    assert ns["_ca_fingerprint_of"]("garbage") == ""


def test_no_ca_file_means_no_hold(tmp_path):
    ns = _ns(tmp_path)
    assert ns["_https_hold_check"]() is True
    assert ns["calls"]["info"] == 0


def test_already_https_means_no_hold(tmp_path):
    ns = _ns(tmp_path, manager="https://10.0.0.1:5443", ca_pem=_ca_pem(tmp_path))
    assert ns["_https_hold_check"]() is True
    assert ns["calls"]["info"] == 0


def test_manager_tls_off_allows_http(tmp_path):
    pem = _ca_pem(tmp_path)
    ns = _ns(tmp_path, ca_pem=pem, info={"ok": True, "ca_pem": pem, "https_url": ""})
    assert ns["_https_hold_check"]() is True
    assert ns["CONFIG"].MANAGER_URL == "http://10.0.0.1:5000"


def test_probe_success_switches_url(tmp_path):
    pem = _ca_pem(tmp_path)
    ns = _ns(tmp_path, ca_pem=pem, info={"ok": True, "ca_pem": pem, "https_url": "https://10.0.0.1:5443"})
    assert ns["_https_hold_check"]() is True
    assert ns["CONFIG"].MANAGER_URL == "https://10.0.0.1:5443"
    assert ns["calls"]["configured"] is True
    assert ns["calls"]["verify"] == [str(tmp_path / "data" / "tls-ca.pem")]


def test_probe_failure_holds(tmp_path):
    pem = _ca_pem(tmp_path)
    ns = _ns(tmp_path, ca_pem=pem, info={"ok": True, "ca_pem": pem, "https_url": "https://10.0.0.1:5443"}, probe_ok=False)
    assert ns["_https_hold_check"]() is False
    assert ns["CONFIG"].MANAGER_URL == "http://10.0.0.1:5000"
    assert "tls_first_probe" in ns["calls"]["diag"]


def test_info_unreachable_holds(tmp_path):
    ns = _ns(tmp_path, ca_pem=_ca_pem(tmp_path), info=None)
    assert ns["_https_hold_check"]() is False
    assert "tls_first_info" in ns["calls"]["diag"]


def test_different_ca_holds(tmp_path):
    mine = _ca_pem(tmp_path)
    (tmp_path / "o").mkdir()
    other = _ca_pem(tmp_path / "o")
    ns = _ns(tmp_path, ca_pem=mine, info={"ok": True, "ca_pem": other, "https_url": "https://10.0.0.1:5443"})
    assert ns["_https_hold_check"]() is False
    assert ns["calls"]["probe"] == 0
    assert "tls_first_ca_mismatch" in ns["calls"]["diag"]


def test_hold_loop_is_wired_before_token_calls():
    reg = _extract("registry_register_blocking")
    assert reg.index("_hold_for_https()") < reg.index("/api/agents/whoami")
    hb = _extract("heartbeat_loop")
    assert hb.index("_hold_for_https()") < hb.index("/api/agents/heartbeat\",")
    loop = _extract("_hold_for_https")
    assert "while not _https_hold_check():" in loop and "time.sleep(15)" in loop


def test_ca_info_not_a_dict_holds(tmp_path):
    ns = _ns(tmp_path, ca_pem=_ca_pem(tmp_path), info=["not", "a", "dict"])
    assert ns["_https_hold_check"]() is False
    assert ns["calls"]["probe"] == 0
    assert ns["CONFIG"].MANAGER_URL == "http://10.0.0.1:5000"


def test_garbled_stored_ca_holds(tmp_path):
    pem = _ca_pem(tmp_path)
    ns = _ns(tmp_path, ca_pem=pem, info={"ok": True, "ca_pem": pem, "https_url": "https://10.0.0.1:5443"})
    (tmp_path / "data" / "tls-ca.pem").write_bytes(b"\xff\xfe\x00\x81garbage\xc3\x28")
    assert ns["_https_hold_check"]() is False
    assert ns["calls"]["probe"] == 0
    assert ns["CONFIG"].MANAGER_URL == "http://10.0.0.1:5000"


def test_verify_setup_failure_restores_url(tmp_path):
    pem = _ca_pem(tmp_path)
    ns = _ns(tmp_path, ca_pem=pem, info={"ok": True, "ca_pem": pem, "https_url": "https://10.0.0.1:5443"})

    def _boom():
        raise RuntimeError("verify setup failed")
    ns["_configure_manager_tls_verify"] = _boom
    assert ns["_https_hold_check"]() is False
    assert ns["CONFIG"].MANAGER_URL == "http://10.0.0.1:5000"


def test_old_manager_404_allows_http(tmp_path):
    ns = _ns(tmp_path, ca_pem=_ca_pem(tmp_path), resp=_Resp(status_code=404, text="<html>Not Found</html>"))
    assert ns["_https_hold_check"]() is True
    assert ns["CONFIG"].MANAGER_URL == "http://10.0.0.1:5000"
    assert ns["calls"]["probe"] == 0


def test_rate_limited_holds(tmp_path):
    ns = _ns(tmp_path, ca_pem=_ca_pem(tmp_path), resp=_Resp(status_code=429, payload={"ok": False}))
    assert ns["_https_hold_check"]() is False
    assert "tls_first_info" in ns["calls"]["diag"]


def test_server_error_holds(tmp_path):
    ns = _ns(tmp_path, ca_pem=_ca_pem(tmp_path), resp=_Resp(status_code=500, text="<html>error</html>"))
    assert ns["_https_hold_check"]() is False
    assert ns["CONFIG"].MANAGER_URL == "http://10.0.0.1:5000"


def test_html_200_holds(tmp_path):
    ns = _ns(tmp_path, ca_pem=_ca_pem(tmp_path), resp=_Resp(status_code=200, text="<html>login</html>"))
    assert ns["_https_hold_check"]() is False
    assert "tls_first_info" in ns["calls"]["diag"]


def test_empty_list_200_holds(tmp_path):
    ns = _ns(tmp_path, ca_pem=_ca_pem(tmp_path), resp=_Resp(status_code=200, payload=[]))
    assert ns["_https_hold_check"]() is False
    assert ns["CONFIG"].MANAGER_URL == "http://10.0.0.1:5000"
    assert "tls_first_info" in ns["calls"]["diag"]


def test_not_ok_object_holds(tmp_path):
    ns = _ns(tmp_path, ca_pem=_ca_pem(tmp_path), info={"ok": False, "https_url": ""})
    assert ns["_https_hold_check"]() is False


def _tls_ns(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    ns: dict = {
        "CONFIG": SimpleNamespace(AGENT_INSTALL_DIR=str(tmp_path), TLS_CERT_FILE="data/tls-cert.pem",
                                  TLS_KEY_FILE="data/tls-key.pem", TLS_CA_FILE="data/tls-ca.pem",
                                  AGENT_HOSTNAME="box", AGENT_BIND_HOST="0.0.0.0", AGENT_BIND_PORT=8765),
        "Path": Path, "logger": logging.getLogger("test"), "subprocess": subprocess, "logging": logging,
        "os": __import__("os"), "time": SimpleNamespace(sleep=lambda s: None), "Optional": object,
        "_state": {}, "_runtime_lock": __import__("threading").Lock(), "_SERVED_WITH_TLS": True,
        "_request_self_shutdown": lambda: None, "hasattr": hasattr, "delattr": delattr,
        "_tls_csr_cache": {},
    }

    def atomic_write_text(path, content, mode=None, encoding="utf-8"):
        Path(path).write_text(content)
        if mode is not None:
            Path(path).chmod(mode)
    ns["atomic_write_text"] = atomic_write_text

    def _diag(key, msg, *args, level=logging.INFO):
        ns.setdefault("diag", []).append(key)
    ns["_diag_throttle"] = _diag
    ns["_tls_cert_expiry_iso"] = lambda: None
    for fn in ("_tls_paths", "_tls_next_key_path", "_tls_csr_pem", "_tls_pubkey_pem", "_tls_write_bundle"):
        exec(compile(_extract(fn), str(AGENT_PY), "exec"), ns)
    return ns


def _sign(tmp_path, csr_pem: str) -> str:
    ca_pem = _ca_pem(tmp_path)
    (tmp_path / "req.csr").write_text(csr_pem)
    subprocess.run(["openssl", "x509", "-req", "-in", str(tmp_path / "req.csr"), "-CA", str(tmp_path / "ca.crt"),
                    "-CAkey", str(tmp_path / "ca.key"), "-CAcreateserial", "-out", str(tmp_path / "leaf.crt"),
                    "-days", "1"], check=True, capture_output=True)
    return (tmp_path / "leaf.crt").read_text(), ca_pem


def test_csr_is_made_from_a_0600_pending_key_and_cached(tmp_path):
    ns = _tls_ns(tmp_path)
    csr = ns["_tls_csr_pem"]()
    assert csr and csr.startswith("-----BEGIN CERTIFICATE REQUEST-----")
    nxt = tmp_path / "data" / "tls-key.pem.next"
    assert nxt.is_file() and (nxt.stat().st_mode & 0o777) == 0o600
    assert not (tmp_path / "data" / "tls-key.pem.next.tmp").exists()
    assert ns["_tls_csr_pem"]() == csr        # second call served from the cache


def test_bundle_without_key_promotes_the_pending_key(tmp_path):
    ns = _tls_ns(tmp_path)
    csr = ns["_tls_csr_pem"]()
    cert_pem, ca_pem = _sign(tmp_path, csr)
    ns["_tls_write_bundle"]({"cert_pem": cert_pem, "ca_pem": ca_pem, "reason": "first-issue"})
    key = tmp_path / "data" / "tls-key.pem"
    assert key.is_file() and (key.stat().st_mode & 0o777) == 0o600
    assert not (tmp_path / "data" / "tls-key.pem.next").exists()
    assert (tmp_path / "data" / "tls-cert.pem").read_text() == cert_pem
    assert (tmp_path / "data" / "tls-ca.pem").read_text() == ca_pem


def test_bundle_without_key_and_without_pending_key_is_ignored(tmp_path):
    ns = _tls_ns(tmp_path)
    key = tmp_path / "data" / "tls-key.pem"
    key.write_text("OLD KEY")
    csr = ns["_tls_csr_pem"]()
    cert_pem, ca_pem = _sign(tmp_path, csr)
    (tmp_path / "data" / "tls-key.pem.next").unlink()
    ns["_tls_write_bundle"]({"cert_pem": cert_pem, "ca_pem": ca_pem})
    assert key.read_text() == "OLD KEY"
    assert not (tmp_path / "data" / "tls-cert.pem").exists()


def test_bundle_for_a_different_key_is_ignored(tmp_path):
    ns = _tls_ns(tmp_path)
    ns["_tls_csr_pem"]()
    (tmp_path / "other").mkdir()
    other = _tls_ns(tmp_path / "other")
    cert_pem, ca_pem = _sign(tmp_path / "other", other["_tls_csr_pem"]())
    ns["_tls_write_bundle"]({"cert_pem": cert_pem, "ca_pem": ca_pem})
    assert not (tmp_path / "data" / "tls-cert.pem").exists()
    assert (tmp_path / "data" / "tls-key.pem.next").is_file()


def test_bundle_with_key_still_works_like_before(tmp_path):
    ns = _tls_ns(tmp_path)
    ns["_tls_write_bundle"]({"cert_pem": "C", "key_pem": "K", "ca_pem": "A"})
    assert (tmp_path / "data" / "tls-key.pem").read_text() == "K"
    assert (tmp_path / "data" / "tls-cert.pem").read_text() == "C"


def test_heartbeat_body_carries_the_csr():
    assert '"csr_pem": _tls_csr_pem(),' in _extract("heartbeat_loop")


def _leftovers(tmp_path):
    return sorted(p.name for p in (tmp_path / "data").iterdir() if p.name.endswith(".tmp"))


def test_rotation_replaces_key_and_cert(tmp_path):
    ns = _tls_ns(tmp_path)
    (tmp_path / "data" / "tls-key.pem").write_text("OLD KEY")
    (tmp_path / "data" / "tls-cert.pem").write_text("OLD CERT")
    cert_pem, ca_pem = _sign(tmp_path, ns["_tls_csr_pem"]())
    ns["_tls_write_bundle"]({"cert_pem": cert_pem, "ca_pem": ca_pem, "reason": "rotate"})
    key = tmp_path / "data" / "tls-key.pem"
    crt = tmp_path / "data" / "tls-cert.pem"
    assert key.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    assert crt.read_text() == cert_pem and (crt.stat().st_mode & 0o777) == 0o644
    assert not (tmp_path / "data" / "tls-key.pem.next").exists()
    assert ns["_tls_csr_cache"] == {}
    assert _leftovers(tmp_path) == []


def test_no_temp_files_after_a_mismatch(tmp_path):
    ns = _tls_ns(tmp_path)
    ns["_tls_csr_pem"]()
    (tmp_path / "other").mkdir()
    other = _tls_ns(tmp_path / "other")
    cert_pem, ca_pem = _sign(tmp_path / "other", other["_tls_csr_pem"]())
    ns["_tls_write_bundle"]({"cert_pem": cert_pem, "ca_pem": ca_pem})
    assert _leftovers(tmp_path) == []
    assert ns["_tls_csr_cache"] != {}


def test_openssl_missing_returns_none(tmp_path, monkeypatch):
    ns = _tls_ns(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))
    assert ns["_tls_csr_pem"]() is None
    assert ns["diag"] == ["tls_csr"]
    assert not (tmp_path / "data" / "tls-key.pem.next").exists()
    assert _leftovers(tmp_path) == []


def test_corrupt_pending_key_is_dropped_and_regenerated(tmp_path):
    ns = _tls_ns(tmp_path)
    nxt = tmp_path / "data" / "tls-key.pem.next"
    nxt.write_text("truncated")
    assert ns["_tls_csr_pem"]() is None
    assert not nxt.exists()
    csr = ns["_tls_csr_pem"]()
    assert csr and csr.startswith("-----BEGIN CERTIFICATE REQUEST-----")
