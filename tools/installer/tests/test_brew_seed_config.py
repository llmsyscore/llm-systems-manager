# brew-seed-config.sh: seeds a fresh config, and on an upgrade adds the https
# entries to an existing config's [alarm_engine].cors_origins.
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1]
SCRIPT = INSTALLER / "brew-seed-config.sh"
EXAMPLE = INSTALLER.parents[1] / "config" / "llm-systems.toml.example"

OLD_LIST = "http://localhost:5000,http://localhost:8081"
NEW_LIST = "http://localhost:5000,https://localhost:5443,http://localhost:8081,https://localhost:8081"


def seed(tmp_path, script=SCRIPT):
    cfg = tmp_path / "etc" / "llm-systems.toml"
    path = f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin"
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={"PATH": path, "LSM_BREW_EXAMPLE": str(EXAMPLE),
             "LSM_BREW_CONFIG": str(cfg), "LSM_BREW_LOG_DIR": str(tmp_path / "log")},
    )
    assert proc.returncode == 0, proc.stderr
    return cfg, proc.stdout


def origins_in(cfg):
    return tomllib.loads(cfg.read_text())["alarm_engine"]["cors_origins"]


def test_fresh_seed_lists_http_and_https(tmp_path):
    cfg, _ = seed(tmp_path)
    assert origins_in(cfg) == NEW_LIST
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"


def test_upgrade_adds_https_to_an_existing_config(tmp_path):
    cfg, _ = seed(tmp_path)
    seeded = cfg.read_text()
    cfg.write_text(seeded.replace(NEW_LIST, OLD_LIST))
    assert origins_in(cfg) == OLD_LIST
    _, out = seed(tmp_path)
    assert "already exists" in out
    assert cfg.read_text() == seeded
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    assert list(cfg.parent.iterdir()) == [cfg]


def test_upgrade_leaves_a_current_config_untouched(tmp_path):
    cfg, _ = seed(tmp_path)
    before = (cfg.read_text(), cfg.stat().st_mtime_ns)
    _, out = seed(tmp_path)
    assert "added https" not in out
    assert (cfg.read_text(), cfg.stat().st_mtime_ns) == before
    assert list(cfg.parent.iterdir()) == [cfg]


def test_upgrade_keeps_a_config_that_does_not_parse(tmp_path):
    cfg, _ = seed(tmp_path)
    cfg.write_text("[alarm_engine\ncors_origins = \"http://localhost:5000\"\n")
    seed(tmp_path)
    assert cfg.read_text() == "[alarm_engine\ncors_origins = \"http://localhost:5000\"\n"
    assert list(cfg.parent.iterdir()) == [cfg]


def test_upgrade_keeps_the_config_when_the_helper_lacks_origins(tmp_path):
    keg = tmp_path / "keg" / "tools" / "installer"
    keg.mkdir(parents=True)
    shutil.copy(SCRIPT, keg / SCRIPT.name)
    (keg / "toml_reconcile.py").write_text("import sys\nsys.stderr.write('usage\\n')\nsys.exit(64)\n")
    cfg, _ = seed(tmp_path, keg / SCRIPT.name)
    old = cfg.read_text().replace(NEW_LIST, OLD_LIST)
    cfg.write_text(old)
    seed(tmp_path, keg / SCRIPT.name)
    assert cfg.read_text() == old
    assert list(cfg.parent.iterdir()) == [cfg]

