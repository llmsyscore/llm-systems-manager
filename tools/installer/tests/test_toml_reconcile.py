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

    def test_new_section_carries_only_missing_keys(self, tmp_path):
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

    def test_new_section_lands_at_its_example_position(self, tmp_path):
        # #528: a section missing from live must be spliced where the example
        # puts it, not appended after every other section.
        live = write(
            tmp_path, "live.toml", "[manager]\nport = 5000\n\n[logging]\nlevel = 'INFO'\n"
        )
        example = write(
            tmp_path,
            "ex.toml",
            "[manager]\nport = 5000\n\n[manager.energy]\ncloud_in = 0.15\n"
            "\n[logging]\nlevel = 'INFO'\n",
        )
        r = run("merge", live, example)
        assert r.returncode == 0
        lines = r.stdout.splitlines()
        assert lines.index("[manager.energy]") < lines.index("[logging]")

    def test_new_section_keys_are_not_blank_separated(self, tmp_path):
        # #528: keys inside a freshly added section must stay contiguous.
        live = write(tmp_path, "live.toml", "[manager]\nport = 5000\n")
        example = write(
            tmp_path,
            "ex.toml",
            "[manager]\nport = 5000\n\n[manager.discord]\n# bot\n"
            "enabled = false\nbot_token = ''\nguild_id = ''\n",
        )
        r = run("merge", live, example)
        assert r.returncode == 0
        body = r.stdout.split("[manager.discord]\n", 1)[1].rstrip("\n").splitlines()
        assert "" not in body, f"blank line inside new section: {body!r}"

    def test_spliced_key_is_not_blank_padded(self, tmp_path):
        # #528: splicing into an existing section must not pad with blanks.
        live = write(tmp_path, "live.toml", "[manager]\na = 1\nc = 3\n")
        example = write(tmp_path, "ex.toml", "[manager]\na = 1\nb = 2\nc = 3\n")
        r = run("merge", live, example)
        assert r.returncode == 0
        body = r.stdout.rstrip("\n").splitlines()[1:]
        assert body == ["a = 1", "b = 2", "c = 3"], body

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


EX_ORDERED = """\
[manager]
port = 5000
tls_port = 5443

[manager.reportcard]
price_kwh = 0.15

[manager.energy]
# Energy & cost intelligence.
cloud_in = 0.15
cloud_out = 0.60

[manager.discord]
enabled = false
bot_token = ""

[logging]
level = "INFO"
"""

# What a config upgraded by the buggy merge looks like: both new sections
# dumped past [logging], every key blank-separated (#528).
LIVE_MISORDERED = """\
[manager]
port = 5000
tls_port = 5443

[manager.reportcard]
price_kwh = 0.15

[logging]
level = "INFO"

[manager.energy]
# Energy & cost intelligence.
cloud_in = 0.15

cloud_out = 0.60

[manager.discord]
enabled = false

bot_token = ""
"""


