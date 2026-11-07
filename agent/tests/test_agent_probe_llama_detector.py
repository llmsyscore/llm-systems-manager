"""Role=auto start-up probe: the installer's detector counts as a llama.cpp sighting."""
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

_AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"
_SRC = _AGENT_PY.read_text()


def _exec_fn(name, ns):
    m = re.search(rf"^def {name}\(.*?(?=^\S)", _SRC, re.MULTILINE | re.DOTALL)
    assert m, name
    exec(compile(m.group(0), str(_AGENT_PY), "exec"), ns)
    return ns


def _with_fake_llama(monkeypatch, det):
    pkg = types.ModuleType("providers")
    mod = types.ModuleType("providers.llama")
    mod._detect_module = lambda: SimpleNamespace(detect=lambda cfg_port=None, **kw: det)
    mod.llama_api_port = lambda url: 8080
    pkg.llama = mod
    monkeypatch.setitem(sys.modules, "providers", pkg)
    monkeypatch.setitem(sys.modules, "providers.llama", mod)


def test_probe_reports_a_stopped_unit(monkeypatch, capsys):
    _with_fake_llama(monkeypatch, SimpleNamespace(found=True, unit="llama-server.service", port=9931, running=False))
    ns = _exec_fn("_probe_llama_detector", {})
    out = ns["_probe_llama_detector"](SimpleNamespace(LLAMA_API_URL="http://127.0.0.1:8080", LLAMA_SYSTEMD_UNIT=""))
    assert out == "unit llama-server.service, port 9931, stopped"


def test_probe_is_blank_when_nothing_found(monkeypatch):
    _with_fake_llama(monkeypatch, SimpleNamespace(found=False))
    ns = _exec_fn("_probe_llama_detector", {})
    assert ns["_probe_llama_detector"](SimpleNamespace(LLAMA_API_URL="x", LLAMA_SYSTEMD_UNIT="")) == ""


def test_probe_tolerates_a_broken_detector(monkeypatch, capsys):
    pkg = types.ModuleType("providers"); mod = types.ModuleType("providers.llama")
    def boom(): raise ImportError("no detector")
    mod._detect_module = boom; mod.llama_api_port = lambda url: 0; pkg.llama = mod
    monkeypatch.setitem(sys.modules, "providers", pkg); monkeypatch.setitem(sys.modules, "providers.llama", mod)
    ns = _exec_fn("_probe_llama_detector", {})
    assert ns["_probe_llama_detector"](SimpleNamespace(LLAMA_API_URL="x", LLAMA_SYSTEMD_UNIT="")) == ""
    assert "detector unavailable" in capsys.readouterr().out


def test_autoconfigure_marks_the_auto_role():
    body = re.search(r"^def _probe_and_autoconfigure\(.*?(?=^\S)", _SRC, re.MULTILINE | re.DOTALL).group(0)
    assert "cfg._auto_role = True" in body and "_probe_llama_detector(cfg)" in body
