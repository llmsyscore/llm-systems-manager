# The package upgrade and the Docker entrypoint both add the https twins to
# [alarm_engine].cors_origins; each is driven here through its own shell code.
import os
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "tools" / "packaging" / "scripts" / "common.sh"
RENDER = REPO / "docker" / "render-config.sh"
EXAMPLE = REPO / "config" / "llm-systems.toml.example"

OLD_LIST = "http://192.0.2.10:5000,http://localhost:5000,http://192.0.2.10:8081"
NEW_LIST = (
    "http://192.0.2.10:5000,https://192.0.2.10:5443,"
    "http://localhost:5000,https://localhost:5443,"
    "http://192.0.2.10:8081,https://192.0.2.10:8081"
)


def origins_in(path):
    return tomllib.loads(Path(path).read_text())["alarm_engine"]["cors_origins"]


def test_package_upgrade_adds_https_origins(tmp_path):
    root = tmp_path / "opt"
    (root / "config").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "tools" / "installer").symlink_to(REPO / "tools" / "installer")
    example = EXAMPLE.read_text()
    (root / "config" / "llm-systems.toml.example").write_text(example)
    default = tomllib.loads(example)["alarm_engine"]["cors_origins"]
    cfg = root / "config" / "llm-systems.toml"
    cfg.write_text(example.replace(f'cors_origins = "{default}"',
                                   f'cors_origins = "{OLD_LIST}"'))
    script = (
        f'. "{COMMON}"; LLMSYS_INSTALL_DIR="{root}"; LLMSYS_CFG="{cfg}"; '
        'LLMSYS_RUN_USER="$(id -un)"; llmsys_reconcile_config'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert origins_in(cfg) == NEW_LIST
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"


def render(tmp_path, **env):
    cfg = tmp_path / "llm-systems.toml"
    proc = subprocess.run(
        ["bash", "-c", f'. "{RENDER}"; render_config'],
        capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "LLM_SYSTEMS_CONFIG": str(cfg), **env},
    )
    assert proc.returncode == 0, proc.stderr
    return cfg


def test_docker_default_origins_gain_https(tmp_path):
    cfg = render(tmp_path)
    assert origins_in(cfg) == "http://localhost:5000,https://localhost:5443"
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"


def test_docker_operator_origins_gain_https(tmp_path):
    cfg = render(tmp_path, LSM_CORS_ORIGINS=OLD_LIST)
    assert origins_in(cfg) == NEW_LIST
