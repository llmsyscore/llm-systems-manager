# Asserts the two removed-paths manifests stay mirrored: every agent/ file
# entry in the manager-host manifest must exist in the agent-side one too.
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MAIN_MANIFEST = REPO / "tools/installer/removed-paths.manifest"
AGENT_MANIFEST = REPO / "agent/install/removed-paths.manifest"


def parse(path):
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        directive, _pr, value = line.split("|", 2)
        entries.append((directive, value))
    return entries


def test_manifests_parse():
    for path in (MAIN_MANIFEST, AGENT_MANIFEST):
        for directive, value in parse(path):
            assert directive in ("file", "toml-key"), f"{path}: bad directive {directive}"
            assert value and not value.startswith("/") and ".." not in value


def test_agent_entries_mirrored_both_ways():
    main_agent_files = {
        value[len("agent/"):]
        for directive, value in parse(MAIN_MANIFEST)
        if directive == "file" and value.startswith("agent/")
    }
    agent_files = {value for directive, value in parse(AGENT_MANIFEST) if directive == "file"}
    assert main_agent_files == agent_files, (
        "agent/ file entries must appear in BOTH manifests: "
        f"main-only={main_agent_files - agent_files}, agent-only={agent_files - main_agent_files}"
    )


def test_agent_manifest_has_no_toml_keys():
    # Agent config is YAML (reconciled separately); toml-key entries there
    # would be silently ignored by the agent prune loop.
    assert not [v for d, v in parse(AGENT_MANIFEST) if d != "file"]
