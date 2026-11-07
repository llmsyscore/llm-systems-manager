# agent/tests/test_hf_cli_builtin.py
"""Built-in hf CLI: command precedence, __main__ dispatch, packaging inputs."""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cache_gguf_list import _load_llama  # noqa: E402

_AGENT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def llama():
    return _load_llama()


class _Cfg:
    HF_CLI_PATH = ""


class _Ctx:
    config = _Cfg()


@pytest.fixture
def env(llama, monkeypatch, tmp_path):
    _Cfg.HF_CLI_PATH = ""
    monkeypatch.setattr(llama, "_require_ctx", _Ctx)
    monkeypatch.setattr(llama.shutil, "which", lambda n: None)
    monkeypatch.setattr(llama.os.path, "expanduser", lambda p: str(tmp_path / "none"))
    monkeypatch.delattr(sys, "frozen", raising=False)
    return tmp_path


def test_configured_path_wins(llama, env, monkeypatch):
    _Cfg.HF_CLI_PATH = "/x/hf"
    monkeypatch.setattr(llama.shutil, "which", lambda n: "/usr/bin/hf")
    assert llama._hf_cli_cmd() == ["/x/hf"]


def test_which_next(llama, env, monkeypatch):
    monkeypatch.setattr(llama.shutil, "which", lambda n: "/usr/bin/hf")
    assert llama._hf_cli_cmd() == ["/usr/bin/hf"]


def test_user_local_bin_next(llama, env, monkeypatch):
    f = env / "hf"
    f.write_text("")
    monkeypatch.setattr(llama.os.path, "expanduser", lambda p: str(f))
    assert llama._hf_cli_cmd() == [str(f)]


def test_frozen_fallback(llama, env, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert llama._hf_cli_cmd() == [sys.executable, "--hf-cli"]


def test_unfrozen_fallback_has_script(llama, env):
    cmd = llama._hf_cli_cmd()
    assert cmd[0] == sys.executable and cmd[-1] == "--hf-cli"
    assert cmd[1].endswith("llm-systems-agent.py")


def test_main_dispatch_runs_hf_main(tmp_path):
    src = (_AGENT_ROOT / "llm-systems-agent.py").read_text()
    m = re.search(r'if __name__ == "__main__":\n(.*)\Z', src, re.S)
    assert m
    block = textwrap.dedent(m.group(1))
    pkg = tmp_path / "stub" / "huggingface_hub" / "cli"
    pkg.mkdir(parents=True)
    (tmp_path / "stub" / "huggingface_hub" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "hf.py").write_text("import sys\ndef main():\n    print('ARGV', sys.argv)\n    return 0\n")
    prog = ("import sys\nVERSION='v'\ndef main():\n    print('MAIN')\n" + block)
    wrapper = tmp_path / "wrap.py"
    wrapper.write_text(f"import sys\nsys.path.insert(0, {str(tmp_path / 'stub')!r})\n" + prog)
    r = subprocess.run([sys.executable, str(wrapper), "--hf-cli", "download", "x/y"],
                       capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "ARGV ['hf', 'download', 'x/y']" in r.stdout
    assert "MAIN" not in r.stdout


def test_main_dispatch_missing_hf_exits_cleanly(tmp_path):
    src = (_AGENT_ROOT / "llm-systems-agent.py").read_text()
    block = textwrap.dedent(re.search(r'if __name__ == "__main__":\n(.*)\Z', src, re.S).group(1))
    script = tmp_path / "run.py"
    script.write_text("import sys\nVERSION='v'\nsys.modules['huggingface_hub'] = None\n"
                      "def main():\n    print('MAIN')\n" + block)
    r = subprocess.run([sys.executable, str(script), "--hf-cli", "download", "x/y"],
                       capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert r.returncode == 1
    assert "Hugging Face download tool not installed" in r.stderr
    assert "Traceback" not in r.stderr and "MAIN" not in r.stdout


def test_detect_default_hf_cmd_blank_without_hf(monkeypatch):
    src = (_AGENT_ROOT / "llm-systems-agent.py").read_text()
    fn = re.search(r"def _detect_default_hf_cmd\(\).*?(?=\n\ndef )", src, re.S).group(0)
    ns = {"shutil": __import__("shutil")}
    exec(fn, ns)
    monkeypatch.setattr(ns["shutil"], "which", lambda n: None)
    assert ns["_detect_default_hf_cmd"]() == ""
    monkeypatch.setattr(ns["shutil"], "which", lambda n: "/usr/bin/hf")
    assert ns["_detect_default_hf_cmd"]() == "/usr/bin/hf"


def test_blank_config_and_no_hf_uses_frozen_fallback(llama, env, monkeypatch):
    _Cfg.HF_CLI_PATH = ""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert llama._hf_cli_cmd() == [sys.executable, "--hf-cli"]


def test_packaging_inputs():
    assert "huggingface_hub" in (_AGENT_ROOT / "install" / "requirements.txt").read_text()
    assert 'collect_submodules("huggingface_hub")' in (_AGENT_ROOT / "llm-systems-agent.spec").read_text()
