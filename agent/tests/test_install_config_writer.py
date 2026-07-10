# agent/tests/test_install_config_writer.py
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"
EXAMPLE = AGENT_DIR / "agent_config.yaml.example"


def _extract_writer() -> str:
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", INSTALL_SH.read_text(), re.S)
    writer = [b for b in blocks if "vllm_unit) = sys.argv" in b]
    assert len(writer) == 1, f"expected 1 config writer, found {len(writer)}"
    return writer[0]


def _run_writer(cfg_path: Path, **over) -> str:
    # positional argv in the exact order install.sh passes them
    argv = [
        str(cfg_path),
        "linux", "inference", "http://m", "http://ae",
        "llmuser", "/opt/agent", "", "",
        "false", "true", "false", "false", "false",
        over.get("llama_api", ""), over.get("llama_log", ""), over.get("llama_unit", ""),
        over.get("llama_bin", ""), over.get("llama_ini", ""), over.get("llama_build_method", ""),
        over.get("llama_build_dir", ""), over.get("llama_backend", ""), over.get("llama_script", ""),
        "", "", "",
        "false", "false", "false",
        "false", "",
    ]
    subprocess.run([sys.executable, "-c", _extract_writer(), *argv],
                   check=True, capture_output=True, text=True)
    return cfg_path.read_text()


def _seed(tmp_path: Path) -> Path:
    cfg = tmp_path / "agent_config.yaml"
    shutil.copy(EXAMPLE, cfg)
    return cfg


def test_managed_source_writes_build_dir_and_backend(tmp_path):
    out = _run_writer(_seed(tmp_path), llama_bin="/opt/llama/bin/llama-server",
                      llama_build_method="source",
                      llama_build_dir="/home/u/.local/share/llama.cpp",
                      llama_backend="vulkan")
    assert re.search(r'^LLAMA_BUILD_DIR:\s*"/home/u/\.local/share/llama\.cpp"\s*$', out, re.M)
    assert re.search(r'^LLAMA_BUILD_OPTS:\s*$', out, re.M)
    assert re.search(r'^  backend: vulkan\s*$', out, re.M)
    # other per-method knob docs stay as comments
    assert re.search(r'^#\s+git_ref:', out, re.M)
    assert re.search(r'^#\s+backup_retain:', out, re.M)


def test_existing_install_writes_build_dir_leaves_opts_commented(tmp_path):
    out = _run_writer(_seed(tmp_path),
                      llama_bin="/usr/local/llama-server/llama-server",
                      llama_build_method="custom_script",
                      llama_build_dir="/usr/local/llama-server",
                      llama_backend="")
    assert re.search(r'^LLAMA_BUILD_DIR:\s*"/usr/local/llama-server"\s*$', out, re.M)
    # no backend chosen -> the OPTS block is left entirely commented
    assert re.search(r'^#\s*LLAMA_BUILD_OPTS:', out, re.M)
    assert not re.search(r'^  backend:', out, re.M)


def test_custom_script_writes_script_path(tmp_path):
    out = _run_writer(_seed(tmp_path), llama_build_method="custom_script",
                      llama_bin="/usr/local/llama-server/llama-server",
                      llama_build_dir="/usr/local/llama-server",
                      llama_script="/opt/build/my-llama.sh")
    assert re.search(r'^LLAMA_BUILD_OPTS:\s*$', out, re.M)
    assert re.search(r'^  script_path: "/opt/build/my-llama\.sh"\s*$', out, re.M)
    assert not re.search(r'^  backend:', out, re.M)


def test_backend_write_is_idempotent(tmp_path):
    cfg = _seed(tmp_path)
    _run_writer(cfg, llama_build_method="source",
                llama_build_dir="/r", llama_backend="vulkan")
    out = _run_writer(cfg, llama_build_method="source",
                      llama_build_dir="/r", llama_backend="cuda")
    assert out.count("LLAMA_BUILD_OPTS:") == 1
    assert len(re.findall(r'^  backend:', out, re.M)) == 1
    assert re.search(r'^  backend: cuda\s*$', out, re.M)


def test_managed_source_output_has_valid_yaml_structure(tmp_path):
    out = _run_writer(_seed(tmp_path), llama_build_method="source",
                      llama_build_dir="/home/u/.local/share/llama.cpp",
                      llama_backend="vulkan")
    lines = out.splitlines()
    hdr = next(i for i, l in enumerate(lines) if l == "LLAMA_BUILD_OPTS:")
    # first live (non-comment, non-blank) line under the mapping is the backend child
    child = next(l for l in lines[hdr + 1:]
                 if l.strip() and not l.lstrip().startswith("#"))
    assert child == "  backend: vulkan"   # exactly 2-space indent, no tab
    assert "\t" not in out
    # full parse is a bonus when PyYAML is present, but not required to enforce
    try:
        import yaml
    except ImportError:
        return
    d = yaml.safe_load(out)
    assert d["LLAMA_BUILD_OPTS"] == {"backend": "vulkan"}
    assert d["LLAMA_BUILD_DIR"] == "/home/u/.local/share/llama.cpp"