class TestReorder:
    def test_moves_misplaced_sections_into_example_order(self, tmp_path):
        live = write(tmp_path, "live.toml", LIVE_MISORDERED)
        example = write(tmp_path, "ex.toml", EX_ORDERED)
        r = run("reorder", live, example)
        assert r.returncode == 0
        order = [ln for ln in r.stdout.splitlines() if ln.startswith("[")]
        assert order == [
            "[manager]",
            "[manager.reportcard]",
            "[manager.energy]",
            "[manager.discord]",
            "[logging]",
        ]

    def test_reorder_preserves_parsed_config_exactly(self, tmp_path):
        live = write(tmp_path, "live.toml", LIVE_MISORDERED)
        example = write(tmp_path, "ex.toml", EX_ORDERED)
        r = run("reorder", live, example)
        assert r.returncode == 0
        assert tomllib.loads(r.stdout) == tomllib.loads(LIVE_MISORDERED)

    def test_strips_blank_lines_between_keys(self, tmp_path):
        live = write(tmp_path, "live.toml", LIVE_MISORDERED)
        example = write(tmp_path, "ex.toml", EX_ORDERED)
        r = run("reorder", live, example)
        assert r.returncode == 0
        section = r.stdout.split("[manager.energy]\n", 1)[1].split("[manager.discord]")[0]
        assert "" not in section.rstrip("\n").splitlines()

    def test_comment_block_travels_with_its_key(self, tmp_path):
        live = write(tmp_path, "live.toml", LIVE_MISORDERED)
        example = write(tmp_path, "ex.toml", EX_ORDERED)
        r = run("reorder", live, example)
        lines = r.stdout.splitlines()
        i = lines.index("cloud_in = 0.15")
        assert lines[i - 1] == "# Energy & cost intelligence."

    def test_reorders_keys_within_a_section(self, tmp_path):
        live = write(
            tmp_path, "live.toml", "[manager]\ntls_port = 5443\nport = 5000\n"
        )
        example = write(tmp_path, "ex.toml", "[manager]\nport = 5000\ntls_port = 5443\n")
        r = run("reorder", live, example)
        assert r.returncode == 0
        body = r.stdout.rstrip("\n").splitlines()[1:]
        assert body == ["port = 5000", "tls_port = 5443"], body

    def test_noop_when_already_in_order(self, tmp_path):
        live = write(tmp_path, "live.toml", EX_ORDERED)
        example = write(tmp_path, "ex.toml", EX_ORDERED)
        r = run("reorder", live, example)
        assert r.returncode == 0
        assert "MOVED=0" in r.stderr
        assert r.stdout == EX_ORDERED

    def test_wrong_parent_key_is_reported_not_moved(self, tmp_path):
        # A key under the wrong table can only move by changing its path,
        # which would change what the config resolves to (#528).
        live = write(
            tmp_path, "live.toml", "[manager]\nport = 5000\ncloud_in = 0.99\n"
        )
        example = write(
            tmp_path, "ex.toml", "[manager]\nport = 5000\n\n[manager.energy]\ncloud_in = 0.15\n"
        )
        r = run("reorder", live, example)
        assert r.returncode == 0
        assert "MISPLACED" in r.stderr
        assert "cloud_in" in r.stderr
        assert "manager.energy" in r.stderr
        assert tomllib.loads(r.stdout)["manager"]["cloud_in"] == 0.99

    def test_unknown_custom_section_is_preserved(self, tmp_path):
        live = write(
            tmp_path,
            "live.toml",
            "[manager]\nport = 5000\n\n[mine.custom]\nkeep = true\n",
        )
        example = write(tmp_path, "ex.toml", "[manager]\nport = 5000\n")
        r = run("reorder", live, example)
        assert r.returncode == 0
        assert tomllib.loads(r.stdout)["mine"]["custom"]["keep"] is True

    def test_array_of_tables_order_preserved(self, tmp_path):
        live = write(
            tmp_path,
            "live.toml",
            "[manager]\nport = 5000\n\n[[tiers]]\nn = 1\n\n[[tiers]]\nn = 2\n",
        )
        example = write(
            tmp_path,
            "ex.toml",
            "[manager]\nport = 5000\n\n[[tiers]]\nn = 1\n\n[[tiers]]\nn = 2\n",
        )
        r = run("reorder", live, example)
        assert r.returncode == 0
        assert [t["n"] for t in tomllib.loads(r.stdout)["tiers"]] == [1, 2]

    def test_parse_failure_exits_2(self, tmp_path):
        live = write(tmp_path, "live.toml", "not [ valid toml =\n")
        example = write(tmp_path, "ex.toml", EX_ORDERED)
        r = run("reorder", live, example)
        assert r.returncode == 2
        assert "PARSE_FAILED" in r.stderr

    def test_stale_banner_from_removed_section_is_replaced(self, tmp_path):
        # #528: pruning a section used to strand its banner above whatever
        # section followed it.
        live = write(
            tmp_path,
            "live.toml",
            "[manager]\nport = 5000\n\n"
            "# ── AGENT (schema only) ────\n"
            "# Documentary. Agents read their own agent_config.yaml.\n"
            "[manager.gateway]\nenabled = true\n",
        )
        example = write(
            tmp_path,
            "ex.toml",
            "[manager]\nport = 5000\n\n"
            "# ── Inference gateway ────\n"
            "# OpenAI-compatible endpoint.\n"
            "[manager.gateway]\nenabled = true\n",
        )
        r = run("reorder", live, example)
        assert r.returncode == 0
        assert "AGENT (schema only)" not in r.stdout
        assert "Inference gateway" in r.stdout
        assert "STALE_COMMENT" in r.stderr
        assert tomllib.loads(r.stdout)["manager"]["gateway"]["enabled"] is True

    def test_repairs_a_config_scrambled_by_the_old_merge(self, tmp_path):
        # Shape seen on a real upgraded host: several sections stranded past
        # [logging], keys blank-separated, plus a stranded banner.
        live = write(
            tmp_path,
            "live.toml",
            "[manager]\nport = 5000\ntls_port = 5443\n\n"
            "[logging]\nlevel = 'INFO'\n\n"
            "# ── AGENT (schema only) ────\n"
            "[manager.gateway]\nenabled = true\n\n"
            "[manager.reportcard]\nprice_kwh = 0.15\n\n"
            "[manager.energy]\ncloud_in = 0.15\n\ncloud_out = 0.60\n",
        )
        example = write(
            tmp_path,
            "ex.toml",
            "[manager]\nport = 5000\ntls_port = 5443\n\n"
            "# ── Inference gateway ────\n"
            "[manager.gateway]\nenabled = true\n\n"
            "[manager.reportcard]\nprice_kwh = 0.15\n\n"
            "[manager.energy]\ncloud_in = 0.15\ncloud_out = 0.60\n\n"
            "[logging]\nlevel = 'INFO'\n",
        )
        r = run("reorder", live, example)
        assert r.returncode == 0
        assert [ln for ln in r.stdout.splitlines() if ln.startswith("[")] == [
            "[manager]",
            "[manager.gateway]",
            "[manager.reportcard]",
            "[manager.energy]",
            "[logging]",
        ]
        energy = r.stdout.split("[manager.energy]\n", 1)[1].split("[logging]")[0]
        assert "" not in energy.rstrip("\n").splitlines()
        assert "AGENT (schema only)" not in r.stdout
        with open(live) as f:
            assert tomllib.loads(r.stdout) == tomllib.loads(f.read())

    def test_operator_comment_under_matching_banner_is_kept(self, tmp_path):
        banner = "# ── Inference gateway ────\n"
        live = write(
            tmp_path,
            "live.toml",
            "[manager]\nport = 5000\n\n" + banner
            + "# operator note: do not raise this\n[manager.gateway]\nenabled = true\n",
        )
        example = write(
            tmp_path,
            "ex.toml",
            "[manager]\nport = 5000\n\n" + banner + "[manager.gateway]\nenabled = true\n",
        )
        r = run("reorder", live, example)
        assert r.returncode == 0
        assert "operator note: do not raise this" in r.stdout


