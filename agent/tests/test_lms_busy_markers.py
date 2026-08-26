"""#619: agent active-model picker uses the shared LMS busy allowlist."""
import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent
        / "llm-systems-agent.py").read_text(encoding="utf-8")


def test_markers_defined_and_match_manager_energy():
    m = re.search(r"_LMS_BUSY_MARKERS = \(([^)]*)\)", _SRC)
    assert m, "agent _LMS_BUSY_MARKERS tuple missing"
    markers = set(re.findall(r'"([A-Z]+)"', m.group(1)))
    assert markers == {"PROMPT", "STREAM", "GENERAT", "PREDICT", "QUEUE"}


def test_active_picker_uses_markers_not_open_ended_rule():
    picker = _SRC.split('"active": next(')[1][:250]
    assert "_LMS_BUSY_MARKERS" in picker
    assert '"IDLE"' not in picker
