# agent/tests/test_llama_api_port.py
# The agent reports its llama API port in the pushed sample (#572).
from __future__ import annotations

import test_llama_props

llama_api_port = test_llama_props.llama.llama_api_port


def test_parses_the_port():
    assert llama_api_port("http://localhost:9931") == 9931
    assert llama_api_port("http://10.0.0.2:8080/") == 8080
    assert llama_api_port("http://10.0.0.2:9931/v1") == 9931
    assert llama_api_port("http://[::1]:8080") == 8080


def test_unparseable_urls_yield_none():
    assert llama_api_port("http://localhost") is None
    assert llama_api_port(None) is None
    assert llama_api_port("") is None
    assert llama_api_port("http://host:notaport") is None
