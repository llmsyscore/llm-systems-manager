# agent/tests/test_llama_upgrade.py
from __future__ import annotations

import os
import stat
from pathlib import Path

from conftest import llama_upgrade as lu


def _exe(path: Path, rc: int = 0, marker: str = "new") -> None:
    path.write_text(f"#!/bin/sh\necho 'version {marker}'\nexit {rc}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _src(tmp_path: Path, *, rc: int = 0, marker: str = "new") -> Path:
    src = tmp_path / "src"; src.mkdir()
    _exe(src / "llama-server", rc=rc, marker=marker)
    _exe(src / "llama-bench", marker=marker)
    (src / "libggml-base.so").write_text(f"GGML-{marker}")
    (src / "notes.txt").write_text("not an artifact")
    return src


def _dest(tmp_path: Path) -> Path:
    dest = tmp_path / "dest"; dest.mkdir()
    _exe(dest / "llama-server", marker="old")
    (dest / "libggml-base.so").write_text("GGML-old")
    (dest / "config.ini").write_text("[*]\nkeep = yes\n")
    return dest


# ---- should_upgrade_in_place / allowlist ----

def test_should_upgrade_defaults_on_for_build_methods():
    assert lu.should_upgrade_in_place("source", {"install_in_place": True})
    assert lu.should_upgrade_in_place("release_binary", {"install_in_place": True})
    assert lu.should_upgrade_in_place("source", {})                              # default on
    assert lu.should_upgrade_in_place("release_binary", None)                    # default on
    assert not lu.should_upgrade_in_place("source", {"install_in_place": False})  # explicit opt-out
    assert not lu.should_upgrade_in_place("custom_script", {"install_in_place": True})
    assert not lu.should_upgrade_in_place("conda", {"install_in_place": True})


def test_is_artifact_allowlist():
    assert lu.is_artifact("llama-server", "llama-server")
    assert lu.is_artifact("llama-bench", "llama-server")
    assert lu.is_artifact("libggml-base.so", "llama-server")
    assert lu.is_artifact("libllama.so.1", "llama-server")
    assert lu.is_artifact("libmtmd.dylib", "llama-server")
    assert lu.is_artifact("my-server", "my-server")          # configured bin name
    assert not lu.is_artifact("config.ini", "llama-server")
    assert not lu.is_artifact("build-llama-cpp.sh", "llama-server")
    assert not lu.is_artifact("notes.txt", "llama-server")


def test_select_artifacts_skips_non_allowlisted(tmp_path):
    src = _src(tmp_path)
    got = lu.select_artifacts(src, "llama-server")
    assert set(got) == {"llama-server", "llama-bench", "libggml-base.so"}
    assert "notes.txt" not in got


# ---- happy path ----

def test_upgrade_swaps_binaries_preserves_user_files(tmp_path):
    src = _src(tmp_path, marker="new")
    dest = _dest(tmp_path)
    broot = tmp_path / "broot"; broot.mkdir()
    (broot / "release.download").write_text("tarball")
    seen = []

    res = lu.upgrade_in_place(str(src / "llama-server"), str(dest / "llama-server"),
                              build_root=str(broot), unit="llama_server.service",
                              emit=seen.append)

    assert res.ok and not res.skipped
    assert res.target == str(dest / "llama-server")
    assert set(res.swapped) == {"llama-server", "llama-bench", "libggml-base.so"}
    # live files now carry the new content …
    assert "version new" in (dest / "llama-server").read_text()
    assert (dest / "libggml-base.so").read_text() == "GGML-new"
    # … but user files are untouched
    assert (dest / "config.ini").read_text() == "[*]\nkeep = yes\n"
    # the new binary stays executable
    assert os.access(dest / "llama-server", os.X_OK)
    # a backup of the old files exists
    backups = [p for p in dest.iterdir() if p.name.startswith(".upgrade.bak.")]
    assert len(backups) == 1
    assert "version old" in (backups[0] / "llama-server").read_text()
    assert (backups[0] / "config.ini").exists() is False     # only allowlisted files backed up
    # download tarball cleaned up, staging removed
    assert not (broot / "release.download").exists()
    assert not any(p.name.startswith(".upgrade.stage.") for p in dest.iterdir())
    assert any("restart to run the new build" in s for s in seen)


def test_upgrade_aborts_when_smoke_test_fails(tmp_path):
    src = _src(tmp_path, rc=1, marker="bad")              # staged binary exits 1 on --version
    dest = _dest(tmp_path)
    seen = []
    res = lu.upgrade_in_place(str(src / "llama-server"), str(dest / "llama-server"),
                              emit=seen.append)
    assert not res.ok
    # nothing changed: live binary + lib are the originals, no backup, no staging
    assert "version old" in (dest / "llama-server").read_text()
    assert (dest / "libggml-base.so").read_text() == "GGML-old"
    assert not any(p.name.startswith(".upgrade.bak.") for p in dest.iterdir())
    assert not any(p.name.startswith(".upgrade.stage.") for p in dest.iterdir())
    assert any("aborting swap" in s for s in seen)


def test_upgrade_aborts_when_target_not_owned(tmp_path, monkeypatch):
    src = _src(tmp_path)
    dest = _dest(tmp_path)
    monkeypatch.setattr(lu.os, "geteuid", lambda: os.stat(dest).st_uid + 9999)
    seen = []
    res = lu.upgrade_in_place(str(src / "llama-server"), str(dest / "llama-server"),
                              agent_user="svc", emit=seen.append)
    assert not res.ok
    assert "version old" in (dest / "llama-server").read_text()     # untouched
    assert any("writable by the agent user" in s for s in seen)


def test_upgrade_skips_when_source_is_dest(tmp_path):
    dest = _dest(tmp_path)
    res = lu.upgrade_in_place(str(dest / "llama-server"), str(dest / "llama-server"))
    assert res.ok and res.skipped


def test_upgrade_fails_when_built_binary_missing(tmp_path):
    dest = _dest(tmp_path)
    res = lu.upgrade_in_place(str(tmp_path / "nope" / "llama-server"),
                              str(dest / "llama-server"))
    assert not res.ok


def test_upgrade_fails_when_dest_dir_absent(tmp_path):
    src = _src(tmp_path)
    res = lu.upgrade_in_place(str(src / "llama-server"),
                              str(tmp_path / "ghost" / "llama-server"))
    assert not res.ok


def test_upgrade_prunes_old_backups_keeping_retain(tmp_path):
    src = _src(tmp_path)
    dest = _dest(tmp_path)
    (dest / ".upgrade.bak.20200101T000000Z").mkdir()
    (dest / ".upgrade.bak.20200102T000000Z").mkdir()
    res = lu.upgrade_in_place(str(src / "llama-server"), str(dest / "llama-server"),
                              retain=1, emit=lambda _s: None)
    assert res.ok
    backups = sorted(p.name for p in dest.iterdir() if p.name.startswith(".upgrade.bak."))
    assert len(backups) == 1                              # only the freshly-made one survives
    assert "20200101" not in backups[0] and "20200102" not in backups[0]


def test_upgrade_rolls_back_on_partial_swap_failure(tmp_path, monkeypatch):
    src = _src(tmp_path, marker="new")
    dest = _dest(tmp_path)
    real_replace = os.replace
    fwd = {"n": 0}

    def flaky_replace(a, b):
        # count only real forward swaps (staging→dest), not the writability
        # probe (b ends .mv) or rollback (a is under the backup dir)
        if lu._STAGE_PREFIX in str(a) and not str(b).endswith(".mv"):
            fwd["n"] += 1
            if fwd["n"] == 2:
                raise OSError("simulated mid-swap failure")
        return real_replace(a, b)

    monkeypatch.setattr(lu.os, "replace", flaky_replace)
    seen = []
    res = lu.upgrade_in_place(str(src / "llama-server"), str(dest / "llama-server"),
                              emit=seen.append)
    assert not res.ok
    assert any("rolled back" in s for s in seen)
    # every live file is back to its original content — no half-swapped state
    assert "version old" in (dest / "llama-server").read_text()
    assert (dest / "libggml-base.so").read_text() == "GGML-old"


def test_upgrade_aborts_when_dir_not_writable_despite_uid_match(tmp_path, monkeypatch):
    src = _src(tmp_path)
    dest = _dest(tmp_path)
    monkeypatch.setattr(lu, "_probe_writable", lambda _d: False)
    seen = []
    res = lu.upgrade_in_place(str(src / "llama-server"), str(dest / "llama-server"),
                              emit=seen.append)
    assert not res.ok
    assert "version old" in (dest / "llama-server").read_text()
    assert any("writable by the agent user" in s for s in seen)


def test_upgrade_copies_symlink_artifacts_as_symlinks(tmp_path):
    src = _src(tmp_path)
    # libllama.so -> libllama.so.1 (symlink artifact)
    (src / "libllama.so.1").write_text("real")
    os.symlink("libllama.so.1", src / "libllama.so")
    dest = _dest(tmp_path)
    res = lu.upgrade_in_place(str(src / "llama-server"), str(dest / "llama-server"),
                              emit=lambda _s: None)
    assert res.ok
    assert (dest / "libllama.so").is_symlink()
    assert os.readlink(dest / "libllama.so") == "libllama.so.1"
    assert (dest / "libllama.so.1").read_text() == "real"
