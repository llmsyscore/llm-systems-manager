"""svcconfig_op / svcconfig_apply(unit=) stdin protocol, with a fake wrapper."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from providers import _shared  # noqa: E402


@pytest.fixture
def fake(tmp_path, monkeypatch):
    out = tmp_path / "stdin.log"
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("#!/bin/sh\n# SVCCONFIG_WRAPPER_VERSION=3\n"
                       "UNIT_PATH='/etc/systemd/system/baked.service'\n"
                       f"cat >> '{out}'\necho '--' >> '{out}'\n")
    wrapper.chmod(0o755)
    real = subprocess.run

    def run(cmd, **kw):
        # Drop the sudo prefix so the fake wrapper runs unprivileged.
        return real(cmd[2:] if cmd[:2] == ["sudo", "-n"] else cmd, **kw)

    monkeypatch.setattr(_shared.subprocess, "run", run)
    return wrapper, out


def test_svcconfig_op_sends_header(fake):
    wrapper, out = fake
    assert _shared.svcconfig_op(str(wrapper), "restart", "llama-server.service")["ok"]
    assert out.read_text() == "op=restart unit=llama-server.service\n--\n"


def test_svcconfig_apply_with_unit_sends_header_and_skips_baked_guard(fake):
    wrapper, out = fake
    res = _shared.svcconfig_apply(str(wrapper), "/other/path.service",
                                  ["/usr/bin/llama-server", "--port", "1"], "hint",
                                  restart_unit="llama-server.service",
                                  unit="llama-server.service")
    assert res == {"ok": True}
    assert out.read_text() == ("op=apply unit=llama-server.service\n"
                               "/usr/bin/llama-server\n--port\n1\n--\n"
                               "op=restart unit=llama-server.service\n--\n")


def test_svcconfig_apply_without_unit_keeps_legacy_payload(fake):
    wrapper, out = fake
    res = _shared.svcconfig_apply(str(wrapper), "/etc/systemd/system/baked.service",
                                  ["/usr/bin/llama-server", "--port", "1"], "hint")
    assert res == {"ok": True}
    assert out.read_text() == "/usr/bin/llama-server\n--port\n1\n--\n"
    res = _shared.svcconfig_apply(str(wrapper), "/other.service", ["/x"], "hint")
    assert not res["ok"] and "baked for" in res["error"]


def test_wrapper_version(fake, tmp_path):
    assert _shared.wrapper_version(str(fake[0])) == 3
    assert _shared.wrapper_version(str(tmp_path / "missing")) == 0
