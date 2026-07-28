# agent/tests/test_lms_download_forwarding.py
"""#468: /lms/download forwards the optional quantization pin to LM Studio."""
from __future__ import annotations

import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent / "providers" / "lms.py").read_text()


def test_download_endpoint_builds_payload_with_quantization():
    body = _SRC.split("def lms_download_endpoint", 1)[1].split("\ndef ", 1)[0]
    assert re.search(r'payload\s*=\s*\{"model":\s*model_id\}', body)
    assert 'body.get("quantization")' in body
    assert re.search(r'payload\["quantization"\]', body)
    assert re.search(r"json=payload", body)


def test_download_endpoint_does_not_validate_model_as_plain_id():
    # HF URLs must pass through; only load enforces _valid_model_id.
    body = _SRC.split("def lms_download_endpoint", 1)[1].split("\ndef ", 1)[0]
    assert "_valid_model_id" not in body
