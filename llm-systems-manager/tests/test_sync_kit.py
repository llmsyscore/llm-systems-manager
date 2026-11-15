"""Tests for tools/kit/sync-kit.sh (copy and drift check of the shared panel kit)."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "kit" / "sync-kit.sh"
SHA = "a" * 40
OTHER = "b" * 40
KIT_JS = "// Panel kit 1.2.3\nvar x = 1;\n"
KIT_CSS = ".kit { color: red; }\n"


def _make_src(tmp_path, sha=SHA):
    d = tmp_path / "src" / sha / "llm-systems-manager" / "frontend" / "kit"
    d.mkdir(parents=True)
    (d / "kit.js").write_text(KIT_JS)
    (d / "kit.css").write_text(KIT_CSS)


def _run(tmp_path, *args):
    env = {**os.environ, "KIT_BASE": f"file://{tmp_path}/src"}
    return subprocess.run(["bash", str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_sync_writes_files_and_pin(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "dest"
    r = _run(tmp_path, SHA, str(dest))
    assert r.returncode == 0, r.stderr
    assert (dest / "kit.js").read_text() == KIT_JS
    assert (dest / "kit.css").read_text() == KIT_CSS
    assert (dest / "kit.pin").read_text() == f"sha={SHA}\nversion=1.2.3\n"


def test_check_clean_is_zero(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "dest"
    assert _run(tmp_path, SHA, str(dest)).returncode == 0
    assert _run(tmp_path, "--check", SHA, str(dest)).returncode == 0


def test_check_detects_edit(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "dest"
    _run(tmp_path, SHA, str(dest))
    with open(dest / "kit.css", "a") as f:
        f.write("x")
    r = _run(tmp_path, "--check", SHA, str(dest))
    assert r.returncode == 1
    assert "kit drift: kit.css" in r.stderr


def test_check_detects_pin_mismatch(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "dest"
    _run(tmp_path, SHA, str(dest))
    (dest / "kit.pin").write_text(f"sha={OTHER}\nversion=1.2.3\n")
    r = _run(tmp_path, "--check", SHA, str(dest))
    assert r.returncode == 1
    assert "kit drift: kit.pin" in r.stderr


def test_bad_sha_exit_2(tmp_path):
    r = _run(tmp_path, "main", str(tmp_path / "dest"))
    assert r.returncode == 2


def test_missing_source_exit_3(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "dest"
    r = _run(tmp_path, OTHER, str(dest))
    assert r.returncode == 3
    assert not dest.exists()


def test_missing_source_keeps_populated_dest(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "dest"
    assert _run(tmp_path, SHA, str(dest)).returncode == 0
    before = {n: (dest / n).read_bytes() for n in ("kit.js", "kit.css", "kit.pin")}
    r = _run(tmp_path, OTHER, str(dest))
    assert r.returncode == 3
    after = {n: (dest / n).read_bytes() for n in before}
    assert after == before


def test_missing_version_exit_4(tmp_path):
    _make_src(tmp_path)
    (tmp_path / "src" / SHA / "llm-systems-manager" / "frontend" / "kit" / "kit.js").write_text("var x = 1;\n")
    dest = tmp_path / "dest"
    r = _run(tmp_path, SHA, str(dest))
    assert r.returncode == 4
    assert "no kit version in kit.js" in r.stderr
    assert not dest.exists()


def test_check_missing_dest_file(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "dest"
    _run(tmp_path, SHA, str(dest))
    (dest / "kit.js").unlink()
    r = _run(tmp_path, "--check", SHA, str(dest))
    assert r.returncode == 1
    assert "kit drift: kit.js" in r.stderr


def test_check_failed_fetch_exit_3(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "dest"
    _run(tmp_path, SHA, str(dest))
    assert _run(tmp_path, "--check", OTHER, str(dest)).returncode == 3


def test_dest_with_spaces(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "my dest dir"
    r = _run(tmp_path, SHA, str(dest))
    assert r.returncode == 0, r.stderr
    assert (dest / "kit.pin").read_text() == f"sha={SHA}\nversion=1.2.3\n"


def test_trailing_check_flag_exit_2_and_dest_unchanged(tmp_path):
    _make_src(tmp_path)
    dest = tmp_path / "dest"
    assert _run(tmp_path, SHA, str(dest)).returncode == 0
    (dest / "kit.css").write_text("edited\n")
    before = {n: (dest / n).read_bytes() for n in ("kit.js", "kit.css", "kit.pin")}
    r = _run(tmp_path, SHA, str(dest), "--check")
    assert r.returncode == 2
    assert "usage:" in r.stderr
    assert {n: (dest / n).read_bytes() for n in before} == before
