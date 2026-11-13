"""detect_lms: LM Studio process, port and lms detection driven by fake /proc trees."""
from __future__ import annotations

import importlib.util
import json
import os
import pwd
import shlex
import sys
from pathlib import Path

_AGENT = Path(__file__).resolve().parent.parent
_FIX = Path(__file__).resolve().parent / "fixtures" / "detect_lms"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dl = _load("detect_lms", _AGENT / "install" / "detect_lms.py")

_UIDS = {"root": 0, "ana": 2001, "bob": 2002, "llmsys": 2999}
_LLMSTER = "/home/ana/.lmstudio/llmster/0.0.1/llmster"


def _fx(name):
    return (_FIX / name).read_text()


def _users(monkeypatch, tmp_path):
    """Fake passwd table with homes under tmp_path; files the test creates belong to ana."""
    ents = {}
    for name, uid in _UIDS.items():
        home = tmp_path / ("roothome" if name == "root" else f"home/{name}")
        ents[name] = pwd.struct_passwd((name, "x", uid, uid, "", str(home), "/bin/sh"))
    by_uid = {e.pw_uid: e for e in ents.values()}
    by_uid[os.getuid()] = ents["ana"]

    def getpwnam(name):
        if name not in ents:
            raise KeyError(name)
        return ents[name]

    def getpwuid(uid):
        if uid not in by_uid:
            raise KeyError(uid)
        return by_uid[uid]

    monkeypatch.setattr(dl.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(dl.pwd, "getpwuid", getpwuid)
    monkeypatch.setattr(dl.pwd, "getpwall", lambda: sorted(ents.values(), key=lambda e: e.pw_uid))
    monkeypatch.setattr(dl.shutil, "which", lambda name, *a, **kw: None)
    monkeypatch.setattr(dl, "_FIXED_LMS", ())
    return ents


def _root(tmp_path):
    root = tmp_path / "proc"
    (root / "net").mkdir(parents=True)
    (root / "net" / "tcp").write_text(_fx("net_tcp.txt"))
    (root / "net" / "tcp6").write_text(_fx("net_tcp6.txt"))
    return root


def _proc(root, pid, argv, user="ana", inodes=()):
    d = Path(root) / str(pid)
    (d / "fd").mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    uid = _UIDS[user]
    (d / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    os.symlink("pipe:[77]", d / "fd" / "0")
    for i, ino in enumerate(inodes, start=3):
        os.symlink(f"socket:[{ino}]", d / "fd" / str(i))


def _lms(home):
    p = Path(home) / ".lmstudio" / "bin" / "lms"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return str(p)


def _answers(*ports):
    return lambda port, timeout=1.0: port in ports


def test_headless_daemon_on_1234(tmp_path, monkeypatch):
    _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    _proc(root, 300, [_LLMSTER, "--daemon"], inodes=(5001,))
    _proc(root, 400, ["/usr/sbin/nginx"], user="root", inodes=(5004,))
    d = dl.detect(proc_root=str(root), probe=_answers(1234, 8080), platform="linux")
    assert (d.found, d.running, d.pid, d.port) == (True, True, 300, 1234)
    assert d.api_url == "http://127.0.0.1:1234"
    assert d.process_user == "ana" and d.candidates == [300]


def test_picks_port_answering_v1_models(tmp_path, monkeypatch):
    _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    _proc(root, 300, ["/opt/LM Studio/lm-studio"], inodes=(5002, 5003, 5009))
    d = dl.detect(proc_root=str(root), probe=_answers(1240), platform="linux")
    assert d.port == 1240 and d.api_url == "http://127.0.0.1:1240"


def test_two_owners_prefer_answering_then_agent_user(tmp_path, monkeypatch):
    _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    _proc(root, 300, [_LLMSTER], user="ana", inodes=(5001,))
    _proc(root, 500, ["/home/bob/.lmstudio/llmster/0.0.1/llmster"], user="bob", inodes=(5003,))
    d = dl.detect(agent_user="ana", proc_root=str(root), probe=_answers(1240), platform="linux")
    assert (d.pid, d.process_user, d.port) == (500, "bob", 1240)
    d = dl.detect(agent_user="bob", proc_root=str(root), probe=_answers(1234, 1240), platform="linux")
    assert (d.pid, d.process_user, d.port) == (500, "bob", 1240)
    d = dl.detect(agent_user="ana", proc_root=str(root), probe=_answers(1234, 1240), platform="linux")
    assert (d.pid, d.process_user, d.port) == (300, "ana", 1234)
    assert d.candidates == [300, 500]


def test_stopped_cli_only(tmp_path, monkeypatch):
    ents = _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    path = _lms(ents["llmsys"].pw_dir)
    d = dl.detect(agent_user="llmsys", proc_root=str(root), probe=_answers(1234), platform="linux")
    assert (d.found, d.running, d.port, d.pid) == (True, False, None, None)
    assert d.lms_cmd == path and d.lms_user == "ana" and d.api_url == ""


def test_stopped_cli_in_another_users_home(tmp_path, monkeypatch):
    ents = _users(monkeypatch, tmp_path)
    monkeypatch.setattr(dl.os, "geteuid", lambda: 0)
    root = _root(tmp_path)
    path = _lms(ents["bob"].pw_dir)
    d = dl.detect(agent_user="llmsys", proc_root=str(root), probe=_answers(), platform="linux")
    assert d.found is True and d.lms_cmd == path


def test_other_homes_scan_only_as_root(tmp_path, monkeypatch):
    ents = _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    path = _lms(ents["bob"].pw_dir)
    monkeypatch.setattr(dl.os, "geteuid", lambda: 2999)
    d = dl.detect(agent_user="llmsys", proc_root=str(root), probe=_answers(), platform="linux")
    assert d.found is False and d.lms_cmd == ""
    monkeypatch.setattr(dl.os, "geteuid", lambda: 0)
    d = dl.detect(agent_user="llmsys", proc_root=str(root), probe=_answers(), platform="linux")
    assert d.found is True and d.lms_cmd == path


def test_cmd_hint_wins(tmp_path, monkeypatch):
    ents = _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    _proc(root, 300, [_LLMSTER], inodes=(5001,))
    _lms(ents["ana"].pw_dir)
    hint = tmp_path / "custom" / "lms"
    hint.parent.mkdir()
    hint.write_text("#!/bin/sh\n")
    d = dl.detect(cmd_hint=str(hint), proc_root=str(root), probe=_answers(1234), platform="linux")
    assert d.lms_cmd == str(hint)
    d = dl.detect(cmd_hint=str(tmp_path / "missing"), proc_root=str(root), probe=_answers(1234),
                  platform="linux")
    assert d.lms_cmd == ents["ana"].pw_dir + "/.lmstudio/bin/lms"


def test_lms_in_owner_home_not_root_home(tmp_path, monkeypatch):
    ents = _users(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", ents["root"].pw_dir)
    root = _root(tmp_path)
    _lms(ents["root"].pw_dir)
    want = _lms(ents["ana"].pw_dir)
    _proc(root, 300, [_LLMSTER], user="ana", inodes=(5001,))
    d = dl.detect(proc_root=str(root), probe=_answers(1234), platform="linux")
    assert d.process_user == "ana" and d.lms_cmd == want


def test_nothing(tmp_path, monkeypatch):
    _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    _proc(root, 400, ["/usr/sbin/nginx"], user="root", inodes=(5004,))
    d = dl.detect(proc_root=str(root), probe=_answers(1234, 8080), platform="linux")
    assert d.found is False and d.running is False and d.pid is None and d.port is None
    assert d.candidates == []
    for k, v in vars(d).items():
        if isinstance(v, str):
            assert v == "", k


def test_lms_cli_process_is_not_a_server(tmp_path, monkeypatch):
    _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    _proc(root, 300, ["/home/ana/.lmstudio/bin/lms", "log", "stream"])
    d = dl.detect(proc_root=str(root), probe=_answers(1234), platform="linux")
    assert d.running is False and d.candidates == []


def test_process_without_answering_port(tmp_path, monkeypatch):
    _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    _proc(root, 300, ["/opt/LM Studio/lm-studio"], inodes=(5002,))
    d = dl.detect(proc_root=str(root), probe=_answers(), platform="linux")
    assert (d.found, d.running, d.pid, d.port, d.api_url) == (True, True, 300, None, "")


def test_unreadable_fds_probe_hint_and_defaults(tmp_path, monkeypatch):
    _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    _proc(root, 300, [_LLMSTER], user="bob")
    os.unlink(root / "300" / "fd" / "0")
    (root / "300" / "fd").rmdir()
    asked = []

    def probe(port, timeout=1.0):
        asked.append(port)
        return port == 1235
    d = dl.detect(port_hint=1300, proc_root=str(root), probe=probe, platform="linux")
    assert asked == [1300, 1234, 1235]
    assert (d.running, d.port, d.process_user) == (True, 1235, "bob")


def test_macos_fallback_uses_ps(tmp_path, monkeypatch):
    _users(monkeypatch, tmp_path)
    monkeypatch.setattr(dl, "_ps_output", lambda: _fx("ps_darwin.txt"))
    d = dl.detect(proc_root=str(tmp_path / "noproc"), probe=_answers(1234), platform="darwin")
    assert (d.running, d.port, d.api_url) == (True, 1234, "http://127.0.0.1:1234")
    assert d.process_user == "ana" and d.pid == 412 and d.candidates == [412, 415]


def test_sh_output_is_shell_safe(tmp_path, capsys, monkeypatch):
    _users(monkeypatch, tmp_path)
    root = _root(tmp_path)
    path = tmp_path / "my apps" / "it's lms"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\n")
    _proc(root, 300, [_LLMSTER], inodes=(5001,))
    det = dl.detect(cmd_hint=str(path), proc_root=str(root), probe=_answers(1234), platform="linux")
    monkeypatch.setattr(dl, "detect", lambda *a, **kw: det)
    assert dl.main(["--sh", "--port", ""]) == 0
    got = {}
    for line in capsys.readouterr().out.splitlines():
        k, _, v = line.partition("=")
        got[k] = shlex.split(v)[0] if shlex.split(v) else ""
    assert got["LMS_DETECTED_LMS_CMD"] == str(path)
    assert got["LMS_DETECTED_FOUND"] == "true" and got["LMS_DETECTED_RUNNING"] == "true"
    assert got["LMS_DETECTED_PORT"] == "1234" and got["LMS_DETECTED_PROCESS_USER"] == "ana"
    assert "LMS_DETECTED_CANDIDATES" not in got


def test_cli_passes_hints(capsys, monkeypatch):
    seen = {}

    def fake(port_hint=None, cmd_hint="", agent_user="", **kw):
        seen.update(port=port_hint, cmd=cmd_hint, user=agent_user)
        return dl.Detection()
    monkeypatch.setattr(dl, "detect", fake)
    assert dl.main(["--json", "--port", "1240", "--cmd", "/x/lms", "--agent-user", "ana"]) == 0
    assert seen == {"port": 1240, "cmd": "/x/lms", "user": "ana"}
    assert json.loads(capsys.readouterr().out)["found"] is False


def test_probe_refuses_closed_port():
    assert dl._probe_v1_models(1, timeout=0.2) is False


def test_probe_refuses_redirect():
    import http.server
    import threading
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/v1/models":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{srv.server_port}/ok")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        assert dl._probe_v1_models(srv.server_port, timeout=2.0) is False
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)
    assert hits == ["/v1/models"]


def test_cli_bad_flag_exits_0_empty(capsys, monkeypatch):
    monkeypatch.setattr(dl, "detect", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("ran")))
    assert dl.main(["--sh", "--bogus"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "LMS_DETECTED_FOUND=false" in out and "LMS_DETECTED_LMS_CMD=''" in out
    assert dl.main(["--json", "--sh"]) == 0
    assert dl.main(["--nope"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == json.loads(
        json.dumps(vars(dl.Detection())))


def test_cli_internal_error_exits_0_empty(tmp_path, capsys, monkeypatch):
    _users(monkeypatch, tmp_path)

    def boom():
        raise RuntimeError("passwd unreadable")
    monkeypatch.setattr(dl.pwd, "getpwall", boom)
    monkeypatch.setattr(dl.os, "geteuid", lambda: 0)
    monkeypatch.setattr(dl, "_scan_proc", lambda root: [])
    assert dl.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["found"] is False
    assert dl.main(["--sh"]) == 0
    assert "LMS_DETECTED_RUNNING=false" in capsys.readouterr().out.splitlines()


def test_macos_args_mentioning_lmstudio_not_a_server(tmp_path, monkeypatch):
    _users(monkeypatch, tmp_path)
    asked = []

    def run(cmd, **kw):
        asked.append(cmd)
        return dl.subprocess.CompletedProcess(cmd, 0, stdout="  901 ana      /usr/bin/tail\n", stderr="")
    monkeypatch.setattr(dl.subprocess, "run", run)
    d = dl.detect(proc_root=str(tmp_path / "noproc"), probe=_answers(1234), platform="darwin")
    assert asked == [["ps", "-axo", "pid=,user=,comm="]]
    assert d.running is False and d.candidates == []
    assert dl._is_lms("/usr/bin/tail") is False
