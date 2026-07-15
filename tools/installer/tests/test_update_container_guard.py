"""guard_not_containerized(): refuse updates in containers / systemd-less hosts."""
import os
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib-common.sh"


def run_guard(tmp_path, allow=""):
    env = dict(os.environ, LLMSYS_CONTAINER_PROBE_ROOT=str(tmp_path),
               LLMSYS_ALLOW_CONTAINER=allow)
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"; guard_not_containerized'],
        env=env, capture_output=True, text=True)


def fake_host(tmp_path, systemd=True, cgroup="0::/init.scope\n"):
    if systemd:
        (tmp_path / "run/systemd/system").mkdir(parents=True)
    (tmp_path / "proc/1").mkdir(parents=True)
    (tmp_path / "proc/1/cgroup").write_text(cgroup)
    return tmp_path


def test_bare_metal_passes(tmp_path):
    r = run_guard(fake_host(tmp_path))
    assert r.returncode == 0, r.stderr


def test_dockerenv_refused(tmp_path):
    fake_host(tmp_path)
    (tmp_path / ".dockerenv").touch()
    r = run_guard(tmp_path)
    assert r.returncode == 2
    assert "docker compose pull" in r.stderr


def test_podman_containerenv_refused(tmp_path):
    fake_host(tmp_path)
    (tmp_path / "run/.containerenv").touch()
    assert run_guard(tmp_path).returncode == 2


def test_docker_cgroup_refused(tmp_path):
    fake_host(tmp_path, cgroup="0::/system.slice/docker-abc123.scope\n")
    r = run_guard(tmp_path)
    assert r.returncode == 2
    assert "docker compose pull" in r.stderr


def test_no_systemd_refused(tmp_path):
    r = run_guard(fake_host(tmp_path, systemd=False))
    assert r.returncode == 2
    assert "systemd" in r.stderr


def test_override_allows(tmp_path):
    fake_host(tmp_path)
    (tmp_path / ".dockerenv").touch()
    assert run_guard(tmp_path, allow="1").returncode == 0
