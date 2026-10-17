from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / "install-influxdb.sh").read_text()


def test_dnf_branch_writes_repo_file():
    blk = TEXT[TEXT.index('if [[ "$LLMSYS_PKG_MGR" == "dnf" ]]; then'):TEXT.index("banner \"InfluxDB — start service\"")]
    assert "/etc/yum.repos.d/influxdata.repo" in blk
    assert "baseurl = https://repos.influxdata.com/stable/$basearch/main" in blk
    assert "gpgcheck = 1" in blk
    assert "gpgkey = https://repos.influxdata.com/influxdata-archive.key" in blk
    assert "$SUDO dnf install -y influxdb2 influxdb2-cli" in blk
    assert "apt-get" not in blk.split("else", 1)[0]


def test_apt_branch_unchanged():
    assert "deb [signed-by=$KEYRING] https://repos.influxdata.com/debian stable main" in TEXT
    assert "apt_update_with_clock_recovery" in TEXT


def test_prereqs_keep_curl_jq_openssl():
    assert "APT_PKGS=(wget gnupg ca-certificates curl jq openssl)" in TEXT
