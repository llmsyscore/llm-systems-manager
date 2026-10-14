"""#1204/#1217 installer: fingerprint normalising, CA pinning, secret file placement."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"
SRC = INSTALL_SH.read_text()


def _func(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from install.sh"
    return m.group(0)


def _bash(script: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, **(env or {})})


def test_fingerprint_forms_normalise_to_one_value():
    fn = _func("_normalize_fingerprint")
    hexd = "ab" * 32
    colon = ":".join(hexd[i:i + 2] for i in range(0, 64, 2)).upper()
    for form in (hexd, hexd.upper(), "sha256:" + hexd, "SHA256:" + colon, colon, " " + hexd + "\n"):
        r = _bash(f"{fn}\n_normalize_fingerprint '{form}'")
        assert r.stdout.strip() == hexd, form
    for bad in ("", "abc", "g" * 64, "sha1:" + hexd):
        r = _bash(f"{fn}\n_normalize_fingerprint '{bad}'")
        assert r.stdout.strip() == "" and r.returncode != 0, bad


def _ca(tmp_path) -> Path:
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "ca.key"),
                    "-out", str(tmp_path / "ca.crt"), "-days", "2", "-subj", "/CN=t"], check=True, capture_output=True)
    return tmp_path / "ca.crt"


def _fp(crt: Path) -> str:
    out = subprocess.check_output(["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(crt)], text=True)
    return out.split("=", 1)[1].strip().replace(":", "").lower()


def _pin_script(tmp_path, response: dict, fingerprint: str) -> str:
    (tmp_path / "resp.json").write_text(json.dumps(response))
    return "\n".join([
        _func("_normalize_fingerprint"), _func("_pin_manager_ca"), _func("_install_pinned_ca"), _func("_place_file"),
        f'INSTALL_DIR="{tmp_path}/inst"; MANAGER_URL="http://10.0.0.1:5000"; CA_FINGERPRINT="{fingerprint}"',
        'SUDO=""; USER_ARG="$(id -un)"; USER_GROUP="$(id -gn)"',
        f'_probe_pki_ca() {{ cat "{tmp_path}/resp.json"; }}',
        'CA_PINNED_FILE=""',
        "_pin_manager_ca && echo MANAGER_URL=$MANAGER_URL CA_PINNED_FILE=$CA_PINNED_FILE",
    ])


def test_pin_writes_ca_and_switches_to_https(tmp_path):
    crt = _ca(tmp_path)
    resp = {"ok": True, "ca_pem": crt.read_text(), "fingerprint": "sha256:" + _fp(crt), "https_url": "https://10.0.0.1:5443"}
    r = _bash(_pin_script(tmp_path, resp, _fp(crt).upper()))
    assert r.returncode == 0, r.stderr
    assert "MANAGER_URL=https://10.0.0.1:5443" in r.stdout
    assert (tmp_path / "inst" / "data" / "tls-ca.pem").read_text() == crt.read_text()
    assert f"CA_PINNED_FILE={tmp_path}/inst/data/tls-ca.pem" in r.stdout


def test_pin_empty_ca_pem_is_a_clear_error(tmp_path):
    resp = {"ok": True, "ca_pem": "", "fingerprint": "", "https_url": "https://10.0.0.1:5443"}
    r = _bash(_pin_script(tmp_path, resp, "ab" * 32))
    assert r.returncode != 0
    assert "no ca certificate" in r.stderr.lower() and "Traceback" not in r.stderr
    assert not (tmp_path / "inst" / "data" / "tls-ca.pem").exists()


def test_pin_mismatch_writes_nothing(tmp_path):
    crt = _ca(tmp_path)
    resp = {"ok": True, "ca_pem": crt.read_text(), "fingerprint": "sha256:" + _fp(crt), "https_url": "https://10.0.0.1:5443"}
    r = _bash(_pin_script(tmp_path, resp, "0" * 64))
    assert r.returncode != 0
    assert "fingerprint" in (r.stderr + r.stdout).lower()
    assert not (tmp_path / "inst" / "data" / "tls-ca.pem").exists()


def test_pin_with_tls_off_keeps_http_but_stores_ca(tmp_path):
    crt = _ca(tmp_path)
    resp = {"ok": True, "ca_pem": crt.read_text(), "fingerprint": "sha256:" + _fp(crt), "https_url": ""}
    r = _bash(_pin_script(tmp_path, resp, _fp(crt)))
    assert r.returncode == 0 and "MANAGER_URL=http://10.0.0.1:5000" in r.stdout
    assert (tmp_path / "inst" / "data" / "tls-ca.pem").is_file()


def test_pin_without_openssl_is_a_clear_error(tmp_path):
    script = _pin_script(tmp_path, {"ok": True}, "ab" * 32).replace(
        "_pin_manager_ca && echo", 'command() { [[ "${2:-}" == openssl ]] && return 1; builtin command "$@"; }\n'
        "_pin_manager_ca && echo")
    r = _bash(script)
    assert r.returncode != 0
    assert "needs curl and openssl" in r.stderr and "does not match" not in r.stderr


def test_manager_url_rewrite_changes_only_that_line(tmp_path):
    cfg = tmp_path / "agent_config.yaml"
    before = '# header\nAGENT_PORT: 9100\nMANAGER_URL: "http://10.0.0.1:5000"   # comment\nALARM_ENGINE_URL: "http://10.0.0.1:8081"\n'
    plain = tmp_path.parent / (tmp_path.name + "-plain.yaml")
    plain.write_text('A: 1\nMANAGER_URL: http://10.0.0.1:5000\nB: 2\n')
    r = _bash(f'{_func("_set_manager_url_in_config")}\nSUDO=""\n'
              f'_set_manager_url_in_config "{plain}" "https://10.0.0.1:5443"')
    assert r.returncode == 0, r.stderr
    assert plain.read_text() == 'A: 1\nMANAGER_URL: "https://10.0.0.1:5443"\nB: 2\n'
    plain.unlink()
    cfg.write_text(before)
    cfg.chmod(0o600)
    r = _bash(f'{_func("_set_manager_url_in_config")}\nSUDO=""\n'
              f'_set_manager_url_in_config "{cfg}" "https://10.0.0.1:5443"')
    assert r.returncode == 0, r.stderr
    after = before.replace('MANAGER_URL: "http://10.0.0.1:5000"   # comment',
                           'MANAGER_URL: "https://10.0.0.1:5443"   # comment')
    assert cfg.read_text() == after
    assert (cfg.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["agent_config.yaml"]


def test_update_honours_ca_fingerprint():
    assert "applies to new installs only" not in SRC
    upd = SRC[SRC.index("if $DO_UPDATE; then"):SRC.index("Fetching latest agent/ from upstream")]
    assert "_pin_manager_ca || exit 1" in upd
    assert upd.index("_install_pinned_ca") < upd.index("_set_manager_url_in_config")
    assert SRC.index("_pin_manager_ca() {") < SRC.index("if $DO_UPDATE; then")
    fetch = _func("_fetch_agent_into")
    curls = [ln for ln in fetch.splitlines() if re.search(r"\bcurl -", ln) and not ln.lstrip().startswith("#")]
    assert len(curls) == 2
    assert '--cacert "$_cacert"' in curls[0] and curls[0].lstrip().startswith('if [[ -n "$_cacert" ]] &&')
    assert "--cacert" not in curls[1] and curls[1].lstrip().startswith("elif curl")
    for ln in curls:
        assert '-K "$tmpdir/curl-auth.cfg"' in ln and "$tok" not in ln and "Bearer" not in ln
    assert "internal CA" in fetch and "trusted certificates" in fetch


def test_secret_file_written_0600_and_kept_on_update(tmp_path):
    fn = _func("_write_enrollment_secret") + "\n" + _func("_place_file")
    inst = tmp_path / "inst"
    (inst / "data").mkdir(parents=True)
    base = f'umask 022\n{fn}\nINSTALL_DIR="{inst}"; SUDO=""; USER_ARG="$(id -un)"; USER_GROUP="$(id -gn)"'
    r = _bash(base + '\nENROLLMENT_SECRET="first"; DO_UPDATE=false; _write_enrollment_secret')
    assert r.returncode == 0, r.stderr
    p = inst / "data" / "enrollment-secret"
    assert p.read_text() == "first" and (p.stat().st_mode & 0o777) == 0o600
    r = _bash(base + '\nENROLLMENT_SECRET=""; DO_UPDATE=true; _write_enrollment_secret')
    assert r.returncode == 0 and p.read_text() == "first"
    r = _bash(base + '\nENROLLMENT_SECRET="second"; DO_UPDATE=true; _write_enrollment_secret')
    assert r.returncode == 0 and p.read_text() == "second"
    p.unlink()
    r = _bash(base + '\nchmod() { :; }; ENROLLMENT_SECRET="third"; DO_UPDATE=false; _write_enrollment_secret')
    assert r.returncode == 0, r.stderr
    assert (p.stat().st_mode & 0o777) == 0o600, "created world-readable before chmod"


def test_planted_symlinks_are_replaced_not_followed(tmp_path):
    """A symlink left at the destination must not make the (root) write land elsewhere."""
    inst = tmp_path / "inst"
    (inst / "data").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (inst / "data" / "enrollment-secret").symlink_to(victim)
    fn = _func("_write_enrollment_secret") + "\n" + _func("_place_file")
    base = f'{fn}\nINSTALL_DIR="{inst}"; SUDO=""; USER_ARG="$(id -un)"; USER_GROUP="$(id -gn)"'
    r = _bash(base + '\nENROLLMENT_SECRET="s3cret"; DO_UPDATE=false; _write_enrollment_secret')
    assert r.returncode == 0, r.stderr
    dest = inst / "data" / "enrollment-secret"
    assert not dest.is_symlink() and dest.read_text() == "s3cret"
    assert victim.read_text() == "untouched"
    assert not list(tmp_path.glob(".llmsys-place.*"))
    crt = _ca(tmp_path)
    (inst / "data" / "tls-ca.pem").symlink_to(victim)
    resp = {"ok": True, "ca_pem": crt.read_text(), "fingerprint": "sha256:" + _fp(crt), "https_url": ""}
    r = _bash(_pin_script(tmp_path, resp, _fp(crt)))
    assert r.returncode == 0, r.stderr
    ca = inst / "data" / "tls-ca.pem"
    assert not ca.is_symlink() and ca.read_text() == crt.read_text()
    assert victim.read_text() == "untouched"
    assert not list(tmp_path.glob(".llmsys-place.*"))


def test_flags_and_env_are_parsed():
    assert "--enrollment-secret)" in SRC and "--ca-fingerprint)" in SRC
    assert 'ENROLLMENT_SECRET="${LLMSYS_ENROLLMENT_SECRET:-}"' in SRC
    assert 'CA_FINGERPRINT="${LLMSYS_CA_FINGERPRINT:-}"' in SRC


def test_secret_never_echoed():
    for line in SRC.splitlines():
        if line.strip() == 'printf \'%s\' "$ENROLLMENT_SECRET" | _place_file "$dest" 0600':
            continue
        if "ENROLLMENT_SECRET" in line and re.search(r"\b(echo|printf|log|_ok|_warn)\b", line):
            assert "$ENROLLMENT_SECRET" not in line and "${ENROLLMENT_SECRET" not in line, line
