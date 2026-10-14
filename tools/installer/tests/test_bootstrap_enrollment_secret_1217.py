"""#1217: fresh manager installs mint [manager.agents].enrollment_secret; re-runs keep it;
Docker and Homebrew seeders write it too."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BOOT = (ROOT / "tools/installer/install-config-bootstrap.sh").read_text()
DOCKER = (ROOT / "docker/render-config.sh").read_text()
BREW = (ROOT / "tools/installer/brew-seed-config.sh").read_text()
EXAMPLE = (ROOT / "config/llm-systems.toml.example").read_text()


def test_example_has_the_key_blank():
    assert re.search(r'^enrollment_secret = ""', EXAMPLE, re.M)


def test_bootstrap_mints_for_manager_hosts_and_keeps_previous():
    assert 'PREV_ENROLLMENT_SECRET="$($SUDO sed -nE \'s/^[[:space:]]*enrollment_secret' in BOOT
    assert 'ENROLLMENT_SECRET="${PREV_ENROLLMENT_SECRET:-$(openssl rand -hex 32)}"' in BOOT
    assert "printf 'ENROLLMENT_SECRET=%s\\n'" in BOOT
    assert 'sub_in_section(text, "manager.agents", "enrollment_secret", enrollment_secret_val)' in BOOT


def test_previous_secret_is_read_before_the_template_overwrites_the_config():
    read_at = BOOT.index('PREV_ENROLLMENT_SECRET="$($SUDO sed')
    assert read_at < BOOT.index('$SUDO cp -a "$EXAMPLE" "$REAL"')
    assert ': "${PREV_ENROLLMENT_SECRET:=}"' in BOOT


def test_bootstrap_prints_secret_and_fingerprint_once():
    assert "Enrollment secret (new agents need it):" in BOOT
    assert "openssl x509 -noout -fingerprint -sha256" in BOOT
    assert BOOT.count("${ENROLLMENT_SECRET}") == 1


def test_docker_renders_the_secret():
    assert 'LSM_ENROLLMENT_SECRET' in DOCKER
    assert re.search(r'^\[manager\.agents\]\nenrollment_secret = "\$enrollment_secret"', DOCKER, re.M)


def test_brew_seeds_the_secret():
    assert "'enrollment_secret = \"\"'*)" in BREW
    assert 'printf \'enrollment_secret = "%s"\\n\' "$ENROLL_SECRET"' in BREW


def test_bootstrap_writer_stores_secret_under_manager_agents(tmp_path):
    body = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF\n", BOOT, re.DOTALL).group(1)
    script = tmp_path / "subst.py"
    script.write_text(body)
    toml = tmp_path / "llm-systems.toml"
    toml.write_text(EXAMPLE)
    secret = "ab" * 32
    (tmp_path / "vals").write_text(f"HAS_MGR=1\nHAS_AE=1\nENROLLMENT_SECRET={secret}\n")
    proc = subprocess.run([sys.executable, str(script), str(toml), str(tmp_path / "vals")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    out = toml.read_text()
    section = re.search(r"^\[manager\.agents\]\n(.*?)(?=^\[)", out, re.M | re.S).group(1)
    line = next(l for l in section.splitlines() if l.startswith("enrollment_secret"))
    assert line.startswith(f'enrollment_secret = "{secret}"')
    assert "# new machines must send this to register" in line
    assert out.count(secret) == 1


def test_docker_resolve_appends_secret_once_to_existing_store(tmp_path):
    func = re.search(r"^resolve_ae_tokens\(\) \{.*?^\}", DOCKER, re.M | re.S).group(0)
    store = tmp_path / "docker-tokens.env"
    store.write_text("LSM_AE_INGEST_TOKEN=" + "1" * 64 + "\nLSM_AE_MANAGEMENT_TOKEN=" + "2" * 64 + "\n")
    script = (f'set -euo pipefail\nTOKEN_STORE="{store}"\n_new_token() {{ echo {"c" * 64}; }}\n{func}\n'
              'resolve_ae_tokens >/dev/null\necho "$ENROLLMENT_SECRET"\n'
              'resolve_ae_tokens >/dev/null\necho "$ENROLLMENT_SECRET"\n')
    env = {"PATH": "/usr/bin:/bin"}
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["c" * 64, "c" * 64]
    assert store.read_text().count("LSM_ENROLLMENT_SECRET=") == 1


def test_deb_asks_for_the_secret_at_high_priority():
    cfg = (ROOT / "tools/packaging/agent/debconf/config").read_text()
    assert "db_input high llm-systems-agent/enrollment-secret" in cfg
    assert "db_reset llm-systems-agent/enrollment-secret" in (ROOT / "tools/packaging/agent/scripts/deb/postinst").read_text()
