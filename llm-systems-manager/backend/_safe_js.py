"""safe_js: encode a value as a JS literal safe to inline in a <script> block."""
from __future__ import annotations

import json
from typing import Any


def safe_js(value: Any) -> str:
    """JSON-encode `value`, escaping every `<` so the literal can't close the
    script element or flip the parser into script-data-double-escaped state."""
    return json.dumps(value).replace("<", "\\u003c")
