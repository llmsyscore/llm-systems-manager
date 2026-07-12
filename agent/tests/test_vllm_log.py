# agent/tests/test_vllm_log.py
"""#125: vLLM journal log tail — argv + output shaping (no live journal)."""
from __future__ import annotations

from types import SimpleNamespace

from tests._vllm_load import load_vllm

vllm = load_vllm()


class _Ctx(SimpleNamespace):
    def check_bearer(self, *_):
        pass

    def check_stream_auth(self, *_):
        pass


def _mk_ctx():
    cfg = SimpleNamespace(VLLM_ENABLED=True, VLLM_SYSTEMD_UNIT="vllm.service")
    return _Ctx(config=cfg)


def test_tail_argv_and_lines(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="l1\nl2\n", stderr="")

    monkeypatch.setattr(vllm, "_ctx", _mk_ctx())
    monkeypatch.setattr(vllm.subprocess, "run", fake_run)
    out = vllm.vllm_log_tail(authorization="Bearer x")
    assert out == {"ok": True, "lines": ["l1", "l2"]}
    assert seen["argv"] == ["journalctl", "-u", "vllm.service", "-n", "100",
                            "--no-pager", "-o", "cat"]


def test_tail_permission_error_surfaces(monkeypatch):
    def fake_run(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="",
                               stderr="No journal files were opened")

    monkeypatch.setattr(vllm, "_ctx", _mk_ctx())
    monkeypatch.setattr(vllm.subprocess, "run", fake_run)
    out = vllm.vllm_log_tail(authorization="Bearer x")
    assert out["ok"] is False
    assert "journal" in out["error"].lower()


def test_stream_routes_registered():
    paths = {(m, p) for m, p, _ in vllm._ROUTES}
    assert ("GET", "/vllm/log/tail") in paths
    assert ("GET", "/vllm/log/stream") in paths


def test_streamer_uses_follow_argv(monkeypatch):
    seen = {}

    class _FakeProc:
        stdout = None

        def __init__(self, argv, **kw):
            seen["argv"] = argv
            raise RuntimeError("stop here")

    monkeypatch.setattr(vllm, "_ctx", _mk_ctx())
    monkeypatch.setattr(vllm._shared.subprocess, "Popen", _FakeProc)
    vllm._vllm_log_streamer()  # swallows the error via its except path
    assert seen["argv"] == ["journalctl", "-u", "vllm.service", "-n", "100",
                            "-f", "-o", "cat"]
    assert vllm._log_state.streaming is False
