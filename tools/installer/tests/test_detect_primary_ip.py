"""detect_primary_ip(): override → default-route src → non-virtual NIC → hostname -I."""
import os
import stat
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib-common.sh"


def fake_bin(tmp_path, route="", addrs="", hostname_i=""):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "route.out").write_text(route)
    (b / "addrs.out").write_text(addrs)
    (b / "hostname.out").write_text(hostname_i)
    ip = b / "ip"
    ip.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *"route get"*) cat "{b}/route.out" ;;\n'
        f'  *"addr show"*) cat "{b}/addrs.out" ;;\n'
        "esac\n"
    )
    hn = b / "hostname"
    hn.write_text(f'#!/usr/bin/env bash\ncat "{b}/hostname.out"; echo\n')
    for f in (ip, hn):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return b


def detect(tmp_path, env_extra=None, **fake):
    b = fake_bin(tmp_path, **fake)
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}")
    env.pop("LLMSYS_PRIMARY_IP", None)
    env.update(env_extra or {})
    r = subprocess.run(["bash", "-c", f'. "{LIB}"; detect_primary_ip'],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


ADDRS_DOCKER_FIRST = (
    "1: lo    inet 127.0.0.1/8 scope host lo\n"
    "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n"
    "5: br-1a2b3c    inet 172.18.0.1/16 scope global br-1a2b3c\n"
    "2: ens18    inet 192.168.1.59/24 brd 192.168.1.255 scope global dynamic ens18\n"
)


def test_override_wins(tmp_path):
    out = detect(tmp_path, env_extra={"LLMSYS_PRIMARY_IP": "10.9.8.7"},
                 route="1.1.1.1 via 172.17.0.1 dev docker0 src 172.17.0.1 uid 0",
                 addrs=ADDRS_DOCKER_FIRST)
    assert out == "10.9.8.7"


def test_default_route_source(tmp_path):
    out = detect(tmp_path,
                 route="1.1.1.1 via 192.168.1.1 dev ens18 src 192.168.1.59 uid 1000 \n    cache ",
                 addrs=ADDRS_DOCKER_FIRST, hostname_i="172.17.0.1 192.168.1.59")
    assert out == "192.168.1.59"


def test_no_default_route_skips_bridges(tmp_path):
    out = detect(tmp_path, route="", addrs=ADDRS_DOCKER_FIRST,
                 hostname_i="172.17.0.1 172.18.0.1 192.168.1.59")
    assert out == "192.168.1.59"


def test_only_bridge_falls_back_to_it(tmp_path):
    addrs = "3: docker0    inet 172.17.0.1/16 scope global docker0\n"
    assert detect(tmp_path, route="", addrs=addrs, hostname_i="172.17.0.1") == "172.17.0.1"


def test_link_local_skipped(tmp_path):
    addrs = ("2: ens18    inet 169.254.10.10/16 scope global ens18\n"
             "4: ens19    inet 10.0.0.5/24 scope global ens19\n")
    assert detect(tmp_path, route="", addrs=addrs) == "10.0.0.5"


def test_hostname_fallback(tmp_path):
    assert detect(tmp_path, route="", addrs="", hostname_i="10.1.2.3 fd00::1") == "10.1.2.3"


def test_loopback_last_resort(tmp_path):
    assert detect(tmp_path, route="", addrs="", hostname_i="") == "127.0.0.1"
