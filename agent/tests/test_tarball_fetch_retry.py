"""#1108: _fetch_tarball() retries a short or broken tarball body on a fresh session and names the reason."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Iterator

import types

import pytest

AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"


def _extract(kind: str, name: str) -> str:
    m = re.search(rf"^{kind} {name}\b.*?(?=^\S)", AGENT_PY.read_text(), re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name} from {AGENT_PY.name}"
    return m.group(0)


class RequestException(Exception):
    pass


class ChunkedEncodingError(RequestException):
    pass


requests = types.SimpleNamespace(RequestException=RequestException, Session=object,
                                 exceptions=types.SimpleNamespace(ChunkedEncodingError=ChunkedEncodingError))


def _ns() -> dict:
    ns = {"requests": requests, "time": time, "Any": Any, "Iterator": Iterator}
    exec(compile(_extract("class", "TarballFetchError") + "\n" + _extract("def", "_fetch_tarball"), str(AGENT_PY), "exec"), ns)
    return ns


class _Resp:
    def __init__(self, body: bytes, length: int, status: int = 200, break_after: bool = False, encoding: str = ""):
        self.status_code, self._body, self._break = status, body, break_after
        self.headers = {"Content-Length": str(length), "X-Agent-Tarball-Sig": "sig"}
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]
        if self._break:
            raise ChunkedEncodingError("IncompleteRead")


def _sessions(responses):
    made = []

    class _Sess:
        def __init__(self):
            self.verify, self.closed = None, False
            made.append(self)

        def get(self, url, **kw):
            return responses.pop(0)

        def close(self):
            self.closed = True
    return _Sess, made


def _run(ns, responses, tmp_path, attempts=3):
    factory, made = _sessions(responses)
    out, sleeps = {}, []
    dest = tmp_path / "agent.tgz"
    notes = list(ns["_fetch_tarball"]("http://m/api/agent-tarball", {"Authorization": "Bearer t"}, str(dest), "/ca.pem",
                                      out, attempts=attempts, session_factory=factory, sleep=sleeps.append))
    return notes, out, sleeps, made, dest


def test_first_try_success_uses_one_session_with_the_ca(tmp_path):
    body = b"x" * 200_000
    notes, out, sleeps, made, dest = _run(_ns(), [_Resp(body, len(body))], tmp_path)
    assert notes == [] and sleeps == [] and out["tries"] == 1 and out["headers"]["X-Agent-Tarball-Sig"] == "sig"
    assert dest.read_bytes() == body and len(made) == 1 and made[0].verify == "/ca.pem" and made[0].closed


def test_short_read_then_success_retries_on_a_fresh_session(tmp_path):
    body = b"y" * 300_000
    notes, out, sleeps, made, dest = _run(_ns(), [_Resp(body[:131072], len(body), break_after=True), _Resp(body, len(body))], tmp_path)
    assert notes == ["fetch retry 2/3: connection broken after 131072 of 300000 bytes"]
    assert out["tries"] == 2 and sleeps == [1.0] and len(made) == 2 and all(s.closed for s in made)
    assert dest.read_bytes() == body


def test_a_quietly_short_body_counts_as_broken(tmp_path):
    body = b"z" * 1000
    notes, out, _, _, _ = _run(_ns(), [_Resp(body[:10], len(body)), _Resp(body, len(body))], tmp_path)
    assert notes == ["fetch retry 2/3: connection broken after 10 of 1000 bytes"] and out["tries"] == 2


def test_three_short_reads_raise_with_the_reason(tmp_path):
    ns = _ns()
    body = b"q" * 300_000
    resps = [_Resp(body[:131072], len(body), break_after=True) for _ in range(3)]
    with pytest.raises(ns["TarballFetchError"]) as e:
        _run(ns, resps, tmp_path)
    assert str(e.value) == "connection broken after 131072 of 300000 bytes (3 tries)"


def test_client_error_fails_at_once_and_is_not_retryable(tmp_path):
    ns = _ns()
    resps = [_Resp(b"", 0, status=401), _Resp(b"ok", 2)]
    with pytest.raises(ns["TarballFetchError"]) as e:
        _run(ns, resps, tmp_path)
    assert str(e.value) == "HTTP 401 from the manager" and e.value.retryable is False and len(resps) == 1


def test_server_errors_are_retried_and_named(tmp_path):
    ns = _ns()
    notes, out, _, _, dest = _run(ns, [_Resp(b"", 0, status=503), _Resp(b"ok", 2)], tmp_path)
    assert notes == ["fetch retry 2/3: HTTP 503 from the manager"] and out["tries"] == 2 and dest.read_bytes() == b"ok"
    with pytest.raises(ns["TarballFetchError"]) as e:
        _run(ns, [_Resp(b"", 0, status=502) for _ in range(3)], tmp_path)
    assert str(e.value) == "HTTP 502 from the manager (3 tries)" and e.value.retryable is True


def test_a_stale_status_reason_does_not_leak_into_the_next_try(tmp_path):
    ns = _ns()
    notes, _, _, _, _ = _run(ns, [_Resp(b"", 0, status=503), _Resp(b"ab", 10, break_after=True), _Resp(b"x", 1)], tmp_path)
    assert notes == ["fetch retry 2/3: HTTP 503 from the manager", "fetch retry 3/3: connection broken after 2 of 10 bytes"]


def test_an_encoded_body_skips_the_length_check(tmp_path):
    notes, out, _, _, _ = _run(_ns(), [_Resp(b"decoded", 999, encoding="gzip")], tmp_path)
    assert notes == [] and out["tries"] == 1
