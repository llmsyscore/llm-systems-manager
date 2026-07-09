# Protocol-level tests for toml_reconcile.py: exercises the merge/prune CLI
# exactly as update.sh consumes it (argv, exit codes, stdout TOML, stderr tags).
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "toml_reconcile.py"


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
    )


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


LIVE_BASIC = """\
[manager]
port = 5000
poll_interval = 5

[alarm_engine.timeouts]
manager_status = 2.0
"""

EXAMPLE_BASIC = """\
[manager]
port = 5000
# TLS listener port.
tls_port = 5443
poll_interval = 5

[alarm_engine.timeouts]
manager_status = 2.0
"""


class TestMerge:
    def test_noop_when_in_sync(self, tmp_path):
        live = write(tmp_path, "live.toml", LIVE_BASIC)
        example = write(tmp_path, "ex.toml", LIVE_BASIC)
        r = run("merge", live, example)
        assert r.returncode == 0
        assert "ADDED=0" in r.stderr
        assert r.stdout == LIVE_BASIC

    def test_positional_splice_with_comment_block(self, tmp_path):
        live = write(tmp_path, "live.toml", LIVE_BASIC)
        example = write(tmp_path, "ex.toml", EXAMPLE_BASIC)
        r = run("merge", live, example)
        assert r.returncode == 0
        assert "ADDED=1" in r.stderr
        assert "+ manager.tls_port" in r.stderr
        merged = tomllib.loads(r.stdout)
        assert merged["manager"]["tls_port"] == 5443
        lines = r.stdout.splitlines()
        port_i = lines.index("port = 5000")
        tls_i = next(i for i, ln in enumerate(lines) if ln.startswith("tls_port"))
        poll_i = lines.index("poll_interval = 5")
        assert port_i < tls_i < poll_i
        assert lines[tls_i - 1] == "# TLS listener port."

    def test_new_section_appended_with_only_missing_keys(self, tmp_path):
        live = write(tmp_path, "live.toml", LIVE_BASIC)
        example = write(
            tmp_path,
            "ex.toml",
            LIVE_BASIC + "\n[manager.benchmark]\nqueue = 10\nother = 2\n",
        )
        r = run("merge", live, example)
        assert r.returncode == 0
        assert "ADDED=2" in r.stderr
        merged = tomllib.loads(r.stdout)
        assert merged["manager"]["benchmark"] == {"queue": 10, "other": 2}

    def test_dotted_key_in_live_not_duplicated(self, tmp_path):
        live = write(tmp_path, "live.toml", "[manager]\nauth.mode = 'basic'\n")
        example = write(
            tmp_path, "ex.toml", "[manager.auth]\nmode = 'basic'\n"
        )
        r = run("merge", live, example)
        assert r.returncode == 0
        assert "ADDED=0" in r.stderr
        tomllib.loads(r.stdout)

    def test_section_header_trailing_comment(self, tmp_path):
        live = write(tmp_path, "live.toml", "[manager]  # core\nport = 5000\n")
        example = write(
            tmp_path, "ex.toml", "[manager]\nport = 5000\nnew_key = 1\n"
        )
        r = run("merge", live, example)
        assert r.returncode == 0
        assert "ADDED=1" in r.stderr
        assert tomllib.loads(r.stdout)["manager"]["new_key"] == 1

    def test_parse_failure_exits_2(self, tmp_path):
        live = write(tmp_path, "live.toml", "not [ valid toml =\n")
        example = write(tmp_path, "ex.toml", LIVE_BASIC)
        r = run("merge", live, example)
        assert r.returncode == 2
        assert "PARSE_FAILED" in r.stderr

    def test_array_of_tables_in_example_not_spliced(self, tmp_path):
        # Regression: the merger previously lacked the [[aot]] guard the
        # pruner had; keys inside [[aot]] must never be spliced as sections.
        live = write(tmp_path, "live.toml", LIVE_BASIC)
        example = write(
            tmp_path,
            "ex.toml",
            LIVE_BASIC + "\n[[watch]]\nname = 'a'\npath = '/x'\n",
        )
        r = run("merge", live, example)
        assert r.returncode == 0
        merged = tomllib.loads(r.stdout)
        assert "watch" not in merged
        assert r.stdout == LIVE_BASIC or "ADDED=0" in r.stderr

    def test_multiline_array_anchor_span(self, tmp_path):
        live = write(
            tmp_path,
            "live.toml",
            "[manager]\nhosts = [\n  'a',\n  'b',\n]\nport = 5000\n",
        )
        example = write(
            tmp_path,
            "ex.toml",
            "[manager]\nhosts = [\n  'a',\n]\nnew_key = 7\nport = 5000\n",
        )
        r = run("merge", live, example)
        assert r.returncode == 0
        merged = tomllib.loads(r.stdout)
        assert merged["manager"]["new_key"] == 7
        assert merged["manager"]["hosts"] == ["a", "b"]


