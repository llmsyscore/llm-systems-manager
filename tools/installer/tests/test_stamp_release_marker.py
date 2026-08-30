"""stamp_release_marker(): .llmsys-release → git describe → leave staged copy."""
import os
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib-common.sh"

PLACEHOLDER = "$Format:%(describe:tags)$\n"


def stamp(tmp_path, src_files, dest_files=None, git_tag=None):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    for name, body in src_files.items():
        (src / name).write_text(body)
    for name, body in (dest_files or {}).items():
        (dest / name).write_text(body)
    env = dict(os.environ, SUDO="", LLMSYS_INSTALL_DIR=str(dest))
    if git_tag is not None:
        run_git = lambda *a: subprocess.run(  # noqa: E731
            ["git", "-C", str(src), *a], capture_output=True, check=True)
        run_git("init", "-q")
        run_git("config", "user.email", "t@example.invalid")
        run_git("config", "user.name", "t")
        run_git("add", "-A")
        run_git("commit", "-qm", "c")
        run_git("tag", git_tag)
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; stamp_release_marker "{src}" "{dest}"'],
        env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    marker = dest / "RELEASE"
    return (marker.read_text().strip() if marker.exists() else None), r.stderr


def test_release_tarball_marker_wins(tmp_path):
    """A staged release tree names its tag in .llmsys-release."""
    got, _ = stamp(tmp_path,
                   {"RELEASE": "v1.4.0\n", ".llmsys-release": "v1.4.0\n"})
    assert got == "v1.4.0"


def test_git_staging_clone_replaces_the_placeholder(tmp_path):
    """The bug in #757: a git clone keeps RELEASE unsubstituted, so the
    deployed copy could never name its release."""
    got, _ = stamp(tmp_path, {"RELEASE": PLACEHOLDER}, git_tag="v1.4.0")
    assert got is not None
    assert not got.startswith("$Format:")
    assert got.split("-")[0] == "v1.4.0"


def test_stale_marker_is_refreshed(tmp_path):
    """update.sh re-stamps every run, so an install does not stay pinned to
    the tag it was first deployed at."""
    got, _ = stamp(tmp_path,
                   {"RELEASE": "v1.4.0\n", ".llmsys-release": "v1.4.0\n"},
                   dest_files={"RELEASE": "v1.3.0\n"})
    assert got == "v1.4.0"


def test_unpacked_tarball_without_the_staging_marker(tmp_path):
    """--source local: no .llmsys-release and no .git, but git archive already
    substituted RELEASE — an update must still refresh a stale dest marker."""
    got, _ = stamp(tmp_path, {"RELEASE": "v1.4.0\n"},
                   dest_files={"RELEASE": "v1.3.0\n"})
    assert got == "v1.4.0"


def test_placeholder_in_src_is_not_treated_as_a_tag(tmp_path):
    got, _ = stamp(tmp_path, {"RELEASE": PLACEHOLDER}, git_tag="v1.4.0")
    assert got.split("-")[0] == "v1.4.0"


def test_untagged_source_leaves_the_staged_copy_and_warns(tmp_path):
    """No tag anywhere: keep whatever rsync deployed, and say so."""
    got, err = stamp(tmp_path, {"RELEASE": PLACEHOLDER},
                     dest_files={"RELEASE": PLACEHOLDER})
    assert got == PLACEHOLDER.strip()
    assert "could not determine a release tag" in err
