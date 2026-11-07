"""install.sh writes AGENT_ROLE: auto when it resolved the role itself, so the agent re-probes at start."""
import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1] / "install" / "install.sh").read_text()


def test_auto_resolution_keeps_auto_in_the_yaml():
    block = _SRC[_SRC.index('if [[ "$ROLE" == "auto" ]]; then'):]
    block = block[:block.index('  → resolved role: $ROLE')]
    assert 'ROLE_WRITTEN="auto"' in block


def test_yaml_writer_receives_the_written_role():
    assert re.search(r'"\$AGENT_OS" "\$\{ROLE_WRITTEN:-\$ROLE\}"', _SRC)


def test_explicit_role_is_written_as_given():
    assert 'ROLE_WRITTEN=""' in _SRC and _SRC.count('ROLE_WRITTEN="auto"') == 1