PRUNE_LIVE = """\
[manager]
port = 5000

[manager.benchmark]
# queue depth
stream_queue_size = 5000

[alarm_engine.timeouts]
manager_status = 2.0
manager_health = 1.5
"""


class TestPrune:
    def test_depth2_key_and_emptied_section_removed(self, tmp_path):
        live = write(tmp_path, "live.toml", PRUNE_LIVE)
        r = run("prune", live, "manager.benchmark.stream_queue_size")
        assert r.returncode == 0
        assert "PRUNED=1" in r.stderr
        pruned = tomllib.loads(r.stdout)
        assert "benchmark" not in pruned.get("manager", {})
        assert "[manager.benchmark]" not in r.stdout
        assert "# queue depth" not in r.stdout

    def test_depth3_key_in_kept_section(self, tmp_path):
        live = write(tmp_path, "live.toml", PRUNE_LIVE)
        r = run("prune", live, "alarm_engine.timeouts.manager_health")
        assert r.returncode == 0
        assert "PRUNED=1" in r.stderr
        pruned = tomllib.loads(r.stdout)
        assert pruned["alarm_engine"]["timeouts"] == {"manager_status": 2.0}

    def test_noop_when_absent(self, tmp_path):
        live = write(tmp_path, "live.toml", PRUNE_LIVE)
        r = run("prune", live, "manager.nonexistent")
        assert r.returncode == 0
        assert "PRUNED=0" in r.stderr
        assert r.stdout == PRUNE_LIVE

    def test_array_of_tables_protected(self, tmp_path):
        live = write(
            tmp_path,
            "live.toml",
            PRUNE_LIVE + "\n[[watch]]\nname = 'a'\n",
        )
        r = run("prune", live, "watch.name")
        assert r.returncode == 0
        assert "PRUNED=0" in r.stderr
        assert "name = 'a'" in r.stdout

    def test_multiline_array_value_fully_removed(self, tmp_path):
        live = write(
            tmp_path,
            "live.toml",
            "[manager]\nhosts = [\n  'a',\n  'b',\n]\nport = 5000\n",
        )
        r = run("prune", live, "manager.hosts")
        assert r.returncode == 0
        assert "PRUNED=1" in r.stderr
        pruned = tomllib.loads(r.stdout)
        assert pruned == {"manager": {"port": 5000}}

    def test_parse_failure_exits_2(self, tmp_path):
        live = write(tmp_path, "live.toml", "not [ valid =\n")
        r = run("prune", live, "a.b")
        assert r.returncode == 2
        assert "PARSE_FAILED" in r.stderr

    def test_multiple_keys(self, tmp_path):
        live = write(tmp_path, "live.toml", PRUNE_LIVE)
        r = run(
            "prune",
            live,
            "manager.benchmark.stream_queue_size",
            "alarm_engine.timeouts.manager_health",
        )
        assert r.returncode == 0
        assert "PRUNED=2" in r.stderr
        pruned = tomllib.loads(r.stdout)
        assert "benchmark" not in pruned["manager"]
        assert "manager_health" not in pruned["alarm_engine"]["timeouts"]


class TestUsage:
    @pytest.mark.parametrize(
        "argv", [[], ["merge"], ["merge", "one"], ["prune"], ["bogus", "a", "b"]]
    )
    def test_bad_usage_exits_64(self, argv, tmp_path):
        r = run(*argv)
        assert r.returncode == 64
        assert "usage:" in r.stderr
