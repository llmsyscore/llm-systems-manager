# agent/tests/test_install_template_render.py
# Literal template substitution (#296) + escaping of quoted values (#297).
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent
INSTALL_SH = AGENT_DIR / "install" / "install.sh"
LIB_COMMON = REPO_ROOT / "tools" / "installer" / "lib-common.sh"
SERVICE_TMPL = AGENT_DIR / "install" / "llm-systems-agent.service.tmpl"
SUDOERS_TMPL = AGENT_DIR / "install" / "llm-systems-agent.sudoers.tmpl"
EXAMPLE = AGENT_DIR / "agent_config.yaml.example"

HOSTILE = ["/opt/a&b", "/opt/a|b", "/opt/a\\b", "/opt/a\\"]


def _extract_func(source: Path, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", source.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from {source.name}"
    return m.group(0)


def _run_bash(script: str, *argv: str) -> str:
    out = subprocess.run(["bash", "-c", script, "bash", *argv],
                         capture_output=True, text=True)
    assert out.returncode == 0, f"bash failed rc={out.returncode}: {out.stderr}"
    return out.stdout


# ── install.sh: _subst_tokens ───────────────────────────────────────────────

def _subst(tmp_path: Path, template_text: str, *pairs: str) -> str:
    tmpl = tmp_path / "t.tmpl"
    tmpl.write_text(template_text)
    func = _extract_func(INSTALL_SH, "_subst_tokens")
    return _run_bash(f"set -euo pipefail\n{func}\n_subst_tokens \"$@\"\n",
                     str(tmpl), *pairs)


def test_subst_tokens_hostile_values_stay_literal(tmp_path):
    for val in HOSTILE:
        out = _subst(tmp_path, "X=__T__\n", "__T__", val)
        assert out == f"X={val}\n", f"corrupted render for {val!r}: {out!r}"


def test_subst_tokens_replaces_every_occurrence(tmp_path):
    out = _subst(tmp_path, "A=__T__\nB=__T__\n", "__T__", "/opt/a&b")
    assert out == "A=/opt/a&b\nB=/opt/a&b\n"


def test_subst_tokens_absent_token_is_noop(tmp_path):
    out = _subst(tmp_path, "A=1\nB=2\n", "__T__", "/opt/x")
    assert out == "A=1\nB=2\n"


def test_subst_tokens_multiple_pairs(tmp_path):
    out = _subst(tmp_path, "U=${AGENT_USER} D=${AGENT_INSTALL_DIR}\n",
                 "${AGENT_USER}", "llm&user", "${AGENT_INSTALL_DIR}", "/opt/a|b")
    assert out == "U=llm&user D=/opt/a|b\n"


def test_agent_unit_render_with_ampersand_dir(tmp_path):
    func = _extract_func(INSTALL_SH, "_subst_tokens")
    out = _run_bash(
        f"set -euo pipefail\n{func}\n"
        "_subst_tokens \"$1\" '${AGENT_USER}' llmuser "
        "'${AGENT_GROUP}' llmuser '${AGENT_INSTALL_DIR}' '/opt/a&b'\n",
        str(SERVICE_TMPL))
    assert "/opt/a&b/venv/bin/python3" in out
    assert "WorkingDirectory=/opt/a&b" in out
    assert "${AGENT_INSTALL_DIR}" not in out


def test_sudoers_render_with_hostile_user(tmp_path):
    func = _extract_func(INSTALL_SH, "_subst_tokens")
    out = _run_bash(
        f"set -euo pipefail\n{func}\n"
        "_subst_tokens \"$1\" '${AGENT_USER}' 'llm&user' "
        "'${LLAMA_UNIT}' llama_server.service\n",
        str(SUDOERS_TMPL))
    assert "llm&user" in out
    assert "${AGENT_USER}" not in out and "${LLAMA_UNIT}" not in out


def test_install_sh_has_no_sed_template_renders():
    # Every .tmpl render must go through _subst_tokens, not sed. A sed render
    # spans continuation lines, so scan a 4-line window before each .tmpl ref.
    lines = INSTALL_SH.read_text().splitlines()
    hits = []
    for i, ln in enumerate(lines):
        if ".tmpl" not in ln:
            continue
        window = lines[max(0, i - 3):i + 1]
        if any(re.search(r"\bsed\b.*\bs\|", w) for w in window):
            hits.append((i + 1, ln.strip()))
    assert not hits, f"sed-based template render(s) remain: {hits}"


# ── install.sh: ExecStart quoting in the unit template (#297) ───────────────

def test_service_template_quotes_execstart_paths():
    line = next(l for l in SERVICE_TMPL.read_text().splitlines()
                if l.startswith("ExecStart="))
    assert '"${AGENT_INSTALL_DIR}/venv/bin/python3"' in line
    assert '"${AGENT_INSTALL_DIR}/llm-systems-agent.py"' in line


# ── install.sh: --install-dir character validation (#297) ──────────────────

def _validate_dir(val: str) -> int:
    func = _extract_func(INSTALL_SH, "_install_dir_chars_ok")
    return subprocess.run(
        ["bash", "-c", f"{func}\n_install_dir_chars_ok \"$1\"", "bash", val],
        capture_output=True).returncode


def test_install_dir_validation_accepts_normal_paths():
    for ok in ["/opt/llm-systems-agent", "/home/gpu-user/llm-systems-agent",
               "/srv/agents/llm-systems-agent"]:
        assert _validate_dir(ok) == 0, ok


def test_install_dir_validation_rejects_breaking_chars():
    for bad in ["/opt/with space/llm-systems-agent", '/opt/q"uote',
                "/opt/back\\slash", "/opt/single'quote", "relative/path"]:
        assert _validate_dir(bad) != 0, bad


# ── install.sh: _xml_escape + plist render (#323) ───────────────────────────

PLIST_TMPL = AGENT_DIR / "install" / "com.llm-systems-agent.plist.tmpl"


def _xml_escape(val: str) -> str:
    func = _extract_func(INSTALL_SH, "_xml_escape")
    return _run_bash(f"set -euo pipefail\n{func}\n_xml_escape \"$1\"\n", val)


def test_xml_escape_escapes_markup_chars():
    assert _xml_escape("a&b") == "a&amp;b"
    assert _xml_escape("a<b>c") == "a&lt;b&gt;c"
    assert _xml_escape("/Users/a&b") == "/Users/a&amp;b"
    # ampersand must be escaped first so &lt; doesn't get double-escaped
    assert _xml_escape("<&>") == "&lt;&amp;&gt;"


def test_xml_escape_passes_through_plain_values():
    assert _xml_escape("llmuser") == "llmuser"
    assert _xml_escape("/Users/gpu/home") == "/Users/gpu/home"


def test_plist_render_with_hostile_user_home_is_well_formed(tmp_path):
    # A home dir / user containing & < > must render valid XML, not break launchd.
    # expat: no external-DTD/entity resolution, so no XXE/billion-laughs surface.
    import xml.parsers.expat
    func = _extract_func(INSTALL_SH, "_subst_tokens") + "\n" + \
        _extract_func(INSTALL_SH, "_xml_escape")
    out = _run_bash(
        f"set -euo pipefail\n{func}\n"
        "_subst_tokens \"$1\" "
        "'${AGENT_USER}' \"$(_xml_escape \"$2\")\" "
        "'${AGENT_USER_HOME}' \"$(_xml_escape \"$3\")\" "
        "'${AGENT_INSTALL_DIR}' \"$(_xml_escape \"$4\")\"\n",
        str(PLIST_TMPL), "a&b", "/Users/a&b<x>", "/opt/agent")
    assert "${AGENT_USER}" not in out and "${AGENT_USER_HOME}" not in out
    assert "&amp;" in out and "&lt;" in out
    xml.parsers.expat.ParserCreate().Parse(out, True)  # raises on malformed XML


def test_install_sh_plist_renders_xml_escape_all_values():
    # Both plist render sites must wrap every substituted value in _xml_escape.
    text = INSTALL_SH.read_text()
    n = 0
    for m in re.finditer(r"com\.llm-systems-agent\.plist\.tmpl", text):
        window = text[m.start():m.start() + 400]
        if "PLIST_DEST" in window or "> \"$PLIST_DEST\"" in window:
            pass
        n += 1
    # 2 render sites (fresh install + --update refresh)
    renders = re.findall(
        r"_subst_tokens \"\$TMPL_DIR/com\.llm-systems-agent\.plist\.tmpl\".*?PLIST_DEST\"",
        text, re.S)
    assert len(renders) == 2, f"expected 2 plist renders, found {len(renders)}"
    for r in renders:
        assert r.count("_xml_escape") == 3, f"plist render missing escapes: {r}"


# ── install.sh: _set_quoted YAML escaping (#297) ────────────────────────────

def _extract_writer() -> str:
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", INSTALL_SH.read_text(), re.S)
    writer = [b for b in blocks if "monitor_influxdb_disk) = sys.argv" in b]
    assert len(writer) == 1
    return writer[0]


def _run_writer(cfg_path: Path, hostname: str, description: str) -> str:
    argv = [
        str(cfg_path),
        "linux", "inference", "http://m", "http://ae",
        "llmuser", "/opt/agent", hostname, description,
        "false", "true", "false", "false", "false",
        "", "", "", "", "", "", "", "", "",
        "", "", "",
        "false", "false", "false",
    ]
    subprocess.run([sys.executable, "-c", _extract_writer(), *argv],
                   check=True, capture_output=True, text=True)
    return cfg_path.read_text()


def test_set_quoted_escapes_embedded_quotes(tmp_path):
    cfg = tmp_path / "agent_config.yaml"
    shutil.copy(EXAMPLE, cfg)
    out = _run_writer(cfg, 'gpu"1', 'Rack "A" east')
    assert 'AGENT_HOSTNAME: "gpu\\"1"' in out
    assert 'AGENT_DESCRIPTION: "Rack \\"A\\" east"' in out


def test_set_quoted_escapes_backslashes(tmp_path):
    cfg = tmp_path / "agent_config.yaml"
    shutil.copy(EXAMPLE, cfg)
    out = _run_writer(cfg, "gpu1", "path C:\\models")
    assert 'AGENT_DESCRIPTION: "path C:\\\\models"' in out


def test_set_quoted_escapes_control_chars(tmp_path):
    # Values without a bash-side control-char guard (URLs, llama paths) must
    # not be able to fabricate extra YAML lines.
    cfg = tmp_path / "agent_config.yaml"
    shutil.copy(EXAMPLE, cfg)
    out = _run_writer(cfg, "gpu1", "line1\nFAKE_KEY: true\ttab")
    line = next(l for l in out.splitlines() if l.startswith("AGENT_DESCRIPTION:"))
    assert line == 'AGENT_DESCRIPTION: "line1\\nFAKE_KEY: true\\ttab"'
    assert not any(l.startswith("FAKE_KEY:") for l in out.splitlines())


# ── install.sh: _render_llama_unit arg escaping (#297) ──────────────────────

def test_render_llama_unit_escapes_quotes_in_paths():
    func = (_extract_func(INSTALL_SH, "_subst_tokens") + "\n"
            + _extract_func(INSTALL_SH, "_render_llama_unit"))
    script = (f"set -euo pipefail\nSRC_DIR={AGENT_DIR}\n{func}\n"
              '_render_llama_unit "$1" "$2" "$3" "$4"\n')
    out = _run_bash(script, "/usr/local/bin/llama-server", "llmuser",
                    '/etc/llama/mo"dels.ini', "")
    line = next(l for l in out.splitlines() if l.startswith("ExecStart="))
    assert '--models-preset "/etc/llama/mo\\"dels.ini"' in line


def test_render_llama_unit_quotes_and_escapes_binary():
    func = (_extract_func(INSTALL_SH, "_subst_tokens") + "\n"
            + _extract_func(INSTALL_SH, "_render_llama_unit"))
    script = (f"set -euo pipefail\nSRC_DIR={AGENT_DIR}\n{func}\n"
              '_render_llama_unit "$1" "$2" "$3" "$4"\n')
    out = _run_bash(script, '/opt/lla"ma/llama-server', "llmuser", "", "")
    line = next(l for l in out.splitlines() if l.startswith("ExecStart="))
    assert line.startswith('ExecStart="/opt/lla\\"ma/llama-server" --metrics')


# ── tools/installer/lib-common.sh: validate_install_dir (#323) ──────────────

def _validate_lib_dir(val: str) -> int:
    func = _extract_func(LIB_COMMON, "validate_install_dir")
    return subprocess.run(
        ["bash", "-c", f"{func}\nvalidate_install_dir \"$1\"", "bash", val],
        capture_output=True).returncode


def test_validate_install_dir_accepts_normal_paths():
    for ok in ["/opt/llm-systems-manager", "/srv/llmsys", "/home/gpu/deploy"]:
        assert _validate_lib_dir(ok) == 0, ok


def test_validate_install_dir_rejects_whitespace_and_metachars():
    for bad in ["/opt/with space", "relative/path", '/opt/q"uote',
                "/opt/a&b", "/opt/a\\b", "/opt/a<b>"]:
        assert _validate_lib_dir(bad) != 0, bad


def test_validate_install_dir_matches_agent_predicate():
    # lib-common's validate_install_dir must use the same charset as the
    # agent installer's _install_dir_chars_ok to avoid silent divergence.
    lib = _extract_func(LIB_COMMON, "validate_install_dir")
    agent = _extract_func(INSTALL_SH, "_install_dir_chars_ok")
    lib_pat = re.search(r'!= \*(\[\[:space:\].*?\])\*', lib).group(1)
    agent_pat = re.search(r'!= \*(\[\[:space:\].*?\])\*', agent).group(1)
    assert lib_pat == agent_pat, f"{lib_pat!r} != {agent_pat!r}"


def test_installer_validates_install_dir_before_deploy():
    text = (REPO_ROOT / "tools" / "installer" / "install.sh").read_text()
    assert "validate_install_dir \"$LLMSYS_INSTALL_DIR\"" in text


# ── tools/installer/lib-common.sh: subst_all + renderers (#296) ─────────────

def _lib_render_unit(tmp_path: Path, install_dir: str) -> str:
    tmpl = tmp_path / "svc.service.example"
    tmpl.write_text("WorkingDirectory=@@INSTALL_DIR@@\n"
                    "ExecStart=@@INSTALL_DIR@@/venv/bin/python x.py\n"
                    "User=@@RUN_USER@@\nGroup=@@RUN_GROUP@@\n")
    out_file = tmp_path / "svc.service"
    subst = _extract_func(LIB_COMMON, "subst_all")
    render = _extract_func(LIB_COMMON, "render_unit_template")
    _run_bash(
        f"set -euo pipefail\n{subst}\n{render}\n"
        f"LLMSYS_INSTALL_DIR=\"$1\"\nLLMSYS_RUN_USER=llmsys\nLLMSYS_RUN_GROUP=llmsys\n"
        "render_unit_template \"$2\" \"$3\"\n",
        install_dir, str(tmpl), str(out_file))
    return out_file.read_text()


def test_lib_common_render_hostile_install_dirs(tmp_path):
    for i, val in enumerate(HOSTILE):
        out = _lib_render_unit(tmp_path, val)
        assert f"WorkingDirectory={val}\n" in out, f"corrupted: {val!r} -> {out!r}"
        assert f"ExecStart={val}/venv/bin/python x.py" in out
        assert "@@" not in out


def test_lib_common_sudoers_fragment_uses_subst_all():
    # install_sudoers_fragment must not sed-substitute @@RUN_USER@@.
    body = _extract_func(LIB_COMMON, "install_sudoers_fragment")
    assert "sed" not in body
    assert "subst_all" in body
