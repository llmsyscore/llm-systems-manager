# agent/tests/test_restart_exit_code.py
# #437: brew-services systemd units are Restart=on-failure — a self-restart
# must leave a non-zero exit, or the agent stops dead instead of respawning.
from __future__ import annotations

import re
from pathlib import Path

AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"


def _extract_func(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", AGENT_PY.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


def _exit_code_with_state(state: dict) -> int:
    ns = {"_state": state}
    exec(compile(_extract_func("_restart_exit_code"), str(AGENT_PY), "exec"), ns)
    return ns["_restart_exit_code"]()


def test_exit_code_zero_by_default():
    assert _exit_code_with_state({}) == 0
    assert _exit_code_with_state({"restart_pending": False}) == 0


def test_exit_code_one_when_restart_pending():
    assert _exit_code_with_state({"restart_pending": True}) == 1


def test_main_exits_nonzero_on_restart_pending():
    src = _extract_func("main")
    assert "_restart_exit_code" in src, \
        "main() never consults _restart_exit_code() after uvicorn.run"
    assert "SystemExit" in src, "main() never raises SystemExit on restart"


def test_tls_auto_restart_marks_restart_pending():
    # The first-TLS-receipt auto-restart must set restart_pending first or a
    # brew agent dies on approval.
    text = AGENT_PY.read_text()
    m = re.search(r"if not _SERVED_WITH_TLS:.*?_th\.Thread\(target=_restart_for_tls",
                  text, re.DOTALL)
    assert m, "could not locate the TLS auto-restart block"
    assert '_state["restart_pending"] = True' in m.group(0), \
        "TLS auto-restart never sets restart_pending"


# #444: self-restart must request a programmatic uvicorn shutdown, never a
# self-SIGTERM — systemd Restart=on-failure (brew) excludes SIGTERM from
# restartable failures, and uvicorn.run never returns after a signal.
class _FakeServer:
    should_exit = False


def _shutdown_ns(server):
    ns = {"_uvicorn_server": server, "os": None, "signal": None, "Optional": None}
    exec(compile(_extract_func("_request_self_shutdown"), str(AGENT_PY), "exec"), ns)
    return ns


def test_request_self_shutdown_sets_should_exit():
    srv = _FakeServer()
    _shutdown_ns(srv)["_request_self_shutdown"]()
    assert srv.should_exit is True


def test_request_self_shutdown_falls_back_to_signal_without_server():
    import types
    kills = []
    fake_os = types.SimpleNamespace(kill=lambda *a: kills.append(a), getpid=lambda: 4242)
    fake_signal = types.SimpleNamespace(SIGTERM=15)
    ns = {"_uvicorn_server": None, "os": fake_os, "signal": fake_signal}
    exec(compile(_extract_func("_request_self_shutdown"), str(AGENT_PY), "exec"), ns)
    ns["_request_self_shutdown"]()
    assert kills == [(4242, 15)]


def test_restart_paths_use_request_self_shutdown_not_sigterm():
    for fname in ("_schedule_self_restart",):
        src = _extract_func(fname)
        assert "_request_self_shutdown" in src, f"{fname} does not use the shutdown request"
        assert "os.kill" not in src, f"{fname} still self-signals"
    text = AGENT_PY.read_text()
    m = re.search(r'def agent_restart\(.*?return \{"ok": True, "restart_eta"', text, re.DOTALL)
    assert m and "_request_self_shutdown" in m.group(0) and "os.kill" not in m.group(0)
    m = re.search(r"if not _SERVED_WITH_TLS:.*?name=\"tls-restart\"", text, re.DOTALL)
    assert m and "_request_self_shutdown" in m.group(0) and "os.kill" not in m.group(0)


def test_main_holds_server_and_exits_via_restart_code():
    src = _extract_func("main")
    assert "uvicorn.Server(uvicorn.Config(" in src, "main() no longer builds an explicit Server"
    assert re.search(r"^\s*server\.run\(\)", src, re.MULTILINE), \
        "main() never calls server.run()"
    assert not re.search(r"^\s*uvicorn\.run\(", src, re.MULTILINE), \
        "main() reverted to blocking uvicorn.run() — the published Server is dead code"
    # The server must be published BEFORE run() starts serving, or shutdown
    # requests fall back to the SIGTERM path that never respawns under brew.
    publish = src.index('globals()["_uvicorn_server"] = server')
    run_call = re.search(r"^\s*server\.run\(\)", src, re.MULTILINE).start()
    assert publish < run_call, "_uvicorn_server published after server.run()"
    assert "_restart_exit_code" in src and "SystemExit" in src


def test_main_exits_nonzero_when_startup_fails():
    # uvicorn.run()'s dropped post-check: lifespan startup failure returns
    # from run() with started=False — that must exit 3, never 0.
    src = _extract_func("main")
    m = re.search(r"if not server\.started:.*?SystemExit\((\d+)\)", src, re.DOTALL)
    assert m, "main() has no startup-failure exit after server.run()"
    assert int(m.group(1)) != 0
    # Startup-failure check must come before the restart_pending exit path.
    assert src.index("not server.started") < src.index("_restart_exit_code()")
