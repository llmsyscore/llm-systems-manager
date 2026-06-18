# agent/tests/test_install_llama.py
from __future__ import annotations

import importlib.util
import types
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent


def _load_install_llama():
    p = _AGENT_ROOT / "install" / "install_llama.py"
    spec = importlib.util.spec_from_file_location("install_llama_mod", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_leaf(*, plan_result="PLAN", run_result, raise_plan=False):
    class _Err(Exception):
        pass

    def plan(method, opts, cfg):
        if raise_plan:
            raise _Err("bad method")
        plan.seen = {"method": method, "opts": opts, "cfg": cfg}
        return plan_result

    def run_install(iplan, emit=lambda _s: None, **_kw):
        return run_result

    return types.SimpleNamespace(InstallError=_Err, plan=plan, run_install=run_install)


def test_install_llama_prints_resolved_on_success(monkeypatch, capsys, tmp_path):
    il = _load_install_llama()
    binp = tmp_path / "llama-server"
    binp.touch()
    leaf = _fake_leaf(run_result=(0, str(binp)))
    monkeypatch.setattr(il, "_load_leaf", lambda: leaf)
    rc = il.main(["--method", "release_binary", "--backend", "cpu", "--agent-user", "svc"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"RESOLVED_BIN={binp}" in out
    assert leaf.plan.seen["method"] == "release_binary"
    assert leaf.plan.seen["opts"]["backend"] == "cpu"


def test_install_llama_returns_rc_on_failed_build(monkeypatch, capsys, tmp_path):
    il = _load_install_llama()
    leaf = _fake_leaf(run_result=(5, None))
    monkeypatch.setattr(il, "_load_leaf", lambda: leaf)
    rc = il.main(["--method", "source"])
    out = capsys.readouterr().out
    assert rc == 5
    assert "RESOLVED_BIN=" not in out


def test_install_llama_errors_when_binary_missing(monkeypatch, capsys):
    il = _load_install_llama()
    leaf = _fake_leaf(run_result=(0, "/nonexistent/llama-server"))
    monkeypatch.setattr(il, "_load_leaf", lambda: leaf)
    rc = il.main(["--method", "source"])
    assert rc == 4
    assert "RESOLVED_BIN=" not in capsys.readouterr().out


def test_install_llama_errors_when_resolved_is_none(monkeypatch, capsys):
    il = _load_install_llama()
    leaf = _fake_leaf(run_result=(0, None))               # rc 0 but resolve returned None
    monkeypatch.setattr(il, "_load_leaf", lambda: leaf)
    rc = il.main(["--method", "source"])
    assert rc == 4
    assert "RESOLVED_BIN=" not in capsys.readouterr().out


def test_install_llama_plan_error_returns_2(monkeypatch, capsys):
    il = _load_install_llama()
    leaf = _fake_leaf(run_result=(0, "/x"), raise_plan=True)
    monkeypatch.setattr(il, "_load_leaf", lambda: leaf)
    rc = il.main(["--method", "nixpkgs"])
    assert rc == 2
