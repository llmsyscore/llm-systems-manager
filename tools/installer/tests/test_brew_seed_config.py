# brew-seed-config.sh: seeds a fresh config; on an upgrade adds new example keys
# and the https entries to an existing config, keeping a .bak copy next to it.
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


def seed(tmp_path, script=SCRIPT, shim_dir=None):
    cfg = tmp_path / "etc" / "llm-systems.toml"
    path = f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin"
    if shim_dir is not None:
        path = f"{shim_dir}:{path}"
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={"PATH": path, "LSM_BREW_EXAMPLE": str(EXAMPLE),
             "LSM_BREW_CONFIG": str(cfg), "LSM_BREW_LOG_DIR": str(tmp_path / "log")},
    )
    assert proc.returncode == 0, proc.stderr
    return cfg, proc.stdout + proc.stderr


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
    old = cfg.read_text()
    _, out = seed(tmp_path)
    assert "already exists" in out
    assert cfg.read_text() == seeded
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    baks = [p for p in cfg.parent.iterdir() if p != cfg]
    assert len(baks) == 1 and baks[0].name.startswith("llm-systems.toml.bak.")
    assert baks[0].read_text() == old


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
    _, out = seed(tmp_path)
    assert "could not merge new keys" in out
    assert cfg.read_text() == "[alarm_engine\ncors_origins = \"http://localhost:5000\"\n"
    assert list(cfg.parent.iterdir()) == [cfg]


def test_upgrade_keeps_the_config_when_the_helper_lacks_origins(tmp_path):
    keg = tmp_path / "keg" / "tools" / "installer"
    keg.mkdir(parents=True)
    shutil.copy(SCRIPT, keg / SCRIPT.name)
    (keg / "toml_reconcile.py").write_text(
        "import sys\n"
        "if sys.argv[1] == 'merge':\n"
        "    sys.stdout.write(open(sys.argv[2]).read()); sys.stderr.write('ADDED=0\\n'); sys.exit(0)\n"
        "sys.stderr.write('usage\\n'); sys.exit(64)\n")
    cfg, _ = seed(tmp_path, keg / SCRIPT.name)
    old = cfg.read_text().replace(NEW_LIST, OLD_LIST)
    cfg.write_text(old)
    _, out = seed(tmp_path, keg / SCRIPT.name)
    assert "could not" not in out
    assert cfg.read_text() == old
    assert list(cfg.parent.iterdir()) == [cfg]


def test_upgrade_keeps_the_config_when_the_backup_cannot_be_written(tmp_path):
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "cp").write_text("#!/usr/bin/env bash\nexit 1\n")
    (shim / "cp").chmod(0o755)
    cfg, _ = seed(tmp_path)
    _drop_line(cfg, "ca_file")
    cfg.write_text(cfg.read_text().replace(NEW_LIST, OLD_LIST))
    old = cfg.read_text()
    _, out = seed(tmp_path, shim_dir=shim)
    assert "could not write the merged config" in out
    assert "could not write the allowed-origins fix" in out
    assert cfg.read_text() == old
    assert list(cfg.parent.iterdir()) == [cfg]



def _drop_line(cfg, prefix):
    lines = cfg.read_text().splitlines(keepends=True)
    kept = [ln for ln in lines if not ln.startswith(prefix)]
    assert len(kept) == len(lines) - 1
    cfg.write_text("".join(kept))


def _backups(cfg):
    return sorted(p for p in cfg.parent.iterdir() if p.name.startswith(cfg.name + ".bak."))


def test_upgrade_adds_a_new_key_and_keeps_a_backup_next_to_the_config(tmp_path):
    cfg, _ = seed(tmp_path)
    _drop_line(cfg, "ca_file")
    old = cfg.read_text()
    assert "ca_file" not in tomllib.loads(old)["notifications"]["smtp"]
    _, out = seed(tmp_path)
    assert "merged 1 new key" in out
    parsed = tomllib.loads(cfg.read_text())
    example = tomllib.loads(EXAMPLE.read_text())
    assert parsed["notifications"]["smtp"]["ca_file"] == example["notifications"]["smtp"]["ca_file"]
    assert parsed["alarm_engine"]["cors_origins"] == NEW_LIST
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    baks = _backups(cfg)
    assert len(baks) == 1 and baks[0].read_text() == old
    assert oct(baks[0].stat().st_mode & 0o777) == "0o600"
    assert sorted(cfg.parent.iterdir()) == sorted([cfg, baks[0]])


def test_upgrade_merge_and_origins_fix_share_one_backup(tmp_path):
    cfg, _ = seed(tmp_path)
    _drop_line(cfg, "enrollment_window_min")
    cfg.write_text(cfg.read_text().replace(NEW_LIST, OLD_LIST))
    old = cfg.read_text()
    _, out = seed(tmp_path)
    assert "merged 1 new key" in out and "added https" in out
    parsed = tomllib.loads(cfg.read_text())
    example = tomllib.loads(EXAMPLE.read_text())
    assert parsed["manager"]["agents"]["enrollment_window_min"] == example["manager"]["agents"]["enrollment_window_min"]
    assert parsed["alarm_engine"]["cors_origins"] == NEW_LIST
    baks = _backups(cfg)
    assert len(baks) == 1 and baks[0].read_text() == old


def test_upgrade_keeps_operator_values_when_merging(tmp_path):
    cfg, _ = seed(tmp_path)
    text = cfg.read_text()
    tokens = tomllib.loads(text)["alarm_engine"]
    _drop_line(cfg, "ca_file")
    seed(tmp_path)
    after = tomllib.loads(cfg.read_text())["alarm_engine"]
    assert after["ingest_token"] == tokens["ingest_token"]
    assert after["management_token"] == tokens["management_token"]