class TestPruneBanner:
    def test_emptied_section_takes_its_banner_with_it(self, tmp_path):
        # #528 root cause: the banner sits in the previous section's text, so
        # dropping an emptied section used to strand it.
        live = write(
            tmp_path,
            "live.toml",
            "[manager]\nport = 5000\n\n"
            "# ── AGENT (schema only) ────\n"
            "# Documentary.\n"
            "[agent]\nschema = 1\n\n"
            "[logging]\nlevel = 'INFO'\n",
        )
        r = run("prune", live, "agent.schema")
        assert r.returncode == 0
        assert "PRUNED=1" in r.stderr
        assert "AGENT (schema only)" not in r.stdout
        assert "Documentary" not in r.stdout
        parsed = tomllib.loads(r.stdout)
        assert "agent" not in parsed
        assert parsed["logging"]["level"] == "INFO"
        assert parsed["manager"]["port"] == 5000


class TestUsage:
    @pytest.mark.parametrize(
        "argv", [[], ["merge"], ["merge", "one"], ["prune"], ["bogus", "a", "b"],
                 ["reorder"], ["reorder", "one"]]
    )
    def test_bad_usage_exits_64(self, argv, tmp_path):
        r = run(*argv)
        assert r.returncode == 64
        assert "usage:" in r.stderr
