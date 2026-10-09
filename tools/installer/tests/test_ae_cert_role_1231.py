# #1231: the alarm-engine certificate the installer pre-issues carries the
# alarm-engine role, so a fresh install never needs a second AE start.
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPO / "tools" / "installer" / "install-config-bootstrap.sh"


def _sign_call() -> str:
    text = BOOTSTRAP.read_text()
    m = re.search(r"_pki\.sign_agent_cert\((.*?)\)\n", text, re.DOTALL)
    assert m, "sign_agent_cert call not found in install-config-bootstrap.sh"
    return m.group(1)


def test_pre_issued_alarm_engine_cert_requests_its_role():
    call = _sign_call()
    assert 'agent_id="llm-systems-alarm-engine"' in call
    assert "role=_pki.ROLE_ALARM_ENGINE" in call
