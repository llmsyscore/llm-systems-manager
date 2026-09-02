# agent/tests/test_agent_readvertise.py
"""#791: the heartbeat re-registers whenever the routable address differs
from the host carried by the last advertised bind_url."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace

AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"


def _extract(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", AGENT_PY.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


class _FakePost:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.posts = []

    def post(self, url, **kw):
        self.posts.append((url, kw.get("json")))
        return SimpleNamespace(ok=self.ok, status_code=200 if self.ok else 500, text="")


class _DeadSocket:
    """socket module stand-in whose every connect fails (network not up)."""
    AF_INET = 2
    SOCK_DGRAM = 2

    @staticmethod
    def socket(*_a, **_k):
        raise OSError(101, "Network is unreachable")


def _ns(bind_host: str = "0.0.0.0", lan_ip=None, post_ok: bool = True) -> dict:
    ns: dict = {
        "CONFIG": SimpleNamespace(AGENT_BIND_HOST=bind_host, AGENT_BIND_PORT=8082,
                                  AGENT_HOSTNAME="agent-x", AGENT_OS="linux",
                                  AGENT_ROLE="llama_host", AGENT_DESCRIPTION="",
                                  AGENT_USER="llmsys", IMGGEN_PORT=1234,
                                  MANAGER_URL="https://manager:5000"),
        "Any": object,
        "socket": _DeadSocket,
        "logger": logging.getLogger("test"),
        "VERSION": "vtest",
        "_pick_non_loopback_ip": lambda: lan_ip,
        "_post_session": _FakePost(post_ok),
        "_machine_identity": lambda: "mid",
        "_tls_enabled": lambda: True,
        "_capabilities": lambda: {},
        "_provider_specs": lambda: [],
        "_last_advertised_host": None,
        "_READVERTISE_RETRY_S": 600.0,
        "_readvertise_retry_at": 0.0,
        "time": SimpleNamespace(monotonic=lambda: ns["now"]),
        "now": 1000.0,
    }
    for fn in ("_advertise_host", "_note_advertised", "_registration_body",
               "_post_registration", "_refresh_registration", "_maybe_readvertise"):
        exec(_extract(fn), ns)
    return ns


def _bind_urls(ns) -> list:
    return [body["bind_url"] for _url, body in ns["_post_session"].posts]


def test_hostname_fallback_when_network_is_down():
    ns = _ns()
    assert ns["_advertise_host"]() == "agent-x"


def test_accepted_registration_records_advertised_host():
    ns = _ns(lan_ip="192.0.2.7")
    body = ns["_registration_body"]()
    assert body["bind_url"] == "https://192.0.2.7:8082"
    assert ns["_last_advertised_host"] is None
    assert ns["_post_registration"](body, "tok") is True
    assert ns["_last_advertised_host"] == "192.0.2.7"


def test_rejected_registration_records_nothing():
    ns = _ns(lan_ip="192.0.2.7", post_ok=False)
    assert ns["_post_registration"](ns["_registration_body"](), "tok") is False
    assert ns["_last_advertised_host"] is None


def test_startup_refresh_failure_leaves_readvertise_armed():
    ns = _ns(lan_ip="192.0.2.7", post_ok=False)
    ns["_refresh_registration"]("tok")
    ns["_post_session"].ok = True
    ns["_maybe_readvertise"]("tok")
    assert _bind_urls(ns)[-1] == "https://192.0.2.7:8082"
    assert ns["_last_advertised_host"] == "192.0.2.7"


def test_explicit_bind_host_wins():
    ns = _ns(bind_host="192.0.2.5", lan_ip="192.0.2.7")
    assert ns["_advertise_host"]() == "192.0.2.5"


def _seed(ns) -> None:
    """Register once and forget the POST so later assertions see only re-registrations."""
    ns["_refresh_registration"]("tok")
    ns["_post_session"].posts.clear()


def test_readvertise_noop_when_host_unchanged():
    ns = _ns(lan_ip="192.0.2.7")
    _seed(ns)
    ns["_maybe_readvertise"]("tok")
    assert ns["_post_session"].posts == []


def test_readvertise_waits_while_network_stays_down():
    ns = _ns()
    _seed(ns)
    assert ns["_last_advertised_host"] == "agent-x"
    ns["_maybe_readvertise"]("tok")
    assert ns["_post_session"].posts == []


def test_readvertise_replaces_hostname_once_ip_appears():
    ns = _ns()
    _seed(ns)
    ns["_pick_non_loopback_ip"] = lambda: "192.0.2.7"
    ns["_maybe_readvertise"]("tok")
    assert ns["_post_session"].posts[0][0] == "https://manager:5000/api/agents/register"
    assert _bind_urls(ns) == ["https://192.0.2.7:8082"]
    assert ns["_last_advertised_host"] == "192.0.2.7"
    ns["_maybe_readvertise"]("tok")
    assert len(ns["_post_session"].posts) == 1, "re-registered again with no change"


def test_readvertise_follows_an_ip_change():
    ns = _ns(lan_ip="192.0.2.7")
    _seed(ns)
    ns["_pick_non_loopback_ip"] = lambda: "192.0.2.50"
    ns["_maybe_readvertise"]("tok")
    assert _bind_urls(ns) == ["https://192.0.2.50:8082"]


def test_readvertise_retries_after_rejected_refresh_with_backoff():
    ns = _ns()
    _seed(ns)
    ns["_post_session"].ok = False
    ns["_pick_non_loopback_ip"] = lambda: "192.0.2.7"
    ns["_maybe_readvertise"]("tok")
    assert len(ns["_post_session"].posts) == 1
    assert ns["_last_advertised_host"] == "agent-x"
    ns["_maybe_readvertise"]("tok")
    assert len(ns["_post_session"].posts) == 1, "retried before the backoff elapsed"
    ns["now"] += 601
    ns["_maybe_readvertise"]("tok")
    assert len(ns["_post_session"].posts) == 2


def test_readvertise_backs_off_after_post_exception():
    ns = _ns()
    _seed(ns)
    ns["_pick_non_loopback_ip"] = lambda: "192.0.2.7"

    def boom(url, **kw):
        raise OSError("connection reset")
    ns["_post_session"].post = boom
    ns["_maybe_readvertise"]("tok")
    assert ns["_last_advertised_host"] == "agent-x"
    assert ns["_readvertise_retry_at"] == ns["now"] + 600.0


def test_initial_registration_records_advertised_host():
    body = _extract("registry_register_blocking")
    assert re.search(r"if r\.ok:\n\s+_note_advertised\(body\)", body), \
        "initial registration never records the accepted bind_url host"


def test_heartbeat_loop_calls_readvertise():
    body = _extract("heartbeat_loop")
    assert re.search(r"_maybe_readvertise\(tok\)", body), \
        "heartbeat_loop never re-checks the advertised host"
