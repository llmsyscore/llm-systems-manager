"""#552: values inlined into a <script> block are JSON-encoded with every `<`
escaped, so no injected value can close the element early or flip the parser
into script-data-double-escaped state. Both injectors (the dashboard globals
and the AE-iframe ALARM_WS_URL) use the same helper."""
from __future__ import annotations

import json

import proxies
from _safe_js import safe_js

_BREAKOUT = "ws://h:1/</script><script>alert(1)</script>"
# `<!--` + `<script` puts the HTML parser in double-escaped state, where the
# real </script> no longer terminates the element.
_DOUBLE_ESCAPE = "ws://h:1/<!--<script>"


def test_safe_js_escapes_every_angle_bracket():
    out = safe_js(_BREAKOUT)
    assert "<" not in out
    assert "\\u003c/script" in out
    # Still the same string once the browser parses it back.
    assert json.loads(out) == _BREAKOUT


def test_safe_js_neutralizes_the_double_escape_sequence():
    out = safe_js(_DOUBLE_ESCAPE)
    assert "<" not in out
    assert json.loads(out) == _DOUBLE_ESCAPE


def test_safe_js_quotes_and_escapes_normal_values():
    assert safe_js("") == '""'
    assert safe_js('a"b\\c') == '"a\\"b\\\\c"'
    assert safe_js(None) == "null"


def test_iframe_injection_cannot_break_out_of_the_script(monkeypatch):
    monkeypatch.setattr(proxies, "ae_ws_url_for_browser", lambda: _BREAKOUT)
    out = proxies._inject_alarm_ws_url(b"<html><head></head><body></body></html>")
    # One script element, and its only `<` are the tags we emitted.
    assert out.count(b"<script>") == 1
    assert out.count(b"</script>") == 1
    assert b"\\u003c/script" in out


def test_iframe_injection_cannot_double_escape_the_parser(monkeypatch):
    monkeypatch.setattr(proxies, "ae_ws_url_for_browser", lambda: _DOUBLE_ESCAPE)
    out = proxies._inject_alarm_ws_url(b"<html><head></head><body>x</body></html>")
    assert b"<!--" not in out
    assert out.count(b"</script>") == 1
    assert b"<body>x</body>" in out


def test_iframe_injection_emits_a_quoted_js_string(monkeypatch):
    monkeypatch.setattr(proxies, "ae_ws_url_for_browser", lambda: "ws://ae:8081/ws")
    out = proxies._inject_alarm_ws_url(b"<html><head></head></html>")
    assert b"window.ALARM_WS_URL=\"ws://ae:8081/ws\";" in out


def test_iframe_injection_marks_an_absent_stream_with_empty_string(monkeypatch):
    # #519: "" tells websocket.js not to dial; it must stay a JS string.
    monkeypatch.setattr(proxies, "ae_ws_url_for_browser", lambda: "")
    out = proxies._inject_alarm_ws_url(b"<html><head></head></html>")
    assert b'window.ALARM_WS_URL="";' in out


def test_injection_is_a_noop_without_a_head(monkeypatch):
    monkeypatch.setattr(proxies, "ae_ws_url_for_browser", lambda: "ws://ae:8081/ws")
    raw = b"<html><body>no head here</body></html>"
    assert proxies._inject_alarm_ws_url(raw) == raw


def test_dashboard_globals_use_the_shared_helper():
    import inspect

    import manager_mod as M

    src = inspect.getsource(M.index) if hasattr(M, "index") else ""
    if not src:
        return
    assert "json.dumps(v).replace" not in src, \
        "dashboard globals re-implement safe_js instead of importing it"
